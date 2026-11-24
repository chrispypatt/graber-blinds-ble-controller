"""Bounded per-blind command queues with in-memory idempotency."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Callable

from .blind import BlindController
from .commands import BlindCommand, CommandResult, Outcome
from graber.bluetooth.protocol import CommandKind


class QueueFull(RuntimeError):
    pass


@dataclass
class _Queued:
    command: BlindCommand
    future: asyncio.Future[CommandResult]
    generation: int
    queued_at: float
    queue_depth_at_enqueue: int
    started_at: float | None = None
    internal_stop: bool = False
    awaiting_group_release: bool = False
    interrupted_by: str | None = None

    @property
    def is_stop(self) -> bool:
        return self.command.kind is CommandKind.STOP


@dataclass(frozen=True)
class GroupMemberAdmission:
    generation: int
    result: asyncio.Future[CommandResult] | None = None
    stop: asyncio.Future[CommandResult] | None = None
    deferred: bool = False


class BlindDispatcher:
    def __init__(self, blind: BlindController, max_queue: int = 8, dedupe_size: int = 256,
                 telemetry: Callable[[str, dict[str, object]], None] | None = None):
        self.blind = blind
        self._queue: asyncio.Queue[_Queued | None] = asyncio.Queue(max_queue)
        self._dedupe_size = dedupe_size
        self._results: OrderedDict[str, CommandResult | asyncio.Future[CommandResult]] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._generation = 0
        self._active_item: _Queued | None = None
        self._active_task: asyncio.Task[CommandResult] | None = None
        self._pending_after_stop: _Queued | None = None
        self._stop_in_progress: asyncio.Future[CommandResult] | None = None
        self._lifecycle_listener: Callable[[str, str, str], None] | None = None
        self._telemetry = telemetry or (lambda _event, _fields: None)

    def set_lifecycle_listener(self, listener: Callable[[str, str, str], None]) -> None:
        self._lifecycle_listener = listener

    def _notify_lifecycle(self, status: str, command_id: str) -> None:
        if self._lifecycle_listener is not None:
            self._lifecycle_listener(self.blind.config.id, status, command_id)

    def start(self) -> None:
        if not self._worker:
            self._worker = asyncio.create_task(self._run(), name=f"blind-{self.blind.config.id}")

    async def submit(self, command: BlindCommand) -> CommandResult:
        async with self._state_lock:
            future = self._submit_locked(command)
        return await asyncio.shield(future)

    async def admit_group_member(self, command: BlindCommand) -> GroupMemberAdmission:
        """Handle busy members immediately and capture the idle member's generation."""
        async with self._state_lock:
            if command.id in self._results:
                return GroupMemberAdmission(self._generation, self._submit_locked(command))
            if self._interruptible_item() or self._stop_in_progress or self._pending_after_stop:
                future = self._submit_locked(command, defer_until_group_release=True)
                pending = self._pending_after_stop
                deferred = pending is not None and pending.future is future
                return GroupMemberAdmission(
                    self._generation, future, self._stop_in_progress if deferred else None, deferred,
                )
            return GroupMemberAdmission(self._generation)

    async def resolve_group_member(self, command_id: str, *, release: bool) -> None:
        async with self._state_lock:
            pending = self._pending_after_stop
            if pending is None or pending.command.id != command_id or not pending.awaiting_group_release:
                return
            if release:
                pending.awaiting_group_release = False
                self._release_pending_locked()
            else:
                self._pending_after_stop = None
                self._store_result_locked(pending, CommandResult(
                    command_id, self.blind.config.id, Outcome.FAILED,
                    received_at=pending.command.received_at, error="group preparation aborted",
                ))

    async def submit_prepared(self, command: BlindCommand, generation: int) -> CommandResult:
        async with self._state_lock:
            future = self._submit_locked(command, generation)
        return await asyncio.shield(future)

    def _interruptible_item(self) -> _Queued | None:
        if (self._active_item is not None and not self._active_item.is_stop
                and self._active_task is not None and not self._active_task.done()):
            return self._active_item
        return None

    @staticmethod
    def _same_target(first: BlindCommand, second: BlindCommand) -> bool:
        return (first.kind, first.rail, first.position) == (second.kind, second.rail, second.position)

    def _submit_locked(
        self, command: BlindCommand, generation: int | None = None,
        *, defer_until_group_release: bool = False,
    ) -> asyncio.Future[CommandResult]:
        previous = self._results.get(command.id)
        if previous is not None:
            completed = isinstance(previous, CommandResult)
            self._telemetry("dispatcher_command_duplicate", {
                "blind_id": self.blind.config.id,
                "command_id": command.id,
                "duplicate_state": "completed" if completed else "in_flight",
            })
            if isinstance(previous, CommandResult):
                duplicate = asyncio.get_running_loop().create_future()
                duplicate.set_result(replace(previous, outcome=Outcome.DUPLICATE))
                return duplicate
            return previous

        if not self._worker:
            self.start()
        future = asyncio.get_running_loop().create_future()
        self._results[command.id] = future
        if generation is not None and generation != self._generation:
            item = _Queued(command, future, generation, time.monotonic(), self._queue.qsize())
            self._store_result_locked(item, self._superseded_result(item, "newer-stop"))
            return future
        is_stop = command.kind is CommandKind.STOP
        active_item = self._interruptible_item()
        pending = self._pending_after_stop
        current = pending or active_item
        if not is_stop and current is not None and self._same_target(current.command, command):
            duplicate = _Queued(command, future, self._generation,
                                time.monotonic(), self._queue.qsize())
            self._store_result_locked(duplicate, CommandResult(
                command.id, self.blind.config.id, Outcome.DUPLICATE,
                received_at=command.received_at,
            ))
            return future

        if pending is not None or self._stop_in_progress is not None:
            if pending is not None:
                self._store_result_locked(pending, self._superseded_result(pending, command.id))
            if not is_stop:
                item = _Queued(command, future, self._generation, time.monotonic(), 0,
                               awaiting_group_release=defer_until_group_release)
                self._pending_after_stop = item
                self._record_queued(item)
                self._release_pending_locked()
                return future
            self._pending_after_stop = None
            self._stop_in_progress = None

        conflict_stop = active_item is not None and not is_stop
        if is_stop or conflict_stop:
            self._generation += 1
            self._supersede_pending_locked(command.id)
            if active_item is not None and self._active_task is not None:
                active_item.interrupted_by = command.id
                self._active_task.cancel()
        item = _Queued(command, future, self._generation, time.monotonic(), self._queue.qsize())
        if conflict_stop and active_item is not None:
            item.awaiting_group_release = defer_until_group_release
            self._pending_after_stop = item
            stop_command = replace(command, id=f"{command.id}:stop", kind=CommandKind.STOP,
                                   rail=None, position=None, internal=True)
            stop_future = asyncio.get_running_loop().create_future()
            self._stop_in_progress = stop_future
            self._queue.put_nowait(_Queued(
                stop_command, stop_future, self._generation,
                item.queued_at, 0, internal_stop=True,
            ))
            self._telemetry("dispatcher_command_conflict", {
                "blind_id": self.blind.config.id,
                "command_id": command.id,
                "interrupted_command_id": active_item.command.id,
                "stop_command_id": stop_command.id,
                "kind": command.kind.value,
            })
            self._record_queued(item)
            return future

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._results.pop(command.id, None)
            self._telemetry("dispatcher_queue_full", {
                "blind_id": self.blind.config.id,
                "command_id": command.id,
                "kind": command.kind.value,
                "queue_depth": self._queue.qsize(),
            })
            raise QueueFull(f"queue full for {self.blind.config.id}")

        self._record_queued(item)
        return future

    def _release_pending_locked(self) -> None:
        pending = self._pending_after_stop
        if pending is not None and self._stop_in_progress is None and not pending.awaiting_group_release:
            self._pending_after_stop = None
            self._queue.put_nowait(pending)

    def _record_queued(self, item: _Queued) -> None:
        self._telemetry("dispatcher_command_queued", {
            "blind_id": self.blind.config.id,
            "command_id": item.command.id,
            "kind": item.command.kind.value,
            "queue_depth": item.queue_depth_at_enqueue + 1,
        })
        self._notify_lifecycle("queued", item.command.id)

    def _superseded_result(self, item: _Queued, stop_id: str) -> CommandResult:
        return CommandResult(
            item.command.id, self.blind.config.id, Outcome.FAILED,
            received_at=item.command.received_at,
            error=f"superseded by stop command {stop_id}",
        )

    def _store_result_locked(self, item: _Queued, result: CommandResult) -> None:
        now = time.monotonic()
        queue_wait_ms = (
            ((item.started_at or now) - item.queued_at) * 1000
        )
        self._telemetry("dispatcher_command_completed", {
            "blind_id": self.blind.config.id,
            "command_id": item.command.id,
            "kind": item.command.kind.value,
            "outcome": result.outcome.value,
            "queue_wait_ms": max(0.0, queue_wait_ms),
            "execution_ms": (
                max(0.0, (now - item.started_at) * 1000)
                if item.started_at is not None else 0.0
            ),
            "dispatcher_total_ms": max(0.0, (now - item.queued_at) * 1000),
            "queue_depth_at_enqueue": item.queue_depth_at_enqueue,
            "superseded": bool(
                result.error and result.error.startswith("superseded by stop command")
            ),
        })
        if item.internal_stop:
            if not item.future.done():
                item.future.set_result(result)
            return
        self._results[item.command.id] = result
        self._results.move_to_end(item.command.id)
        if len(self._results) > self._dedupe_size:
            # Pending STOP and movement requests remain idempotent while a
            # burst of discarded commands fills the completed-result cache.
            completed = [command_id for command_id, value in self._results.items()
                         if isinstance(value, CommandResult)]
            excess = max(0, len(completed) - self._dedupe_size)
            for command_id in completed[:excess]:
                del self._results[command_id]
        if not item.future.done():
            item.future.set_result(result)

    def _supersede_pending_locked(self, stop_id: str) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None:
                self._store_result_locked(item, self._superseded_result(item, stop_id))
            self._queue.task_done()

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            result: CommandResult | None = None
            try:
                async with self._state_lock:
                    if not item.is_stop and item.generation != self._generation:
                        result = self._superseded_result(item, "newer-stop")
                    else:
                        self._active_item = item
                        item.started_at = time.monotonic()
                        self._telemetry("dispatcher_command_started", {
                            "blind_id": self.blind.config.id,
                            "command_id": item.command.id,
                            "kind": item.command.kind.value,
                            "queue_wait_ms": max(
                                0.0, (item.started_at - item.queued_at) * 1000
                            ),
                            "queue_depth_at_enqueue": item.queue_depth_at_enqueue,
                        })
                        if not item.internal_stop:
                            self._notify_lifecycle("running", item.command.id)
                        self._active_task = asyncio.create_task(
                            self.blind.execute(item.command),
                            name=f"blind-execute-{self.blind.config.id}",
                        )
                if result is None:
                    try:
                        result = await self._active_task
                    except asyncio.CancelledError:
                        if item.interrupted_by is None or asyncio.current_task().cancelling():
                            raise
            except Exception as exc:
                result = CommandResult(item.command.id, self.blind.config.id, Outcome.FAILED,
                                       received_at=item.command.received_at,
                                       error=f"{type(exc).__name__}: {exc}")
            finally:
                async with self._state_lock:
                    if item.interrupted_by is not None:
                        result = self._superseded_result(item, item.interrupted_by)
                    elif result is None:
                        result = CommandResult(
                            item.command.id, self.blind.config.id, Outcome.FAILED,
                            received_at=item.command.received_at,
                            error="controller shutdown during execution",
                        )
                    if self._active_item is item:
                        self._active_item = None
                        self._active_task = None
                    self._store_result_locked(item, result)
                    if item.internal_stop:
                        if self._stop_in_progress is item.future:
                            self._stop_in_progress = None
                        if result.outcome is Outcome.COMPLETED:
                            self._release_pending_locked()
                        elif self._pending_after_stop is not None:
                            pending, self._pending_after_stop = self._pending_after_stop, None
                            self._store_result_locked(pending, replace(
                                result, command_id=pending.command.id,
                                received_at=pending.command.received_at,
                                error=f"STOP was not confirmed: {result.error or result.outcome.value}",
                            ))
                self._queue.task_done()

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        async with self._state_lock:
            if self._active_item is not None and not self._active_item.future.done():
                self._store_result_locked(self._active_item, CommandResult(
                    self._active_item.command.id, self.blind.config.id, Outcome.FAILED,
                    received_at=self._active_item.command.received_at,
                    error="controller shutdown during execution",
                ))
            self._active_item = None
            self._active_task = None
            self._stop_in_progress = None
            if self._pending_after_stop is not None:
                pending, self._pending_after_stop = self._pending_after_stop, None
                self._store_result_locked(pending, CommandResult(
                    pending.command.id, self.blind.config.id, Outcome.FAILED,
                    received_at=pending.command.received_at,
                    error="controller shutdown before execution",
                ))
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None:
                    self._store_result_locked(item, CommandResult(
                        item.command.id, self.blind.config.id, Outcome.FAILED,
                        received_at=item.command.received_at,
                        error="controller shutdown before execution",
                    ))
                self._queue.task_done()
        await self.blind.close()
