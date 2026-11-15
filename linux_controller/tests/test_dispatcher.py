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
