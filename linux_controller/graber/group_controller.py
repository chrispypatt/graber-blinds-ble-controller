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
        return bool(self.results) and all(x.outcome is Outcome.COMPLETED for x in self.results.values())


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
                 adapter_health_provider: Callable[[], Mapping[str, object]] | None = None):
        self.dispatchers = dispatchers
        self.groups = {item.id: item for item in groups}
        self._diagnostics = {
            blind_id: {"last_command_status": "idle", "connection_failures": 0, "retry_count": 0}
            for blind_id in dispatchers
        }
        self._result_listener: Callable[[str, dict[str, object]], None] | None = None
        self._adapter_health_provider = adapter_health_provider or (lambda: {})

    def set_result_listener(self, listener: Callable[[str, dict[str, object]], None]) -> None:
        self._result_listener = listener

    def diagnostics_for(self, blind_id: str) -> dict[str, object]:
        return dict(self._diagnostics[blind_id])

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
            "battery": state.battery,
            "rssi": state.rssi,
            "updated_at": state.updated_at,
        }

    def _record_result(self, result: CommandResult) -> None:
        diagnostic = self._diagnostics[result.blind_id]
        diagnostic["last_command_status"] = result.outcome.value
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
            result = await self.dispatchers[target_id].submit(
                BlindCommand(command_id, kind, rail, raw_position, time.time())
            )
            self._record_result(result)
            return result
        if target_id not in self.groups:
            if target_type == "group":
                raise KeyError(f"unknown group {target_id}")
            raise KeyError(f"unknown blind/group {target_id}")
        result = await self._group(self.groups[target_id], command_id, kind, rail, raw_position)
        for member_result in result.results.values():
            self._record_result(member_result)
        return result

    async def _group(self, group: GroupConfig, command_id: str, kind: CommandKind,
                     rail: Rail | None, position: int | None) -> GroupResult:
        members = [item for item in group.members if item in self.dispatchers]
        preparation_errors: dict[str, str] = {}
        ready = list(members)
        # STOP is an interrupt, not a synchronized movement. Preparing every
        # member first would delay it behind the operations it must supersede.
        if group.mode == "synchronized" and kind is not CommandKind.STOP:
            async def prepare(blind_id: str) -> None:
                try:
                    await self.dispatchers[blind_id].blind.prepare()
                except Exception as exc:
                    preparation_errors[blind_id] = f"{type(exc).__name__}: {exc}"
            try:
                async with asyncio.timeout(group.prepare_timeout_s):
                    await asyncio.gather(*(prepare(item) for item in members))
            except TimeoutError:
                for item in members:
                    session = self.dispatchers[item].blind._session
                    if (not session or not session.connection.connected) and item not in preparation_errors:
                        preparation_errors[item] = "preparation deadline exceeded"
            ready = [item for item in members if item not in preparation_errors]
            if preparation_errors and group.partial_failure == "abort":
                return GroupResult(command_id, group.id, {}, preparation_errors)
        async def run(blind_id: str) -> tuple[str, CommandResult]:
            command = BlindCommand(f"{command_id}:{blind_id}", kind, rail, position, time.time())
            return blind_id, await self.dispatchers[blind_id].submit(command)
        gathered = await asyncio.gather(*(run(item) for item in ready), return_exceptions=True)
        pairs = []
        for blind_id, value in zip(ready, gathered):
            if isinstance(value, BaseException):
                pairs.append((blind_id, CommandResult(
                    f"{command_id}:{blind_id}", blind_id, Outcome.FAILED,
                    error=f"{type(value).__name__}: {value}",
                )))
            else:
                pairs.append(value)
        return GroupResult(command_id, group.id, dict(pairs), preparation_errors)

    def snapshot(self) -> dict[str, object]:
        blinds = {}
        for blind_id, dispatcher in self.dispatchers.items():
            config, state = dispatcher.blind.config, dispatcher.blind.state
            blinds[blind_id] = {
                "id": blind_id, "name": config.name, "dual_rail": config.dual_rail,
                "bottom": state.bottom,
                "top": (raw_to_ha_position(Rail.TOP, state.top) if state.top is not None else None),
                "battery": state.battery,
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
