"""Bounded per-blind command queues with in-memory idempotency."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import Callable

from .blind import BlindCommand, BlindController, CommandResult, Outcome
from .protocol import CommandKind


class QueueFull(RuntimeError):
    pass


@dataclass
class _Queued:
    command: BlindCommand
    future: asyncio.Future[CommandResult]
    generation: int


class BlindDispatcher:
    def __init__(self, blind: BlindController, max_queue: int = 8, dedupe_size: int = 256):
        self.blind = blind
        self._queue: asyncio.Queue[_Queued | None] = asyncio.Queue(max_queue)
        self._dedupe_size = dedupe_size
        self._results: OrderedDict[str, CommandResult | asyncio.Future[CommandResult]] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._generation = 0
        self._active_item: _Queued | None = None
        self._active_task: asyncio.Task[CommandResult] | None = None
        self._lifecycle_listener: Callable[[str, str, str], None] | None = None

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
            previous = self._results.get(command.id)
            if isinstance(previous, CommandResult):
                return CommandResult(**{**previous.__dict__, "outcome": Outcome.DUPLICATE})
            if isinstance(previous, asyncio.Future):
                future = previous
            else:
                if not self._worker:
                    self.start()
                future = asyncio.get_running_loop().create_future()
                self._results[command.id] = future
                if command.kind is CommandKind.STOP:
                    self._generation += 1
                    self._supersede_pending_locked(command.id)
                    if (self._active_item is not None and
                            self._active_item.command.kind is not CommandKind.STOP and
                            self._active_task is not None):
                        self._active_task.cancel()
                try:
                    self._queue.put_nowait(_Queued(command, future, self._generation))
                    self._notify_lifecycle("queued", command.id)
                except asyncio.QueueFull:
                    self._results.pop(command.id, None)
                    raise QueueFull(f"queue full for {self.blind.config.id}")
        return await asyncio.shield(future)

    def _superseded_result(self, item: _Queued, stop_id: str) -> CommandResult:
        return CommandResult(
            item.command.id, self.blind.config.id, Outcome.FAILED,
            received_at=item.command.received_at,
            error=f"superseded by stop command {stop_id}",
        )

    def _store_result_locked(self, item: _Queued, result: CommandResult) -> None:
        self._results[item.command.id] = result
        self._results.move_to_end(item.command.id)
        while len(self._results) > self._dedupe_size:
            self._results.popitem(last=False)
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
                    if (item.command.kind is not CommandKind.STOP and
                            item.generation != self._generation):
                        result = self._superseded_result(item, "newer-stop")
                    else:
                        self._active_item = item
                        self._notify_lifecycle("running", item.command.id)
                        self._active_task = asyncio.create_task(
                            self.blind.execute(item.command),
                            name=f"blind-execute-{self.blind.config.id}",
                        )
                if result is None:
                    try:
                        result = await self._active_task
                    except asyncio.CancelledError:
                        async with self._state_lock:
                            preempted = (
                                item.command.kind is not CommandKind.STOP and
                                item.generation != self._generation
                            )
                        if not preempted:
                            raise
                        result = self._superseded_result(item, "newer-stop")
            except Exception as exc:
                result = CommandResult(item.command.id, self.blind.config.id, Outcome.FAILED,
                                       received_at=item.command.received_at,
                                       error=f"{type(exc).__name__}: {exc}")
            finally:
                async with self._state_lock:
                    if result is None:
                        result = CommandResult(
                            item.command.id, self.blind.config.id, Outcome.FAILED,
                            received_at=item.command.received_at,
                            error="controller shutdown during execution",
                        )
                    if self._active_item is item:
                        self._active_item = None
                        self._active_task = None
                    if result is not None:
                        self._store_result_locked(item, result)
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
