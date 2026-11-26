"""Best-effort and bounded synchronized group operations."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping

from .blind import BlindCommand, CommandResult, Outcome
from .config import GroupConfig
from .dispatcher import BlindDispatcher
from .protocol import CommandKind, Rail, ha_to_raw_position, raw_to_ha_position


@dataclass
class GroupResult:
    command_id: str
    group_id: str
    results: dict[str, CommandResult]
    preparation_errors: dict[str, str]

    @property
    def successful(self) -> bool:
        return bool(self.results) and not self.preparation_errors and all(
            item.outcome in {Outcome.COMPLETED, Outcome.DUPLICATE}
            for item in self.results.values()
        )


def parse_action(action: str, position: int | None = None) -> tuple[CommandKind, Rail | None, int | None]:
    aliases = {
        "open": CommandKind.FULL_OPEN, "close": CommandKind.FULL_CLOSE,
        "stop": CommandKind.STOP, "favorite": CommandKind.FAVORITE,
        "stack_up": CommandKind.STACK_UP, "stack_down": CommandKind.STACK_DOWN,
    }
    if action in aliases:
        return aliases[action], None, None
    if action in {"open_bottom", "close_bottom"}:
        value = 100 if action == "open_bottom" else 0
        return CommandKind.SET_POSITION, Rail.BOTTOM, value
    if action in {"open_top", "close_top"}:
        ha_position = 100 if action == "open_top" else 0
        return CommandKind.SET_POSITION, Rail.TOP, ha_to_raw_position(Rail.TOP, ha_position)
    if action in {"set_bottom", "set_top", "set_position", "set_bottom_position", "set_top_position"}:
        if position is None or not 0 <= int(position) <= 100:
            raise ValueError("position must be from 0 through 100")
        rail = Rail.TOP if action in {"set_top", "set_top_position"} else Rail.BOTTOM
        return CommandKind.SET_POSITION, rail, ha_to_raw_position(rail, int(position))
    if action.startswith("preset_"):
        return CommandKind.PRESET, None, int(action.removeprefix("preset_"))
    raise ValueError(f"unsupported action {action!r}")


class Controller:
    """Facade used by MQTT, web, and main without exposing BLE internals."""

    def __init__(self, dispatchers: dict[str, BlindDispatcher], groups: tuple[GroupConfig, ...],
                 adapter_health_provider: Callable[[], Mapping[str, object]] | None = None,
                 telemetry: Callable[[str, dict[str, object]], None] | None = None):
        self.dispatchers = dispatchers
        self.groups = {item.id: item for item in groups}
        self._diagnostics = {
            blind_id: {
                "last_command_status": "idle",
                "active_command_id": None,
                "connection_failures": 0,
                "retry_count": 0,
            }
            for blind_id in dispatchers
        }
        self._result_listener: Callable[[str, dict[str, object]], None] | None = None
        self._adapter_health_provider = adapter_health_provider or (lambda: {})
        self._telemetry = telemetry or (lambda _event, _fields: None)
        for dispatcher in self.dispatchers.values():
            setter = getattr(dispatcher, "set_lifecycle_listener", None)
            if setter is not None:
                setter(self.record_lifecycle)

    def set_result_listener(self, listener: Callable[[str, dict[str, object]], None]) -> None:
        self._result_listener = listener

    def diagnostics_for(self, blind_id: str) -> dict[str, object]:
        return dict(self._diagnostics[blind_id])

    def record_lifecycle(self, blind_id: str, status: str, command_id: str) -> None:
        """Publish non-terminal command progress without changing retry semantics."""
        diagnostic = self._diagnostics[blind_id]
        diagnostic["last_command_status"] = status
        diagnostic["active_command_id"] = command_id
        if self._result_listener:
            self._result_listener(blind_id, dict(diagnostic))

    async def refresh_status(self, target_id: str):
        if target_id in self.dispatchers:
            state = await self.dispatchers[target_id].blind.refresh_status()
        elif target_id in self.groups:
            raise ValueError("status refresh is available only for individual blinds")
        else:
            raise KeyError(f"unknown blind {target_id}")
        return {
            "bottom": state.bottom,
            "top": (raw_to_ha_position(Rail.TOP, state.top) if state.top is not None else None),
            **state.battery_fields(),
            "rssi": state.rssi,
            "updated_at": state.updated_at,
        }

    def _record_result(self, result: CommandResult) -> None:
        diagnostic = self._diagnostics[result.blind_id]
        # A STOP can supersede an active command and become the new active
        # command before the cancelled command's waiter records its result.
        # Keep that newer lifecycle visible instead of restoring stale state.
        active_command_id = diagnostic["active_command_id"]
        if active_command_id is None or active_command_id == result.command_id:
            diagnostic["last_command_status"] = result.outcome.value
            diagnostic["active_command_id"] = None
        diagnostic["retry_count"] = int(diagnostic["retry_count"]) + max(0, result.attempts - 1)
        superseded = bool(result.error and result.error.startswith("superseded by stop command"))
        if result.outcome in {Outcome.FAILED, Outcome.COMPLETION_UNCONFIRMED} and not superseded:
            diagnostic["connection_failures"] = int(diagnostic["connection_failures"]) + (
                1 if result.connect_ms is None else 0
            )
        if self._result_listener:
            self._result_listener(result.blind_id, dict(diagnostic))

    async def submit_command(self, target_id: str, action: str, position: int | None = None,
                             command_id: str | None = None, source: str = "internal",
                             target_type: str | None = None) -> CommandResult | GroupResult:
        if target_type not in {None, "blind", "group"}:
            raise ValueError(f"invalid target type {target_type!r}")
        command_id = command_id or uuid.uuid4().hex
        kind, rail, raw_position = parse_action(action, position)
        use_blind = target_type == "blind" or (
            target_type is None and target_id in self.dispatchers
        )
        if use_blind:
            if target_id not in self.dispatchers:
                raise KeyError(f"unknown blind {target_id}")
            if (
                self._requires_tdbu(kind, rail)
                and not self.dispatchers[target_id].blind.config.dual_rail
            ):
                raise ValueError(f"{action} requires a TDBU blind")
            result = await self.dispatchers[target_id].submit(
                BlindCommand(command_id, kind, rail, raw_position, time.time())
            )
            self._record_result(result)
            return result
        if target_id not in self.groups:
            if target_type == "group":
                raise KeyError(f"unknown group {target_id}")
            raise KeyError(f"unknown blind/group {target_id}")
        members = [
            self.dispatchers[item]
            for item in self.groups[target_id].members
            if item in self.dispatchers
        ]
        if self._requires_tdbu(kind, rail) and any(
            not item.blind.config.dual_rail for item in members
        ):
            raise ValueError(f"{action} requires an all-TDBU group")
        result = await self._group(
            self.groups[target_id], command_id, kind, rail, raw_position, source
        )
        for member_result in result.results.values():
            self._record_result(member_result)
        return result

    @staticmethod
    def _requires_tdbu(kind: CommandKind, rail: Rail | None) -> bool:
        return rail is Rail.TOP or kind in {
            CommandKind.PRESET,
            CommandKind.STACK_UP,
            CommandKind.STACK_DOWN,
        }

    async def _group(self, group: GroupConfig, command_id: str, kind: CommandKind,
                     rail: Rail | None, position: int | None, source: str) -> GroupResult:
        started = time.monotonic()
        members = [item for item in group.members if item in self.dispatchers]
        self._telemetry("group_command_received", {
            "command_id": command_id, "group_id": group.id, "source": source,
            "mode": group.mode, "kind": kind.value, "member_count": len(members),
        })
        preparation_errors: dict[str, str] = {}
        ready = list(members)
        early: dict[str, asyncio.Future[CommandResult]] = {}
        prepared_generations: dict[str, int] = {}

        def member_command(blind_id: str) -> BlindCommand:
            return BlindCommand(f"{command_id}:{blind_id}", kind, rail, position, time.time())

        # STOP is an interrupt, not a synchronized movement. Preparing every
        # member first would delay it behind the operations it must supersede.
        if group.mode == "synchronized" and kind is not CommandKind.STOP:
            deferred_members: list[str] = []
            member_stops: dict[str, asyncio.Future[CommandResult]] = {}
            release_deferred = False
            try:
                prepare_started = time.monotonic()
                for blind_id in members:
                    admission = await self.dispatchers[blind_id].admit_group_member(
                        member_command(blind_id),
                    )
                    if admission.deferred:
                        deferred_members.append(blind_id)
                    if admission.stop is not None:
                        member_stops[blind_id] = admission.stop
                    if admission.result is not None:
                        early[blind_id] = admission.result
                        self._telemetry("group_member_dispatched", {
                            "command_id": command_id, "group_id": group.id, "blind_id": blind_id,
                            "dispatch_offset_ms": (time.monotonic() - started) * 1000,
                            "before_preparation": True,
                        })
                    else:
                        prepared_generations[blind_id] = admission.generation
                waiting = [item for item in members if item not in early]

                async def prepare(blind_id: str) -> None:
                    try:
                        await self.dispatchers[blind_id].blind.prepare(
                            command_id=f"{command_id}:{blind_id}",
                        )
                    except Exception as exc:
                        preparation_errors[blind_id] = f"{type(exc).__name__}: {exc}"

                async def confirm_stop(blind_id: str, future: asyncio.Future[CommandResult]) -> None:
                    result = await asyncio.shield(future)
                    if result.outcome is not Outcome.COMPLETED:
                        preparation_errors[blind_id] = f"STOP was not confirmed: {result.error or result.outcome.value}"

                try:
                    async with asyncio.timeout(group.prepare_timeout_s):
                        await asyncio.gather(
                            *(prepare(item) for item in waiting),
                            *(confirm_stop(item, future) for item, future in member_stops.items()),
                        )
                except TimeoutError:
                    for item in waiting:
                        session = self.dispatchers[item].blind._session
                        if (not session or not session.connection.connected) and item not in preparation_errors:
                            preparation_errors[item] = "preparation deadline exceeded"
                    for item, future in member_stops.items():
                        if not future.done() or future.result().outcome is not Outcome.COMPLETED:
                            preparation_errors.setdefault(item, "STOP confirmation deadline exceeded")
                ready = [item for item in waiting if item not in preparation_errors]
                self._telemetry("group_preparation_completed", {
                    "command_id": command_id, "group_id": group.id, "mode": group.mode,
                    "prepare_ms": (time.monotonic() - prepare_started) * 1000,
                    "ready_count": len(ready),
                    "preparation_failure_count": len(preparation_errors),
                    "failed_members": sorted(preparation_errors),
                })
                release_deferred = not preparation_errors or group.partial_failure != "abort"
                if not release_deferred:
                    ready = []
            finally:
                for blind_id in deferred_members:
                    await self.dispatchers[blind_id].resolve_group_member(
                        f"{command_id}:{blind_id}",
                        release=release_deferred and blind_id not in preparation_errors,
                    )
        release_started = time.monotonic()
        dispatch_times: dict[str, float] = {}
        self._telemetry("group_commands_released", {
            "command_id": command_id, "group_id": group.id, "mode": group.mode,
            "ready_count": len(ready), "elapsed_ms": (release_started - started) * 1000,
        })
        async def run(blind_id: str) -> tuple[str, CommandResult]:
            if blind_id in early:
                return blind_id, await asyncio.shield(early[blind_id])
            dispatched_at = time.monotonic()
            dispatch_times[blind_id] = dispatched_at
            self._telemetry("group_member_dispatched", {
                "command_id": command_id, "group_id": group.id, "blind_id": blind_id,
                "dispatch_offset_ms": (dispatched_at - release_started) * 1000,
            })
            dispatcher = self.dispatchers[blind_id]
            command = member_command(blind_id)
            if blind_id in prepared_generations:
                return blind_id, await dispatcher.submit_prepared(
                    command, prepared_generations[blind_id],
                )
            return blind_id, await dispatcher.submit(command)
        released = [item for item in members if item in early or item in ready]
        gathered = await asyncio.gather(*(run(item) for item in released), return_exceptions=True)
        pairs = []
        for blind_id, value in zip(released, gathered):
            if isinstance(value, BaseException):
                pairs.append((blind_id, CommandResult(
                    f"{command_id}:{blind_id}", blind_id, Outcome.FAILED,
                    error=f"{type(value).__name__}: {value}",
                )))
            else:
                pairs.append(value)
        results = dict(pairs)
        for blind_id, member_result in results.items():
            self._telemetry("group_member_completed", {
                "command_id": command_id, "group_id": group.id, "blind_id": blind_id,
                "outcome": member_result.outcome.value,
                "adapter_id": member_result.adapter_id,
                "attempts": member_result.attempts,
                "movement_start_ms": member_result.movement_start_ms,
            })
        self._emit_group_summary(
            group, command_id, started, members, released, results, preparation_errors,
            dispatch_times=dispatch_times, release_started=release_started,
        )
        return GroupResult(command_id, group.id, results, preparation_errors)

    def _emit_group_summary(
        self, group: GroupConfig, command_id: str, started: float,
        members: list[str], ready: list[str], results: dict[str, CommandResult],
        preparation_errors: dict[str, str], *,
        dispatch_times: dict[str, float] | None = None,
        release_started: float | None = None,
    ) -> None:
        movement_starts = [
            item.movement_start_ms for item in results.values()
            if item.movement_start_ms is not None
        ]
        completed_count = sum(
            item.outcome is Outcome.COMPLETED for item in results.values()
        )
        dispatch_offsets = [
            (value - release_started) * 1000
            for value in (dispatch_times or {}).values()
            if release_started is not None
        ]
        self._telemetry("group_command_completed", {
            "command_id": command_id, "group_id": group.id, "mode": group.mode,
            "duration_ms": (time.monotonic() - started) * 1000,
            "member_count": len(members), "released_count": len(ready),
            "completed_count": completed_count,
            "failed_count": len(results) - completed_count,
            "preparation_failure_count": len(preparation_errors),
            "movement_observed_count": len(movement_starts),
            "movement_start_spread_ms": (
                max(movement_starts) - min(movement_starts)
                if len(movement_starts) >= 2 else 0.0 if movement_starts else None
            ),
            "dispatch_spread_ms": (
                max(dispatch_offsets) - min(dispatch_offsets)
                if len(dispatch_offsets) >= 2 else 0.0 if dispatch_offsets else None
            ),
            "outcomes": sorted({item.outcome.value for item in results.values()}),
        })

    def snapshot(self) -> dict[str, object]:
        blinds = {}
        for blind_id, dispatcher in self.dispatchers.items():
            config, state = dispatcher.blind.config, dispatcher.blind.state
            blinds[blind_id] = {
                "id": blind_id, "name": config.name, "dual_rail": config.dual_rail,
                "bottom": state.bottom,
                "top": (raw_to_ha_position(Rail.TOP, state.top) if state.top is not None else None),
                **state.battery_fields(),
                "rssi": state.rssi, "updated_at": state.updated_at,
                **self.diagnostics_for(blind_id),
            }
        return {
            "blinds": blinds,
            "groups": {key: {"id": key, "name": value.name, "members": list(value.members),
                              "mode": value.mode} for key, value in self.groups.items()},
            "adapters": dict(self._adapter_health_provider()),
        }

    async def close(self) -> None:
        await asyncio.gather(*(item.close() for item in self.dispatchers.values()), return_exceptions=True)
