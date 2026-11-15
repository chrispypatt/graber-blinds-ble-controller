"""Independent state machine for one Graber motor."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable

from .adapter_manager import AdapterManager, AdapterRuntime
from .ble_transport import BleConnection, BleTransport, DiscoveredDevice
from .config import BlindConfig, RetryConfig
from .protocol import (
    AUTH_CHARACTERISTIC, BATTERY_CHARACTERISTIC, BOTTOM_POSITION_CHARACTERISTIC,
    EXECUTE_CHARACTERISTIC, TOP_POSITION_CHARACTERISTIC, CommandKind, Rail, auth_payload, battery_from_read,
    build_command, parse_manufacturer_data, position_from_notification,
)


class Outcome(str, Enum):
    RECEIVED = "received"
    CONNECTED = "connected"
    WRITE_ACKNOWLEDGED = "write_acknowledged"
    MOVEMENT_OBSERVED = "movement_observed"
    COMPLETED = "completed"
    COMPLETION_UNCONFIRMED = "completion_unconfirmed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class BlindCommand:
    id: str
    kind: CommandKind
    rail: Rail | None = None
    position: int | None = None  # raw motor coordinate; boundaries convert HA values
    received_at: float = field(default_factory=time.time)


@dataclass
class PositionState:
    bottom: int | None = None
    top: int | None = None
    battery: int | None = None
    rssi: int | None = None
    updated_at: float | None = None


@dataclass
class CommandResult:
    command_id: str
    blind_id: str
    outcome: Outcome
    adapter_id: str | None = None
    attempts: int = 0
    received_at: float = 0.0
    discovery_ms: float | None = None
    connect_ms: float | None = None
    auth_ms: float | None = None
    command_ack_ms: float | None = None
    feedback_ms: float | None = None
    movement_start_ms: float | None = None
    finished_at: float = field(default_factory=time.time)
    movement_observed: bool = False
    error: str | None = None


Telemetry = Callable[[str, dict[str, object]], None]
StateListener = Callable[[str, PositionState], None]


class CommandFailure(RuntimeError):
    pass


class CompletionUnconfirmed(CommandFailure):
    """Final write was acknowledged, so automatic replay is unsafe."""


@dataclass
class _Session:
    connection: BleConnection
    adapter: AdapterRuntime
    last_used: float
    connected_at: float = field(default_factory=time.monotonic)
    close_task: asyncio.Task[None] | None = None


class BlindController:
    def __init__(
        self, config: BlindConfig, adapters: AdapterManager, transport: BleTransport,
        retry: RetryConfig, warm_connection_s: float = 30.0,
        telemetry: Telemetry | None = None, state_listener: StateListener | None = None,
    ) -> None:
        self.config, self._adapters, self._transport = config, adapters, transport
        self._retry, self._warm_s = retry, warm_connection_s
        self._telemetry = telemetry or (lambda _event, _fields: None)
        self._state_listener = state_listener or (lambda _blind_id, _state: None)
        self.state = PositionState()
        self._lock = asyncio.Lock()
        self._session: _Session | None = None
        self._sample_counts = {Rail.BOTTOM: 0, Rail.TOP: 0}
        self._sample_event = asyncio.Event()
        self._last_change = time.monotonic()
        self._last_battery_read = 0.0
        self._closed = False

    def _emit(self, event: str, **fields: object) -> None:
        safe = {"blind_id": self.config.id, **fields}
        self._telemetry(event, safe)

    @staticmethod
    def _result_fields(result: CommandResult) -> dict[str, object]:
        fields = asdict(result)
        fields["outcome"] = result.outcome.value
        fields["total_latency_ms"] = max(0.0, (result.finished_at - result.received_at) * 1000)
        return fields

    def observe_advertisement(self, device: DiscoveredDevice) -> None:
        parsed = parse_manufacturer_data(device.manufacturer_data, tdbu=self.config.dual_rail)
        self.state.rssi = device.rssi
        if parsed:
            self._update_position(Rail.BOTTOM, parsed.bottom)
            if parsed.top is not None:
                self._update_position(Rail.TOP, parsed.top)

    def _notification(self, rail: Rail) -> Callable[[bytes], None]:
        def callback(value: bytes) -> None:
            try:
                self._update_position(rail, position_from_notification(value))
            except ValueError as exc:
                self._emit("invalid_notification", rail=rail.value, error=str(exc))
        return callback

    def _update_position(self, rail: Rail, value: int) -> None:
        previous = self.state.bottom if rail is Rail.BOTTOM else self.state.top
        if previous is not None and previous != value:
            self._last_change = time.monotonic()
        if rail is Rail.BOTTOM:
            self.state.bottom = value
        else:
            self.state.top = value
        self.state.updated_at = time.time()
        self._sample_counts[rail] += 1
        self._sample_event.set()
        self._state_listener(self.config.id, self.state)

    async def execute(self, command: BlindCommand) -> CommandResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("blind controller is shutting down")
            if self._session and not self._session.connection.connected:
                await self._drop_session()
            self._emit("command_received", command_id=command.id, kind=command.kind.value,
                       received_at=command.received_at)
            result = CommandResult(command.id, self.config.id, Outcome.RECEIVED, received_at=command.received_at)
            last_error: BaseException | None = None
            # A warm session stays bound to its adapter; otherwise preferred/fallback order applies.
            candidates = [self._session.adapter] if self._session else await self._adapters.candidates(self.config)
            for attempt in range(1, self._retry.attempts + 1):
                runtime = candidates[(attempt - 1) % len(candidates)]
                result.attempts = attempt
                result.adapter_id = runtime.config.id
                effectful_write_acked = False
                connection_ready = False
                try:
                    cold = not (self._session and self._session.connection.connected)
                    if cold:
                        self._emit("connection_attempt_started", command_id=command.id, attempt=attempt,
                                   adapter_id=runtime.config.id)
                    session, timings = await self._ensure_session(runtime, command.id, attempt)
                    connection_ready = True
                    result.discovery_ms = timings.get("discovery_ms", result.discovery_ms)
                    result.connect_ms = timings.get("connect_ms", result.connect_ms)
                    result.auth_ms = timings.get("auth_ms", result.auth_ms)
                    baseline = dict(self._sample_counts)
                    started = time.monotonic()
                    steps = build_command(command.kind, rail=command.rail, position=command.position,
                                          dual_rail=self.config.dual_rail,
                                          has_execute_characteristic=session.connection.has_characteristic(
                                              EXECUTE_CHARACTERISTIC
                                          ))
                    for index, step in enumerate(steps):
                        sent_at = time.monotonic()
                        await asyncio.wait_for(session.connection.write(step.characteristic, step.value),
                                               self._retry.gatt_timeout_s)
                        effectful_write_acked = effectful_write_acked or step.effectful
                        self._emit("gatt_write_acknowledged", command_id=command.id, step=index + 1,
                                   steps=len(steps), adapter_id=runtime.config.id)
                        if step.delay_after_s:
                            await asyncio.sleep(max(0.0, sent_at + step.delay_after_s - time.monotonic()))
                    result.command_ack_ms = (time.monotonic() - started) * 1000
                    result.outcome = Outcome.WRITE_ACKNOWLEDGED
                    feedback_start = time.monotonic()
                    movement, movement_start_ms = await self._await_feedback(
                        command, baseline, session.connection
                    )
                    result.feedback_ms = (time.monotonic() - feedback_start) * 1000
                    result.movement_observed = movement
                    result.movement_start_ms = movement_start_ms
                    result.outcome = Outcome.COMPLETED
                    result.finished_at = time.time()
                    session.last_used = time.monotonic()
                    await self._maybe_read_battery(session.connection)
                    self._schedule_close(session)
                    self._emit("command_completed", **self._result_fields(result))
                    return result
                except CompletionUnconfirmed as exc:
                    result.outcome = Outcome.COMPLETION_UNCONFIRMED
                    result.error = str(exc)
                    result.finished_at = time.time()
                    await self._drop_session()
                    self._emit("command_completion_unconfirmed", **self._result_fields(result))
                    return result
                except Exception as exc:
                    last_error = exc
                    self._adapters.record_failure(runtime, exc)
                    self._emit("command_attempt_failed", command_id=command.id, attempt=attempt,
                               adapter_id=runtime.config.id, error=f"{type(exc).__name__}: {exc}",
                               effectful_write_acked=effectful_write_acked)
                    if not connection_ready:
                        self._emit("connection_attempt_failed", command_id=command.id, attempt=attempt,
                                   adapter_id=runtime.config.id,
                                   error=f"{type(exc).__name__}: {exc}")
                    await self._drop_session()
                    if effectful_write_acked:
                        result.outcome = Outcome.COMPLETION_UNCONFIRMED
                        result.error = f"{type(exc).__name__}: {exc}"
                        result.finished_at = time.time()
                        self._emit("command_completion_unconfirmed", **self._result_fields(result))
                        return result
                    if attempt < self._retry.attempts:
                        delay = self._retry.backoff_s * (2 ** (attempt - 1)) + random.uniform(0, self._retry.jitter_s)
                        await asyncio.sleep(delay)
                        candidates = await self._adapters.candidates(self.config)
            result.outcome = Outcome.FAILED
            result.error = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown failure"
            result.finished_at = time.time()
            self._emit("command_failed", **self._result_fields(result))
            return result

    async def prepare(self) -> str:
        """Prepare and authenticate a connection without issuing a movement command."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("blind controller is shutting down")
            if self._session and not self._session.connection.connected:
                await self._drop_session()
            candidates = [self._session.adapter] if self._session else await self._adapters.candidates(self.config)
            last_error: BaseException | None = None
            for runtime in candidates:
                try:
                    session, _ = await self._ensure_session(runtime)
                    session.last_used = time.monotonic()
                    self._schedule_close(session)
                    return runtime.config.id
                except Exception as exc:
                    last_error = exc
                    self._adapters.record_failure(runtime, exc)
                    await self._drop_session()
            raise CommandFailure(f"preparation failed: {last_error}")

    async def _ensure_session(self, runtime: AdapterRuntime, command_id: str | None = None,
                              attempt: int | None = None) -> tuple[_Session, dict[str, float]]:
        if self._session and self._session.connection.connected:
            if self._session.close_task:
                self._session.close_task.cancel()
                self._session.close_task = None
            return self._session, {}
        if runtime.resolved is None:
            raise CommandFailure(f"adapter {runtime.config.id} is unavailable")
        await runtime.connection_slots.acquire()
        connection: BleConnection | None = None
        try:
            async with self._adapters.cold_connection(runtime):
                discover_started = time.monotonic()
                device = await self._transport.discover(self.config.address, runtime.resolved.device,
                                                        self._retry.connect_timeout_s)
                discovery_ms = (time.monotonic() - discover_started) * 1000
                self.observe_advertisement(device)
                connect_started = time.monotonic()
                connection = await self._transport.connect(device, runtime.resolved.device,
                                                            self._retry.connect_timeout_s)
                connect_ms = (time.monotonic() - connect_started) * 1000
                if command_id is not None:
                    self._emit("connection_attempt_succeeded", command_id=command_id, attempt=attempt,
                               adapter_id=runtime.config.id, discovery_ms=discovery_ms,
                               connect_ms=connect_ms)
            setup_started = time.monotonic()
            await asyncio.wait_for(
                connection.start_notify(BOTTOM_POSITION_CHARACTERISTIC, self._notification(Rail.BOTTOM)),
                self._retry.gatt_timeout_s,
            )
            if self.config.dual_rail:
                await asyncio.wait_for(
                    connection.start_notify(TOP_POSITION_CHARACTERISTIC, self._notification(Rail.TOP)),
                    self._retry.gatt_timeout_s,
                )
            auth_started = time.monotonic()
            await asyncio.wait_for(connection.write(AUTH_CHARACTERISTIC, auth_payload(self.config.access_code)),
                                   self._retry.gatt_timeout_s)
            auth_ms = (time.monotonic() - auth_started) * 1000
            gatt_setup_ms = (time.monotonic() - setup_started) * 1000
            self._session = _Session(connection, runtime, time.monotonic())
            self._adapters.record_success(runtime)
            self._emit("connection_ready", adapter_id=runtime.config.id, discovery_ms=discovery_ms,
                       connect_ms=connect_ms, auth_ms=auth_ms, gatt_setup_ms=gatt_setup_ms)
            return self._session, {"discovery_ms": discovery_ms, "connect_ms": connect_ms,
                                   "auth_ms": auth_ms, "gatt_setup_ms": gatt_setup_ms}
        except BaseException:
            if connection:
                try:
                    await connection.disconnect()
                except BaseException:
                    pass
            runtime.connection_slots.release()
            raise

    async def _await_feedback(self, command: BlindCommand, baseline: dict[Rail, int],
                              connection: BleConnection) -> tuple[bool, float | None]:
        required = [command.rail] if command.kind is CommandKind.SET_POSITION else [Rail.BOTTOM]
        if self.config.dual_rail and command.kind in {
            CommandKind.STOP, CommandKind.FAVORITE, CommandKind.FULL_OPEN,
            CommandKind.FULL_CLOSE, CommandKind.STACK_UP, CommandKind.STACK_DOWN,
        }:
            required = [Rail.BOTTOM, Rail.TOP]
        if command.kind is CommandKind.PRESET:
            if command.position != 5:
                raise CompletionUnconfirmed(
                    "preset write acknowledged; completion target is unknown for this index"
                )
            required = [Rail.BOTTOM, Rail.TOP]
        deadline = time.monotonic() + self._retry.feedback_timeout_s
        movement = False
        movement_start_ms: float | None = None
        initial = {Rail.BOTTOM: self.state.bottom, Rail.TOP: self.state.top}
        while time.monotonic() < deadline:
            fresh = all(self._sample_counts[rail] > baseline[rail] for rail in required if rail is not None)
            current = {Rail.BOTTOM: self.state.bottom, Rail.TOP: self.state.top}
            changed = any(initial[rail] is not None and current[rail] != initial[rail] for rail in required if rail)
            if changed and not movement:
                movement_start_ms = (time.time() - command.received_at) * 1000
                self._emit("movement_observed", command_id=command.id,
                           movement_start_ms=movement_start_ms)
            movement = movement or changed
            target_ok = True
            if command.kind is CommandKind.SET_POSITION and command.rail is not None:
                value = current[command.rail]
                target_ok = value is not None and command.position is not None and abs(value - command.position) <= 1
            elif command.kind is CommandKind.FULL_OPEN:
                target_ok = current[Rail.BOTTOM] == 100 and (not self.config.dual_rail or current[Rail.TOP] == 100)
            elif command.kind is CommandKind.FULL_CLOSE:
                target_ok = current[Rail.BOTTOM] == 0 and (not self.config.dual_rail or current[Rail.TOP] == 100)
            elif command.kind in {CommandKind.STOP, CommandKind.FAVORITE}:
                target_ok = time.monotonic() - self._last_change >= 2.0
                if command.kind is CommandKind.FAVORITE:
                    target_ok = target_ok and movement
            elif command.kind is CommandKind.PRESET:
                target_ok = current[Rail.BOTTOM] == 50 and current[Rail.TOP] == 100
            elif command.kind in {CommandKind.STACK_UP, CommandKind.STACK_DOWN}:
                target_ok = time.monotonic() - self._last_change >= 0.6
            if fresh and target_ok:
                return movement, movement_start_ms
            # Quiet notification streams get bounded explicit reads.
            read_updated = False
            for rail in required:
                if rail is None or self._sample_counts[rail] > baseline[rail]:
                    continue
                characteristic = BOTTOM_POSITION_CHARACTERISTIC if rail is Rail.BOTTOM else TOP_POSITION_CHARACTERISTIC
                try:
                    value = await asyncio.wait_for(connection.read(characteristic), self._retry.gatt_timeout_s)
                    self._update_position(rail, position_from_notification(value))
                    read_updated = True
                except Exception:
                    pass
            if read_updated:
                # Re-evaluate immediately; do not spend the remaining timeout
                # waiting for a notification after a successful explicit read.
                continue
            self._sample_event.clear()
            try:
                await asyncio.wait_for(self._sample_event.wait(), min(1.2, max(0.01, deadline - time.monotonic())))
            except TimeoutError:
                pass
        raise CompletionUnconfirmed("GATT writes acknowledged but fresh completion feedback timed out")

    async def read_battery(self) -> int | None:
        """Read once during an existing warm connection; never opens one solely here."""
        async with self._lock:
            if not self._session or not self._session.connection.connected:
                return None
            raw = await asyncio.wait_for(self._session.connection.read(BATTERY_CHARACTERISTIC),
                                         self._retry.gatt_timeout_s)
            value = battery_from_read(raw)
            self.state.battery = value
            self._state_listener(self.config.id, self.state)
            self._emit("battery_read", value=value)
            return value

    async def _maybe_read_battery(self, connection: BleConnection) -> None:
        now = time.time()
        if now - self._last_battery_read < self.config.battery_interval_s:
            return
        # Battery work is low priority and occurs only on an already-open link.
        # A failure never changes an otherwise completed movement result.
        self._last_battery_read = now
        try:
            raw = await asyncio.wait_for(connection.read(BATTERY_CHARACTERISTIC),
                                         self._retry.gatt_timeout_s)
            self.state.battery = battery_from_read(raw)
            self._state_listener(self.config.id, self.state)
            self._emit("battery_read", value=self.state.battery)
        except Exception as exc:
            self._emit("battery_read_failed", error=f"{type(exc).__name__}: {exc}")

    def _schedule_close(self, session: _Session) -> None:
        if session.close_task:
            session.close_task.cancel()
        async def later() -> None:
            try:
                await asyncio.sleep(self._warm_s)
                async with self._lock:
                    if self._session is session and time.monotonic() - session.last_used >= self._warm_s:
                        await self._drop_session()
            except asyncio.CancelledError:
                pass
        session.close_task = asyncio.create_task(later())

    async def _drop_session(self) -> None:
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
                       connection_duration_ms=(time.monotonic() - session.connected_at) * 1000)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._drop_session()
