import asyncio

from graber.blind import BlindCommand, CommandResult, Outcome
from graber.dispatcher import BlindDispatcher
from graber.protocol import CommandKind


class FakeConfig:
    id = "blind"


class FakeBlind:
    config = FakeConfig()
    calls = 0

    async def execute(self, command):
        self.calls += 1
        return CommandResult(command.id, "blind", Outcome.COMPLETED)

    async def close(self):
        pass


def test_duplicate_command_id_is_not_executed_twice():
    async def scenario():
        blind = FakeBlind()
        dispatcher = BlindDispatcher(blind)
        first = await dispatcher.submit(BlindCommand("stable-id", CommandKind.STOP))
        duplicate = await dispatcher.submit(BlindCommand("stable-id", CommandKind.STOP))
        assert first.outcome is Outcome.COMPLETED
        assert duplicate.outcome is Outcome.DUPLICATE
        assert blind.calls == 1
        await dispatcher.close()
    asyncio.run(scenario())


def test_stop_preempts_running_command_and_supersedes_queued_work():
    class BlockingBlind:
        config = FakeConfig()

        def __init__(self):
            self.started = asyncio.Event()
            self.calls = []
            self.cancelled = []
            self.active = 0
            self.max_active = 0

        async def execute(self, command):
            self.calls.append(command.kind)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if command.kind is not CommandKind.STOP:
                    self.started.set()
                    await asyncio.Event().wait()
                return CommandResult(command.id, "blind", Outcome.COMPLETED)
            except asyncio.CancelledError:
                self.cancelled.append(command.kind)
                raise
            finally:
                self.active -= 1

        async def close(self):
            pass

    async def scenario():
        blind = BlockingBlind()
        dispatcher = BlindDispatcher(blind)
        running = asyncio.create_task(dispatcher.submit(
            BlindCommand("open", CommandKind.FULL_OPEN)
        ))
        await blind.started.wait()
        queued = asyncio.create_task(dispatcher.submit(
            BlindCommand("close", CommandKind.FULL_CLOSE)
        ))
        await asyncio.sleep(0)

        stopped = await asyncio.wait_for(dispatcher.submit(
            BlindCommand("stop", CommandKind.STOP)
        ), 0.2)
        running_result, queued_result = await asyncio.gather(running, queued)

        assert stopped.outcome is Outcome.COMPLETED
        assert running_result.outcome is Outcome.FAILED
        assert queued_result.outcome is Outcome.FAILED
        assert "superseded by stop" in running_result.error
        assert "superseded by stop" in queued_result.error
        assert blind.calls == [CommandKind.FULL_OPEN, CommandKind.STOP]
        assert blind.cancelled == [CommandKind.FULL_OPEN]
        assert blind.max_active == 1
        await dispatcher.close()

    asyncio.run(scenario())


def test_preempted_command_id_remains_deduplicated():
    class BlockingBlind:
        config = FakeConfig()

        def __init__(self):
            self.started = asyncio.Event()

        async def execute(self, command):
            if command.kind is not CommandKind.STOP:
                self.started.set()
                await asyncio.Event().wait()
            return CommandResult(command.id, "blind", Outcome.COMPLETED)

        async def close(self):
            pass

    async def scenario():
        blind = BlockingBlind()
        dispatcher = BlindDispatcher(blind)
        original = asyncio.create_task(dispatcher.submit(
            BlindCommand("move", CommandKind.FULL_OPEN)
        ))
        await blind.started.wait()
        await dispatcher.submit(BlindCommand("stop", CommandKind.STOP))
        result = await original
        duplicate = await dispatcher.submit(BlindCommand("move", CommandKind.FULL_OPEN))
        assert result.outcome is Outcome.FAILED
        assert duplicate.outcome is Outcome.DUPLICATE
        assert duplicate.error == result.error
        await dispatcher.close()

    asyncio.run(scenario())
