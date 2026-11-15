import asyncio

from graber.blind import CommandResult, Outcome
from graber.config import GroupConfig
from graber.group_controller import Controller, GroupResult, parse_action
from graber.protocol import CommandKind, Rail


class FakeBlind:
    def __init__(self, blind_id, prepare_error=None):
        self.config = type("Config", (), {"id": blind_id, "name": blind_id, "dual_rail": False})()
        self.state = type("State", (), {"bottom": None, "top": None, "battery": None, "rssi": None, "updated_at": None})()
        self._session = object()
        self.prepare_error = prepare_error

    async def prepare(self):
        if self.prepare_error:
            self._session = None
            raise RuntimeError(self.prepare_error)
        return "adapter"


class FakeDispatcher:
    def __init__(self, blind_id, outcome=Outcome.COMPLETED, prepare_error=None):
        self.blind = FakeBlind(blind_id, prepare_error)
        self.outcome = outcome
        self.commands = []

    async def submit(self, command):
        self.commands.append(command)
        return CommandResult(command.id, self.blind.config.id, self.outcome)

    async def close(self):
        pass


def test_best_effort_failure_does_not_block_other_blinds():
    async def scenario():
        dispatchers = {"a": FakeDispatcher("a", Outcome.FAILED), "b": FakeDispatcher("b")}
        controller = Controller(dispatchers, (GroupConfig("living", "Living", ("a", "b")),))
        result = await controller.submit_command("living", "open", command_id="group", source="test")
        assert isinstance(result, GroupResult)
        assert result.results["a"].outcome is Outcome.FAILED
        assert result.results["b"].outcome is Outcome.COMPLETED
    asyncio.run(scenario())


def test_synchronized_release_ready_omits_failed_preparation():
    async def scenario():
        dispatchers = {"a": FakeDispatcher("a", prepare_error="missing"), "b": FakeDispatcher("b")}
        group = GroupConfig("living", "Living", ("a", "b"), "synchronized", 1, "release_ready")
        result = await Controller(dispatchers, (group,)).submit_command(
            "living", "close", command_id="group", source="test"
        )
        assert set(result.results) == {"b"}
        assert "a" in result.preparation_errors
    asyncio.run(scenario())


def test_top_home_assistant_position_is_inverted_at_boundary():
    kind, rail, raw = parse_action("set_top_position", 80)
    assert (kind, rail, raw) == (CommandKind.SET_POSITION, Rail.TOP, 20)
