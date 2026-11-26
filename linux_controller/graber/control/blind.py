"""Independent state machine for one Graber motor."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from graber.bluetooth.adapter_manager import AdapterManager, AdapterRuntime
from graber.bluetooth.ble_transport import BleConnection, BleTransport, DiscoveredDevice
from graber.config import BlindConfig, RetryConfig
from graber.bluetooth.protocol import (
    AUTH_CHARACTERISTIC, BATTERY_CHARACTERISTIC, BOTTOM_POSITION_CHARACTERISTIC,
    EXECUTE_CHARACTERISTIC, TOP_POSITION_CHARACTERISTIC, Rail, auth_payload, battery_from_read,
    build_command, parse_manufacturer_data, position_from_notification,
)

from .battery import BatteryHistory
from .commands import (
    BlindCommand, CommandFailure, CommandResult, CompletionUnconfirmed, Outcome, PositionState,
)
from .feedback import PositionFeedback


Telemetry = Callable[[str, dict[str, object]], None]
StateListener = Callable[[str, PositionState], None]


@dataclass
class _Attempt:
    connection_ready: bool = False
    effectful_write_acked: bool = False


@dataclass
class _Session:
    connection: BleConnection
    adapter: AdapterRuntime
    last_used: float
    connected_at: float = field(default_factory=time.monotonic)
    command_count: int = 0
    battery_read_attempted: bool = False
    close_task: asyncio.Task[None] | None = None


class BlindController:
    def __init__(
        self, config: BlindConfig, adapters: AdapterManager, transport: BleTransport,
        retry: RetryConfig, warm_connection_s: float = 30.0,
        telemetry: Telemetry | None = None, state_listener: StateListener | None = None,
        battery_history: BatteryHistory | None = None,
    ) -> None:
        self.config, self._adapters, self._transport = config, adapters, transport
        self._retry, self._warm_s = retry, warm_connection_s
        self._telemetry = telemetry or (lambda _event, _fields: None)
        self._state_listener = state_listener or (lambda _blind_id, _state: None)
        self.state = PositionState()
        self.battery_read_since_start = False
        self._battery_history = battery_history
        if battery_history is not None:
            battery_history.restore(config.address, self.state)
        self._lock = asyncio.Lock()
        self._session: _Session | None = None
        self._feedback = PositionFeedback(
            self.state, self.config, self._retry, self._state_listener, self._emit,
        )
        self._closed = False

    def _emit(self, event: str, **fields: object) -> None:
        safe = {"blind_id": self.config.id, **fields}
        self._telemetry(event, safe)

    @staticmethod
    def _error_category(error: BaseException) -> str:
        text = f"{type(error).__name__}: {error}".casefold()
        if isinstance(error, CompletionUnconfirmed):
            return "completion_unconfirmed"
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or "timed out" in text:
            return "timeout"
        if any(marker in text for marker in (
            "adapter not", "adapter is not", "controller unavailable",
            "no default controller", "org.bluez.error.notready",
        )):
            return "adapter_unavailable"
        if any(marker in text for marker in (
            "disconnected", "not connected", "connection abort", "unknownobject",
        )):
            return "disconnected"
        if "org.bluez" in text or "bluez" in text:
            return "bluez"
        if any(marker in text for marker in ("gatt", "characteristic", "services")):
            return "gatt"
        return "other"

    def observe_advertisement(self, device: DiscoveredDevice) -> None:
        parsed = parse_manufacturer_data(device.manufacturer_data, tdbu=self.config.dual_rail)
        self.state.rssi = device.rssi
        if parsed:
            self._feedback.update_position(Rail.BOTTOM, parsed.bottom, verified=False)
            if parsed.top is not None:
                self._feedback.update_position(Rail.TOP, parsed.top, verified=False)

    def _notification(self, rail: Rail) -> Callable[[bytes], None]:
        def callback(value: bytes) -> None:
            try:
                self._feedback.update_position(rail, position_from_notification(value))
            except ValueError as exc:
                self._emit("invalid_notification", rail=rail.value, error=str(exc))
        return callback

    async def execute(self, command: BlindCommand) -> CommandResult:
        async with self._lock:
            controller_queue_ms = max(0.0, (time.time() - command.received_at) * 1000)
            if self._closed:
                raise RuntimeError("blind controller is shutting down")
            if self._session and not self._session.connection.connected:
                await self._drop_session("stale_connection")
            self._emit("command_received", command_id=command.id, kind=command.kind.value,
                       received_at=command.received_at,
                       controller_queue_ms=controller_queue_ms)
            result = CommandResult(
                command.id, self.config.id, Outcome.RECEIVED,
                received_at=command.received_at,
                controller_queue_ms=controller_queue_ms,
            )
            last_error: BaseException | None = None
            # A warm session stays bound to its adapter; otherwise preferred/fallback order applies.
            candidates = [self._session.adapter] if self._session else await self._adapters.candidates(self.config)
            for attempt in range(1, self._retry.attempts + 1):
                runtime = candidates[(attempt - 1) % len(candidates)]
                result.attempts = attempt
                result.adapter_id = runtime.config.id
                progress = _Attempt()
                try:
                    await self._execute_attempt(command, runtime, result, progress)
                    return result
                except CompletionUnconfirmed as exc:
                    result.outcome = Outcome.COMPLETION_UNCONFIRMED
                    result.error_category = self._error_category(exc)
                    result.error = str(exc)
                    result.finished_at = time.time()
                    await self._drop_session("completion_unconfirmed")
                    self._emit("command_completion_unconfirmed", **result.telemetry_fields())
                    return result
                except Exception as exc:
                    last_error = exc
                    error_category = self._error_category(exc)
                    await self._record_attempt_failure(runtime, result, progress, exc, error_category)
                    if progress.effectful_write_acked:
                        result.outcome = Outcome.COMPLETION_UNCONFIRMED
                        result.error_category = error_category
                        result.error = f"{type(exc).__name__}: {exc}"
                        result.finished_at = time.time()
                        self._emit("command_completion_unconfirmed", **result.telemetry_fields())
                        return result
                    if attempt < self._retry.attempts:
                        await self._wait_before_retry(result, error_category)
                        candidates = await self._adapters.candidates(self.config)
            result.outcome = Outcome.FAILED
            if last_error is not None:
                result.error_category = self._error_category(last_error)
            result.error = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown failure"
            result.finished_at = time.time()
            self._emit("command_failed", **result.telemetry_fields())
            return result

    async def _execute_attempt(
        self, command: BlindCommand, runtime: AdapterRuntime, result: CommandResult, progress: _Attempt,
    ) -> None:
        session, timings = await self._ensure_session(runtime, command.id, result.attempts)
        progress.connection_ready = True
        result.record_connection_timings(timings)
        session.command_count += 1
        baseline = self._feedback.baseline()
        result.command_ack_ms = await self._write_command(command, session, progress)
        result.outcome = Outcome.WRITE_ACKNOWLEDGED
        feedback_start = time.monotonic()
        try:
            (
                result.movement_observed, result.movement_start_ms, result.feedback_first_sample_ms,
            ) = await self._feedback.wait_for_completion(command, baseline, session.connection)
        finally:
            result.feedback_ms = (time.monotonic() - feedback_start) * 1000
        result.outcome = Outcome.COMPLETED
        result.finished_at = time.time()
        session.last_used = time.monotonic()
        self._emit(
            "feedback_completed", command_id=command.id,
            adapter_id=runtime.config.id, feedback_ms=result.feedback_ms,
            feedback_first_sample_ms=result.feedback_first_sample_ms,
            movement_observed=result.movement_observed,
        )
        self._emit("command_completed", **result.telemetry_fields())
        await self._maybe_read_battery(session, command.id)
        self._schedule_close(session)

    async def _write_command(self, command: BlindCommand, session: _Session, progress: _Attempt) -> float:
        started = time.monotonic()
        connection = session.connection
        steps = build_command(
            command.kind, rail=command.rail, position=command.position, dual_rail=self.config.dual_rail,
            has_execute_characteristic=connection.has_characteristic(EXECUTE_CHARACTERISTIC),
        )
        for index, step in enumerate(steps, 1):
            sent_at = time.monotonic()
            await asyncio.wait_for(connection.write(step.characteristic, step.value), self._retry.gatt_timeout_s)
            # Record the no-replay boundary before telemetry, inter-write delays, or the next write.
            if step.effectful:
                progress.effectful_write_acked = True
            self._emit(
                "gatt_write_acknowledged", command_id=command.id, step=index, steps=len(steps),
                adapter_id=session.adapter.config.id, write_ack_ms=(time.monotonic() - sent_at) * 1000,
                effectful=step.effectful, internal=command.internal,
            )
            if step.delay_after_s:
                await asyncio.sleep(max(0.0, sent_at + step.delay_after_s - time.monotonic()))
        return (time.monotonic() - started) * 1000

    async def _record_attempt_failure(
        self, runtime: AdapterRuntime, result: CommandResult, progress: _Attempt,
        exc: Exception, error_category: str,
    ) -> None:
        self._adapters.record_failure(runtime, exc)
        fields = {
            "command_id": result.command_id, "attempt": result.attempts,
            "adapter_id": result.adapter_id, "error": f"{type(exc).__name__}: {exc}",
            "error_category": error_category,
        }
        self._emit("command_attempt_failed", **fields, effectful_write_acked=progress.effectful_write_acked)
        if not progress.connection_ready:
            self._emit("connection_attempt_failed", **fields)
        await self._drop_session("command_attempt_failed")

    async def _wait_before_retry(self, result: CommandResult, error_category: str) -> None:
        attempt = result.attempts
        delay = self._retry.backoff_s * 2 ** (attempt - 1) + random.uniform(0, self._retry.jitter_s)
        self._emit(
            "command_retry_scheduled", command_id=result.command_id, attempt=attempt, next_attempt=attempt + 1,
            adapter_id=result.adapter_id, backoff_ms=delay * 1000, error_category=error_category,
        )
        await asyncio.sleep(delay)

    async def prepare(self, *, command_id: str | None = None) -> str:
        """Prepare and authenticate a connection without issuing a movement command."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("blind controller is shutting down")
            if self._session and not self._session.connection.connected:
                await self._drop_session("stale_connection")
            candidates = [self._session.adapter] if self._session else await self._adapters.candidates(self.config)
            last_error: BaseException | None = None
            for attempt, runtime in enumerate(candidates, 1):
                prepare_started = time.monotonic()
                try:
                    session, timings = await self._ensure_session(
                        runtime, command_id=command_id, attempt=attempt, operation="group_prepare"
                    )
                    session.last_used = time.monotonic()
                    self._schedule_close(session)
                    self._emit(
                        "prepared", command_id=command_id, attempt=attempt,
                        adapter_id=session.adapter.config.id,
                        cold_queue_ms=timings.get("cold_queue_ms"),
                        discovery_ms=timings.get("discovery_ms"),
                        connect_ms=timings.get("connect_ms"),
                        notify_setup_ms=timings.get("notify_setup_ms"),
                        auth_ms=timings.get("auth_ms"),
                        gatt_setup_ms=timings.get("gatt_setup_ms"),
                        warm_connection=bool(timings.get("warm_connection", False)),
                        total_latency_ms=(time.monotonic() - prepare_started) * 1000,
                    )
                    return runtime.config.id
                except Exception as exc:
                    last_error = exc
                    self._adapters.record_failure(runtime, exc)
                    self._emit(
                        "prepare_failed", command_id=command_id, attempt=attempt,
                        adapter_id=runtime.config.id,
                        total_latency_ms=(time.monotonic() - prepare_started) * 1000,
                        error_category=self._error_category(exc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await self._drop_session("prepare_failed")
            raise CommandFailure(f"preparation failed: {last_error}")

    async def refresh_status(self) -> PositionState:
        """Read current rails and battery through one bounded, non-movement session."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("blind controller is shutting down")
            if self._session and not self._session.connection.connected:
                await self._drop_session("stale_connection")
            candidates = [self._session.adapter] if self._session else await self._adapters.candidates(self.config)
            last_error: BaseException | None = None
            for runtime in candidates:
                refresh_started = time.monotonic()
                session_to_keep = self._session
                disconnect_reason = "status_refresh_finished"
                try:
                    session, timings = await self._ensure_session(
                        runtime, operation="status_refresh"
                    )
                    rail_read_started = time.monotonic()
                    bottom = await asyncio.wait_for(
                        session.connection.read(BOTTOM_POSITION_CHARACTERISTIC),
                        self._retry.gatt_timeout_s,
                    )
                    self._feedback.update_position(Rail.BOTTOM, position_from_notification(bottom))
                    if self.config.dual_rail:
                        top = await asyncio.wait_for(
                            session.connection.read(TOP_POSITION_CHARACTERISTIC),
                            self._retry.gatt_timeout_s,
                        )
                        self._feedback.update_position(Rail.TOP, position_from_notification(top))
                    rail_read_ms = (time.monotonic() - rail_read_started) * 1000
                    battery_read_ms: float | None = None
                    try:
                        battery_read_ms = await self._read_session_battery(session, "status_refresh")
                    except Exception as exc:
                        self._emit("battery_read_failed", source="status_refresh",
                                   error_category=self._error_category(exc),
                                   error=f"{type(exc).__name__}: {exc}")
                    self._emit("status_refreshed", adapter_id=runtime.config.id,
                               battery_available=self.state.battery is not None,
                               cold_queue_ms=timings.get("cold_queue_ms"),
                               discovery_ms=timings.get("discovery_ms"),
                               connect_ms=timings.get("connect_ms"),
                               notify_setup_ms=timings.get("notify_setup_ms"),
                               auth_ms=timings.get("auth_ms"),
                               gatt_setup_ms=timings.get("gatt_setup_ms"),
                               warm_connection=bool(timings.get("warm_connection", False)),
                               rail_read_ms=rail_read_ms,
                               battery_read_ms=battery_read_ms,
                               total_latency_ms=(time.monotonic() - refresh_started) * 1000)
                    return self.state
                except Exception as exc:
                    last_error = exc
                    self._adapters.record_failure(runtime, exc)
                    self._emit(
                        "status_refresh_failed", adapter_id=runtime.config.id,
                        total_latency_ms=(time.monotonic() - refresh_started) * 1000,
                        error_category=self._error_category(exc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    session_to_keep = None
                    disconnect_reason = "status_refresh_failed"
                finally:
                    if self._session is not None and self._session is not session_to_keep:
                        cleanup = asyncio.create_task(self._drop_session(disconnect_reason))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # Keep the blind locked until the in-flight disconnect finishes.
                            await cleanup
                            raise
            raise CommandFailure(f"status refresh failed: {last_error}")

    async def _ensure_session(self, runtime: AdapterRuntime, command_id: str | None = None,
                              attempt: int | None = None,
                              operation: str = "command") -> tuple[_Session, dict[str, float]]:
        if self._session and self._session.connection.connected:
            # A status read borrows a warm link without extending its idle hold.
            if self._session.close_task and operation != "status_refresh":
                self._session.close_task.cancel()
                self._session.close_task = None
            connection_age_ms = (time.monotonic() - self._session.connected_at) * 1000
            self._emit(
                "connection_reused", command_id=command_id, attempt=attempt,
                adapter_id=self._session.adapter.config.id,
                connection_age_ms=connection_age_ms, operation=operation,
            )
            return self._session, {
                "warm_connection": 1.0,
                "connection_age_ms": connection_age_ms,
            }
        context = {
            "command_id": command_id, "attempt": attempt, "adapter_id": runtime.config.id,
            "operation": operation, "max_connections": runtime.config.max_connections,
            "max_cold_connections": runtime.config.max_cold_connections,
            "cold_start_spacing_s": runtime.config.cold_start_spacing_s,
        }
        phase = "active_slot"
        phase_started = time.monotonic()
        if command_id is not None:
            self._emit("connection_attempt_started", **context)
        active_slot = False
        connection: BleConnection | None = None
        timings: dict[str, float] = {}
        bluez: dict[str, object] = {}
        try:
            if runtime.resolved is None:
                raise CommandFailure(f"adapter {runtime.config.id} is unavailable")
            await runtime.connection_slots.acquire()
            active_slot = True
            timings["active_queue_ms"] = (time.monotonic() - phase_started) * 1000
            phase = "cold_slot"
            phase_started = time.monotonic()
            async with self._adapters.cold_connection(runtime) as cold_queue_ms:
                timings["cold_queue_ms"] = cold_queue_ms
                if command_id is not None:
                    self._emit("cold_connection_slot_acquired", **context, **timings)
                phase = "discovery"
                phase_started = time.monotonic()
                device = await self._transport.discover(self.config.address, runtime.resolved.device,
                                                        self._retry.connect_timeout_s)
                timings["discovery_ms"] = (time.monotonic() - phase_started) * 1000
                self.observe_advertisement(device)
                phase = "connect"
                phase_started = time.monotonic()
                connection = await self._transport.connect(device, runtime.resolved.device,
                                                            self._retry.connect_timeout_s)
                timings["connect_ms"] = (time.monotonic() - phase_started) * 1000
                bluez = getattr(connection, "connection_phase_metrics", {}) or {}
                if command_id is not None:
                    self._emit(
                        "connection_attempt_succeeded", **context, **timings, **bluez,
                        advertisement_rssi=device.rssi,
                    )
            setup_started = time.monotonic()
            notify_started = setup_started
            phase = "notify_bottom"
            phase_started = notify_started
            await asyncio.wait_for(
                connection.start_notify(BOTTOM_POSITION_CHARACTERISTIC, self._notification(Rail.BOTTOM)),
                self._retry.gatt_timeout_s,
            )
            if self.config.dual_rail:
                phase = "notify_top"
                phase_started = time.monotonic()
                await asyncio.wait_for(
                    connection.start_notify(TOP_POSITION_CHARACTERISTIC, self._notification(Rail.TOP)),
                    self._retry.gatt_timeout_s,
                )
            timings["notify_setup_ms"] = (time.monotonic() - notify_started) * 1000
            phase = "auth"
            phase_started = time.monotonic()
            await asyncio.wait_for(connection.write(AUTH_CHARACTERISTIC, auth_payload(self.config.access_code)),
                                   self._retry.gatt_timeout_s)
            timings["auth_ms"] = (time.monotonic() - phase_started) * 1000
            timings["gatt_setup_ms"] = (time.monotonic() - setup_started) * 1000
            self._session = _Session(connection, runtime, time.monotonic())
            self._adapters.record_success(runtime)
            self._emit(
                "connection_ready", **context, **timings, **bluez,
                advertisement_rssi=device.rssi, warm_connection=False,
            )
            return self._session, {**timings, "warm_connection": 0.0, "connection_age_ms": 0.0}
        except BaseException as exc:
            phase_elapsed_ms = (time.monotonic() - phase_started) * 1000
            if connection:
                try:
                    await connection.disconnect()
                except BaseException:
                    pass
            if active_slot:
                runtime.connection_slots.release()
            self._emit(
                "connection_phase_failed", **context, **timings,
                **(getattr(exc, "connection_phase_metrics", None) or bluez),
                phase=phase, phase_elapsed_ms=phase_elapsed_ms,
                error_category="cancelled" if isinstance(exc, asyncio.CancelledError) else self._error_category(exc),
            )
            raise

    async def _read_session_battery(self, session: _Session, source: str, **context: object) -> float:
        started = time.monotonic()
        raw = await asyncio.wait_for(session.connection.read(BATTERY_CHARACTERISTIC),
                                     self._retry.gatt_timeout_s)
        value = battery_from_read(raw)
        duration_ms = (time.monotonic() - started) * 1000
        session.battery_read_attempted = True
        self.battery_read_since_start = True
        self.state.observe_battery(value, time.time())
        self._emit(
            "battery_read", value=value, accepted_value=self.state.battery,
            battery_status=self.state.battery_status, source=source,
            battery_read_ms=duration_ms, **context,
        )
        if self._battery_history is not None:
            self._battery_history.record(self.config.address, self.state)
        self._state_listener(self.config.id, self.state)
        return duration_ms

    async def read_battery(self) -> int | None:
        """Read an existing warm connection and return the accepted percentage."""
        async with self._lock:
            if not self._session or not self._session.connection.connected:
                return None
            await self._read_session_battery(self._session, "warm_connection")
            return self.state.battery

    async def _maybe_read_battery(self, session: _Session, command_id: str) -> None:
        if session.battery_read_attempted:
            return
        # Battery work is low priority and occurs only on an already-open link.
        # A failure never changes an otherwise completed movement result.
        session.battery_read_attempted = True
        try:
            await self._read_session_battery(session, "post_command", command_id=command_id)
        except Exception as exc:
            self._emit(
                "battery_read_failed", source="post_command", command_id=command_id,
                error_category=self._error_category(exc),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _schedule_close(self, session: _Session) -> None:
        if session.close_task:
            session.close_task.cancel()
        async def later() -> None:
            try:
                await asyncio.sleep(self._warm_s)
                async with self._lock:
                    if self._session is session and time.monotonic() - session.last_used >= self._warm_s:
                        await self._drop_session("warm_timeout")
            except asyncio.CancelledError:
                pass
        session.close_task = asyncio.create_task(later())

    async def _drop_session(self, reason: str = "cleanup") -> None:
        session, self._session = self._session, None
        if not session:
            return
        if session.close_task and session.close_task is not asyncio.current_task():
            session.close_task.cancel()
        try:
            await session.connection.disconnect()
        except Exception as exc:
            # Disconnect is cleanup. BlueZ may already have torn down the D-Bus
            # connection; that must not replace a completed command result.
            self._emit("disconnect_cleanup_failed", adapter_id=session.adapter.config.id,
                       error=f"{type(exc).__name__}: {exc}")
        finally:
            session.adapter.connection_slots.release()
            self._emit("disconnected", adapter_id=session.adapter.config.id,
                       reason=reason, command_count=session.command_count,
                       connection_duration_ms=(time.monotonic() - session.connected_at) * 1000)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._drop_session("shutdown")
        if self._battery_history is not None:
            await self._battery_history.flush()
