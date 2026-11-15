"""Stable adapter assignment, concurrency limits, and health accounting."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from .config import AdapterConfig, BlindConfig


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


@dataclass
class AdapterRuntime:
    config: AdapterConfig
    resolved: ResolvedAdapter | None = None
    stats: AdapterStats = field(default_factory=AdapterStats)
    connection_slots: asyncio.Semaphore = field(init=False)
    cold_slots: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.connection_slots = asyncio.Semaphore(self.config.max_connections)
        self.cold_slots = asyncio.Semaphore(self.config.max_cold_connections)


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
                "status": "online" if runtime.resolved is not None else "unavailable",
                "connection_attempts": runtime.stats.connection_attempts,
                "connection_successes": runtime.stats.connection_successes,
                "failures": runtime.stats.failures,
                "last_error": runtime.stats.last_error,
                "last_seen": runtime.stats.last_seen,
            }
            for adapter_id, runtime in self._runtime.items()
        }

    async def refresh(self) -> None:
        async with self._refresh_lock:
            resolved = {item.id: item for item in await self._resolver(self._configs)}
            now = time.time()
            for adapter_id, runtime in self._runtime.items():
                runtime.resolved = resolved.get(adapter_id)
                if runtime.resolved:
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
        if runtime.resolved is None:
            raise AdapterUnavailable(f"adapter {runtime.config.id} disappeared")
        async with runtime.connection_slots:
            runtime.stats.assignments += 1
            yield runtime.resolved

    @asynccontextmanager
    async def cold_connection(self, runtime: AdapterRuntime) -> AsyncIterator[None]:
        async with runtime.cold_slots:
            runtime.stats.connection_attempts += 1
            yield

    def record_success(self, runtime: AdapterRuntime) -> None:
        runtime.stats.connection_successes += 1
        runtime.stats.last_error = None

    def record_failure(self, runtime: AdapterRuntime, error: BaseException) -> None:
        runtime.stats.failures += 1
        runtime.stats.last_error = f"{type(error).__name__}: {error}"
        # Missing-device/BlueZ errors are re-resolved before the next command.
        text = str(error).lower()
        if any(word in text for word in ("not available", "no such", "org.bluez", "adapter")):
            runtime.resolved = None
