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
        self.prepare_calls = 0
        self.refreshes = 0

    async def prepare(self):
        self.prepare_calls += 1
        if self.prepare_error:
            self._session = None
            raise RuntimeError(self.prepare_error)
        return "adapter"

    async def refresh_status(self):
        self.refreshes += 1
        self.state.bottom = 31
        self.state.top = 71
        self.state.battery = 88
        return self.state


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


def test_status_refresh_is_individual_only():
    async def scenario():
        dispatchers = {"a": FakeDispatcher("a"), "b": FakeDispatcher("b")}
        controller = Controller(dispatchers, (GroupConfig("living", "Living", ("a", "b")),))
        status = await controller.refresh_status("a")
        assert dispatchers["a"].blind.refreshes == 1
        assert status["bottom"] == 31
        assert status["top"] == 29
        assert status["battery"] == 88
        try:
            await controller.refresh_status("living")
        except ValueError as exc:
            assert "individual" in str(exc)
        else:
            raise AssertionError("group refresh should be rejected")
    asyncio.run(scenario())


def test_status_refresh_prefers_blind_when_group_has_same_id():
    async def scenario():
        dispatchers = {"basement": FakeDispatcher("basement")}
        group = GroupConfig("basement", "Basement", ("basement",))
        controller = Controller(dispatchers, (group,))
        await controller.refresh_status("basement")
        assert dispatchers["basement"].blind.refreshes == 1
    asyncio.run(scenario())


def test_explicit_target_type_disambiguates_same_blind_and_group_id():
    async def scenario():
        dispatchers = {"shared": FakeDispatcher("shared"), "member": FakeDispatcher("member")}
        group = GroupConfig("shared", "Shared", ("member",))
        controller = Controller(dispatchers, (group,))

        blind_result = await controller.submit_command(
            "shared", "stop", command_id="blind", target_type="blind"
        )
        group_result = await controller.submit_command(
            "shared", "stop", command_id="group", target_type="group"
        )
        legacy_result = await controller.submit_command(
            "shared", "stop", command_id="legacy"
        )

        assert isinstance(blind_result, CommandResult)
        assert isinstance(group_result, GroupResult)
        assert isinstance(legacy_result, CommandResult)
        assert [item.id for item in dispatchers["shared"].commands] == ["blind", "legacy"]
        assert [item.id for item in dispatchers["member"].commands] == ["group:member"]
    asyncio.run(scenario())


def test_synchronized_group_stop_skips_readiness_preparation():
    async def scenario():
        dispatchers = {
            "a": FakeDispatcher("a", prepare_error="would block stop"),
            "b": FakeDispatcher("b", prepare_error="would block stop"),
        }
        group = GroupConfig("living", "Living", ("a", "b"), "synchronized", 10)
        result = await Controller(dispatchers, (group,)).submit_command(
            "living", "stop", command_id="halt", target_type="group"
        )
        assert result.successful
        assert all(item.blind.prepare_calls == 0 for item in dispatchers.values())
        assert all(item.commands[0].kind is CommandKind.STOP for item in dispatchers.values())
    asyncio.run(scenario())


def test_group_stop_is_not_serialized_behind_existing_group_job():
    class BlockingDispatcher(FakeDispatcher):
        def __init__(self, blind_id):
            super().__init__(blind_id)
            self.started = asyncio.Event()

        async def submit(self, command):
            self.commands.append(command)
            if command.kind is not CommandKind.STOP:
                self.started.set()
                await asyncio.Event().wait()
            return CommandResult(command.id, self.blind.config.id, Outcome.COMPLETED)

    async def scenario():
        dispatcher = BlockingDispatcher("a")
        controller = Controller(
            {"a": dispatcher}, (GroupConfig("living", "Living", ("a",)),)
        )
        moving = asyncio.create_task(controller.submit_command(
            "living", "open", command_id="move", target_type="group"
        ))
        await dispatcher.started.wait()
        stopped = await asyncio.wait_for(controller.submit_command(
            "living", "stop", command_id="halt", target_type="group"
        ), 0.2)
        assert stopped.successful
        assert [item.kind for item in dispatcher.commands] == [
            CommandKind.FULL_OPEN, CommandKind.STOP,
        ]
        moving.cancel()
        try:
            await moving
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
