"""Bounded per-blind command queues with in-memory idempotency."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass

from .blind import BlindCommand, BlindController, CommandResult, Outcome


class QueueFull(RuntimeError):
    pass


@dataclass
class _Queued:
    command: BlindCommand
    future: asyncio.Future[CommandResult]


class BlindDispatcher:
    def __init__(self, blind: BlindController, max_queue: int = 8, dedupe_size: int = 256):
        self.blind = blind
        self._queue: asyncio.Queue[_Queued | None] = asyncio.Queue(max_queue)
        self._dedupe_size = dedupe_size
        self._results: OrderedDict[str, CommandResult | asyncio.Future[CommandResult]] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self._worker:
            self._worker = asyncio.create_task(self._run(), name=f"blind-{self.blind.config.id}")

    async def submit(self, command: BlindCommand) -> CommandResult:
        previous = self._results.get(command.id)
        if isinstance(previous, CommandResult):
            return CommandResult(**{**previous.__dict__, "outcome": Outcome.DUPLICATE})
        if isinstance(previous, asyncio.Future):
            return await asyncio.shield(previous)
        if not self._worker:
            self.start()
        future: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._results[command.id] = future
        try:
            self._queue.put_nowait(_Queued(command, future))
        except asyncio.QueueFull:
            self._results.pop(command.id, None)
            raise QueueFull(f"queue full for {self.blind.config.id}")
        return await asyncio.shield(future)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                result = await self.blind.execute(item.command)
            except Exception as exc:
                result = CommandResult(item.command.id, self.blind.config.id, Outcome.FAILED,
                                       received_at=item.command.received_at,
                                       error=f"{type(exc).__name__}: {exc}")
            self._results[item.command.id] = result
            self._results.move_to_end(item.command.id)
            while len(self._results) > self._dedupe_size:
                self._results.popitem(last=False)
            if not item.future.done():
                item.future.set_result(result)
            self._queue.task_done()

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None and not item.future.done():
                item.future.set_result(CommandResult(
                    item.command.id, self.blind.config.id, Outcome.FAILED,
                    received_at=item.command.received_at,
                    error="controller shutdown before execution",
                ))
            self._queue.task_done()
        await self.blind.close()
