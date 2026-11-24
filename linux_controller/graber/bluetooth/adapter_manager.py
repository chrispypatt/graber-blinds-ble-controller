"""Stable adapter assignment, concurrency limits, and health accounting."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from ..config import AdapterConfig, BlindConfig


class AdapterUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedAdapter:
    id: str
    controller_address: str
    device: str  # transient BlueZ name such as hci1; never persisted as identity
    usb_path: str | None = None


@dataclass
class AdapterStats:
    assignments: int = 0
    connection_attempts: int = 0
    connection_successes: int = 0
    failures: int = 0
    last_error: str | None = None
    last_seen: float | None = None
    disconnects: int = 0
    recoveries: int = 0
    last_unavailable: float | None = None
    last_recovered: float | None = None
    resolution_failures: int = 0
    last_resolution_error: str | None = None


@dataclass
class AdapterRuntime:
    config: AdapterConfig
    resolved: ResolvedAdapter | None = None
    stats: AdapterStats = field(default_factory=AdapterStats)
    connection_slots: asyncio.Semaphore = field(init=False)
    cold_slots: asyncio.Semaphore = field(init=False)
    cold_start_lock: asyncio.Lock = field(init=False)
    last_cold_start: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.connection_slots = asyncio.Semaphore(self.config.max_connections)
        self.cold_slots = asyncio.Semaphore(self.config.max_cold_connections)
        self.cold_start_lock = asyncio.Lock()


Resolver = Callable[[tuple[AdapterConfig, ...]], Awaitable[list[ResolvedAdapter]]]


class AdapterManager:
    def __init__(self, configs: tuple[AdapterConfig, ...], resolver: Resolver):
        self._configs = configs
        self._resolver = resolver
        self._runtime = {item.id: AdapterRuntime(item) for item in configs}
        self._refresh_lock = asyncio.Lock()

    @property
    def stats(self) -> dict[str, AdapterStats]:
        return {key: value.stats for key, value in self._runtime.items()}

    def health(self) -> dict[str, dict[str, object]]:
        return {
            adapter_id: {
                "status": (
                    "unknown" if runtime.stats.last_resolution_error is not None
                    else "online" if runtime.resolved is not None
                    else "unavailable"
                ),
                "connection_attempts": runtime.stats.connection_attempts,
                "connection_successes": runtime.stats.connection_successes,
                "failures": runtime.stats.failures,
                "last_error": runtime.stats.last_error,
                "last_seen": runtime.stats.last_seen,
                "disconnects": runtime.stats.disconnects,
                "recoveries": runtime.stats.recoveries,
                "last_unavailable": runtime.stats.last_unavailable,
                "last_recovered": runtime.stats.last_recovered,
                "resolution_failures": runtime.stats.resolution_failures,
                "last_resolution_error": runtime.stats.last_resolution_error,
            }
            for adapter_id, runtime in self._runtime.items()
        }

    async def refresh(self, *, resolver: Resolver | None = None) -> None:
        async with self._refresh_lock:
            try:
                resolved = {item.id: item for item in await (resolver or self._resolver)(self._configs)}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                # Resolution failure means controller state is unknown, not
                # necessarily offline. Preserve active handles and retry on the
                # next bounded refresh instead of killing the health task.
                for runtime in self._runtime.values():
                    runtime.stats.resolution_failures += 1
                    runtime.stats.last_resolution_error = error
                return
            now = time.time()
            for adapter_id, runtime in self._runtime.items():
                self._update_resolution(runtime, resolved.get(adapter_id), now)

    def _update_resolution(
        self, runtime: AdapterRuntime, current: ResolvedAdapter | None, now: float,
    ) -> None:
        runtime.stats.last_resolution_error = None
        previous = runtime.resolved
        if current is None:
            if previous is not None:
                self._mark_unavailable(runtime, "adapter not found during refresh", now=now)
            return

        expected_path = runtime.config.usb_path
        if expected_path and current.usb_path != expected_path and (
            previous is None or previous.usb_path != current.usb_path
        ):
            logging.getLogger("graber").warning(
                "adapter %s USB path changed from %s to %s",
                runtime.config.id, expected_path, current.usb_path,
            )

        rebound = previous is not None and previous.device != current.device
        recovered = previous is None and runtime.stats.last_unavailable is not None
        if rebound:
            # A new hci name implies a disconnect between samples. Only
            # _mark_unavailable knows when we actually observed an outage.
            runtime.stats.disconnects += 1
        if rebound or recovered:
            runtime.stats.recoveries += 1
            runtime.stats.last_recovered = now
            runtime.stats.last_error = None
        runtime.resolved = current
        runtime.stats.last_seen = now

    async def candidates(self, blind: BlindConfig) -> list[AdapterRuntime]:
        if not any(runtime.resolved for runtime in self._runtime.values()):
            await self.refresh()
        ordered: list[str] = []
        if blind.preferred_adapter:
            ordered.append(blind.preferred_adapter)
        ordered.extend(x for x in blind.fallback_adapters if x not in ordered)
        if not ordered:
            ordered = list(self._runtime)
        result = [self._runtime[x] for x in ordered if self._runtime[x].resolved]
        if not result:
            await self.refresh()
            result = [self._runtime[x] for x in ordered if self._runtime[x].resolved]
        if not result:
            raise AdapterUnavailable(f"no configured adapter available for {blind.id}")
        return result

    @asynccontextmanager
    async def connection(self, runtime: AdapterRuntime) -> AsyncIterator[ResolvedAdapter]:
        async with runtime.connection_slots:
            if runtime.resolved is None:
                await self.refresh()
            if runtime.resolved is None:
                raise AdapterUnavailable(f"adapter {runtime.config.id} disappeared")
            runtime.stats.assignments += 1
            yield runtime.resolved

    @asynccontextmanager
    async def cold_connection(self, runtime: AdapterRuntime) -> AsyncIterator[float]:
        queued_at = time.monotonic()
        await runtime.cold_slots.acquire()
        try:
            # Only admission to a new cold attempt is spaced. Once admitted,
            # attempts may overlap up to max_cold_connections. This avoids a
            # burst of same-instant BlueZ operations without serializing the
            # full connection lifecycle.
            async with runtime.cold_start_lock:
                if runtime.last_cold_start is not None:
                    delay = (
                        runtime.last_cold_start
                        + runtime.config.cold_start_spacing_s
                        - time.monotonic()
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                runtime.last_cold_start = time.monotonic()
            queue_wait_ms = (time.monotonic() - queued_at) * 1000
            # Re-check after waiting in the per-adapter queue. A health refresh
            # may have observed a USB disconnect while this work was queued.
            if runtime.resolved is None:
                await self.refresh()
            if runtime.resolved is None:
                raise AdapterUnavailable(f"adapter {runtime.config.id} disappeared")
            runtime.stats.connection_attempts += 1
            yield queue_wait_ms
        finally:
            runtime.cold_slots.release()

    def record_success(self, runtime: AdapterRuntime) -> None:
        runtime.stats.connection_successes += 1
        runtime.stats.last_error = None

    def record_failure(self, runtime: AdapterRuntime, error: BaseException) -> None:
        runtime.stats.failures += 1
        runtime.stats.last_error = f"{type(error).__name__}: {error}"
        if self._adapter_disappeared(error):
            self._mark_unavailable(runtime, runtime.stats.last_error)

    @staticmethod
    def _adapter_disappeared(error: BaseException) -> bool:
        """Recognize controller-loss errors without blaming normal GATT failures."""
        text = f"{type(error).__name__}: {error}".casefold()
        markers = (
            "adapter is not available",
            "adapter not available",
            "adapter not found",
            "no such adapter",
            "no default controller available",
            "org.bluez.error.notready",
            "org.freedesktop.dbus.error.unknownobject",
            "controller is not available",
            "controller unavailable",
            "controller powered off",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _mark_unavailable(
        runtime: AdapterRuntime, error: str, *, now: float | None = None,
    ) -> None:
        if runtime.resolved is not None:
            runtime.stats.disconnects += 1
            runtime.stats.last_unavailable = time.time() if now is None else now
        runtime.resolved = None
        runtime.stats.last_error = error
