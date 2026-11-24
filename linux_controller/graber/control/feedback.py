"""GATT feedback, movement observation, and command-specific completion criteria."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from graber.bluetooth.ble_transport import BleConnection
from graber.bluetooth.protocol import (
    BOTTOM_POSITION_CHARACTERISTIC, TOP_POSITION_CHARACTERISTIC, CommandKind, Rail,
    position_from_notification,
)
from graber.config import BlindConfig, RetryConfig

from .commands import BlindCommand, CompletionUnconfirmed, PositionState


ENDPOINT_RECHECK_INTERVAL_S = 3.0


@dataclass(frozen=True)
class _CompletionTarget:
    command: BlindCommand
    rails: tuple[Rail, ...]
    endpoints: dict[Rail, int]

    @classmethod
    def for_command(cls, command: BlindCommand, dual_rail: bool) -> _CompletionTarget:
        if command.kind is CommandKind.PRESET and command.position != 5:
            raise CompletionUnconfirmed(
                "preset write acknowledged; completion target is unknown for this index"
            )
        if command.kind is CommandKind.SET_POSITION:
            # build_command validates rail and position before the first write.
            assert command.rail is not None
            rails = (command.rail,)
        else:
            rails = (Rail.BOTTOM, Rail.TOP) if dual_rail else (Rail.BOTTOM,)
        endpoints = {}
        if command.kind in {CommandKind.FULL_OPEN, CommandKind.FULL_CLOSE}:
            endpoints[Rail.BOTTOM] = 100 if command.kind is CommandKind.FULL_OPEN else 0
            if dual_rail:
                endpoints[Rail.TOP] = 100
        return cls(command, rails, endpoints)

    def reached(self, current: dict[Rail, int | None], quiet_s: float, movement: bool) -> bool:
        if self.command.kind is CommandKind.SET_POSITION:
            assert self.command.rail is not None and self.command.position is not None
            value = current[self.command.rail]
            return value is not None and abs(value - self.command.position) <= 1
        if self.endpoints:
            return all(current[rail] == target for rail, target in self.endpoints.items())
        if self.command.kind is CommandKind.STOP:
            return quiet_s >= 2.0
        if self.command.kind is CommandKind.FAVORITE:
            return movement and quiet_s >= 2.0
        if self.command.kind is CommandKind.PRESET:
            return current[Rail.BOTTOM] == 50 and current[Rail.TOP] == 100
        if self.command.kind in {CommandKind.STACK_UP, CommandKind.STACK_DOWN}:
            return quiet_s >= 0.6
        return False


class PositionFeedback:
    def __init__(
        self, state: PositionState, config: BlindConfig, retry: RetryConfig,
        state_listener: Callable[[str, PositionState], None], emit: Callable[..., None],
    ) -> None:
        self.state = state
        self.config = config
        self._retry = retry
        self._state_listener = state_listener
        self._emit = emit
        self._sample_counts = {Rail.BOTTOM: 0, Rail.TOP: 0}
        # Advertisements keep unsolicited movement visible, but only feedback
        # from the authenticated GATT connection can complete our commands.
        self._feedback_positions: dict[Rail, int | None] = {
            Rail.BOTTOM: None, Rail.TOP: None,
        }
        self._sample_event = asyncio.Event()
        self._last_change = time.monotonic()

    def baseline(self) -> dict[Rail, int]:
        """Snapshot authenticated sample counts immediately before the first command write."""
        return dict(self._sample_counts)

    def update_position(self, rail: Rail, value: int, *, verified: bool = True) -> None:
        previous = self.state.bottom if rail is Rail.BOTTOM else self.state.top
        if previous is not None and previous != value:
            self._last_change = time.monotonic()
        if rail is Rail.BOTTOM:
            self.state.bottom = value
        else:
            self.state.top = value
        self.state.updated_at = time.time()
        if verified:
            self._feedback_positions[rail] = value
            self._sample_counts[rail] += 1
            self._sample_event.set()
        self._state_listener(self.config.id, self.state)

    async def wait_for_completion(
        self, command: BlindCommand, baseline: dict[Rail, int], connection: BleConnection,
    ) -> tuple[bool, float | None, float | None]:
        target = _CompletionTarget.for_command(command, self.config.dual_rail)
        movement = False
        movement_start_ms: float | None = None
        feedback_started = time.monotonic()
        deadline = feedback_started + self._retry.feedback_timeout_s
        final_recheck_at = deadline - min(
            self._retry.gatt_timeout_s * len(target.endpoints), self._retry.feedback_timeout_s / 3,
        )
        last_endpoint_recheck = feedback_started
        endpoint_read: dict[Rail, int] = {}
        feedback_first_sample_ms: float | None = None
        initial = {Rail.BOTTOM: self.state.bottom, Rail.TOP: self.state.top}
        while time.monotonic() < deadline:
            fresh_rails = [rail for rail in target.rails if self._sample_counts[rail] > baseline[rail]]
            fresh = len(fresh_rails) == len(target.rails)
            if fresh and feedback_first_sample_ms is None:
                feedback_first_sample_ms = (time.monotonic() - feedback_started) * 1000
                self._emit(
                    "feedback_observed", command_id=command.id,
                    feedback_first_sample_ms=feedback_first_sample_ms,
                )
            current = dict(self._feedback_positions)
            changed = bool(fresh_rails) and any(
                initial[rail] is not None and current[rail] != initial[rail]
                for rail in target.rails
            )
            if changed and not movement:
                movement_start_ms = (time.time() - command.received_at) * 1000
                self._emit("movement_observed", command_id=command.id,
                           movement_start_ms=movement_start_ms)
            movement = movement or changed
            if fresh and target.reached(current, time.monotonic() - self._last_change, movement):
                if target.endpoints and endpoint_read == target.endpoints:
                    self._emit("endpoint_verified_by_read", command_id=command.id,
                               bottom_raw=endpoint_read.get(Rail.BOTTOM), top_raw=endpoint_read.get(Rail.TOP))
                return movement, movement_start_ms, feedback_first_sample_ms
            # Even fresh feedback can miss the endpoint. Check the actual rails
            # after a pause in reported progress, including repeated identical samples.
            now = time.monotonic()
            endpoint_recheck_at = max(self._last_change, last_endpoint_recheck) + ENDPOINT_RECHECK_INTERVAL_S
            # Reserve one last check even if conflicting reports look like progress.
            if last_endpoint_recheck < final_recheck_at:
                endpoint_recheck_at = min(endpoint_recheck_at, final_recheck_at)
            recheck_endpoint = bool(target.endpoints) and fresh and now >= endpoint_recheck_at
            if recheck_endpoint:
                endpoint_read = {}
            read_updated = await self._read_positions(
                connection, target.rails, baseline, deadline,
                endpoint_read if recheck_endpoint else None,
            )
            if recheck_endpoint:
                last_endpoint_recheck = time.monotonic()
            if read_updated:
                # Re-evaluate immediately; do not spend the remaining timeout
                # waiting for a notification after a successful explicit read.
                continue
            self._sample_event.clear()
            wait_s = min(1.2, deadline - time.monotonic())
            if target.endpoints and fresh and not recheck_endpoint:
                wait_s = min(wait_s, endpoint_recheck_at - time.monotonic())
            try:
                await asyncio.wait_for(self._sample_event.wait(), max(0.001, wait_s))
            except TimeoutError:
                pass
        self._emit(
            "feedback_timed_out", command_id=command.id,
            feedback_ms=(time.monotonic() - feedback_started) * 1000,
            feedback_first_sample_ms=feedback_first_sample_ms,
            movement_observed=movement,
            bottom_raw=self.state.bottom, top_raw=self.state.top,
            endpoint_read_bottom_raw=endpoint_read.get(Rail.BOTTOM),
            endpoint_read_top_raw=endpoint_read.get(Rail.TOP),
        )
        raise CompletionUnconfirmed("GATT writes acknowledged but fresh completion feedback timed out")

    async def _read_positions(
        self, connection: BleConnection, rails: tuple[Rail, ...], baseline: dict[Rail, int],
        deadline: float, endpoint_read: dict[Rail, int] | None,
    ) -> bool:
        """Fill missing rail samples, or read one complete endpoint snapshot."""
        updated = False
        for rail in rails:
            if endpoint_read is None and self._sample_counts[rail] > baseline[rail]:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            characteristic = BOTTOM_POSITION_CHARACTERISTIC if rail is Rail.BOTTOM else TOP_POSITION_CHARACTERISTIC
            try:
                raw = await asyncio.wait_for(
                    connection.read(characteristic), min(self._retry.gatt_timeout_s, remaining),
                )
                position = position_from_notification(raw)
                if endpoint_read is None:
                    self.update_position(rail, position)
                    updated = True
                else:
                    endpoint_read[rail] = position
            except Exception:
                # Notifications or a later bounded read may still confirm completion.
                continue
        if endpoint_read is not None and len(endpoint_read) == len(rails):
            for rail, position in endpoint_read.items():
                self.update_position(rail, position)
            updated = True
        return updated
