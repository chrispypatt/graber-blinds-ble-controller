import asyncio

import pytest

from graber.blind import CommandResult, Outcome
from graber.config import GroupConfig
from graber.dispatcher import BlindDispatcher, GroupMemberAdmission
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

    async def prepare(self, *, command_id=None):
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
        self.lifecycle_listener = None

    def set_lifecycle_listener(self, listener):
        self.lifecycle_listener = listener

    async def submit(self, command):
        self.commands.append(command)
        return CommandResult(command.id, self.blind.config.id, self.outcome)

    async def admit_group_member(self, command):
        return GroupMemberAdmission(0)

    async def submit_prepared(self, command, generation):
        return await self.submit(command)

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


def test_best_effort_group_telemetry_reports_dispatch_and_movement_spread():
    class TimedDispatcher(FakeDispatcher):
        def __init__(self, blind_id, movement_start_ms):
            super().__init__(blind_id)
            self.movement_start_ms = movement_start_ms

        async def submit(self, command):
            self.commands.append(command)
            return CommandResult(
                command.id, self.blind.config.id, Outcome.COMPLETED,
                adapter_id="external", attempts=1,
                movement_start_ms=self.movement_start_ms,
            )

    async def scenario():
        events = []
        dispatchers = {
            "a": TimedDispatcher("a", 1000),
            "b": TimedDispatcher("b", 1450),
        }
        controller = Controller(
            dispatchers, (GroupConfig("living", "Living", ("a", "b")),),
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        result = await controller.submit_command(
            "living", "open", command_id="group", source="mqtt",
            target_type="group",
        )
        assert result.successful
        names = [event for event, _ in events]
        assert names.count("group_command_received") == 1
        assert names.count("group_commands_released") == 1
        assert names.count("group_member_dispatched") == 2
        assert names.count("group_member_completed") == 2
        summary = [fields for event, fields in events
                   if event == "group_command_completed"][0]
        assert summary["group_id"] == "living"
        assert summary["mode"] == "best_effort"
        assert summary["member_count"] == 2
        assert summary["completed_count"] == 2
        assert summary["movement_observed_count"] == 2
        assert summary["movement_start_spread_ms"] == 450
        assert summary["dispatch_spread_ms"] >= 0

    asyncio.run(scenario())


def test_active_lifecycle_replaces_stale_terminal_status_and_clears_on_result():
    dispatcher = FakeDispatcher("a")
    controller = Controller({"a": dispatcher}, ())
    updates = []
    controller.set_result_listener(lambda blind_id, value: updates.append((blind_id, value)))

    dispatcher.lifecycle_listener("a", "queued", "next-command")
    assert controller.diagnostics_for("a")["last_command_status"] == "queued"
    assert controller.diagnostics_for("a")["active_command_id"] == "next-command"

    dispatcher.lifecycle_listener("a", "awaiting_feedback", "next-command")
    controller._record_result(CommandResult("next-command", "a", Outcome.COMPLETED))
    assert [value["last_command_status"] for _, value in updates] == [
        "queued", "awaiting_feedback", "completed",
    ]
    assert controller.diagnostics_for("a")["active_command_id"] is None


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


def test_synchronized_group_telemetry_reports_preparation_failure():
    async def scenario():
        events = []
        dispatchers = {
            "a": FakeDispatcher("a", prepare_error="missing"),
            "b": FakeDispatcher("b"),
        }
        group = GroupConfig(
            "living", "Living", ("a", "b"), "synchronized", 1, "release_ready"
        )
        await Controller(
            dispatchers, (group,),
            telemetry=lambda event, fields: events.append((event, fields)),
        ).submit_command("living", "close", command_id="group", source="web")
        prepared = [fields for event, fields in events
                    if event == "group_preparation_completed"][0]
        assert prepared["ready_count"] == 1
        assert prepared["preparation_failure_count"] == 1
        assert prepared["failed_members"] == ["a"]
        summary = [fields for event, fields in events
                   if event == "group_command_completed"][0]
        assert summary["released_count"] == 1
        assert summary["preparation_failure_count"] == 1

    asyncio.run(scenario())


def test_top_home_assistant_position_is_inverted_at_boundary():
    kind, rail, raw = parse_action("set_top_position", 80)
    assert (kind, rail, raw) == (CommandKind.SET_POSITION, Rail.TOP, 20)


def test_top_open_close_actions_map_to_opposite_raw_endpoints():
    assert parse_action("open_top") == (CommandKind.SET_POSITION, Rail.TOP, 0)
    assert parse_action("close_top") == (CommandKind.SET_POSITION, Rail.TOP, 100)
    assert parse_action("open_bottom") == (CommandKind.SET_POSITION, Rail.BOTTOM, 100)
    assert parse_action("close_bottom") == (CommandKind.SET_POSITION, Rail.BOTTOM, 0)


def test_snapshot_normalizes_raw_tdbu_boundaries_once():
    dispatcher = FakeDispatcher("tdbu")
    dispatcher.blind.config.dual_rail = True
    controller = Controller({"tdbu": dispatcher}, ())

    dispatcher.blind.state.bottom = 0
    dispatcher.blind.state.top = 0
    state = controller.snapshot()["blinds"]["tdbu"]
    assert state["bottom"] == 0
    assert state["top"] == 100

    dispatcher.blind.state.bottom = 100
    dispatcher.blind.state.top = 100
    state = controller.snapshot()["blinds"]["tdbu"]
    assert state["bottom"] == 100
    assert state["top"] == 0


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


def test_tdbu_only_commands_fail_before_dispatch_for_single_rail_targets():
    async def scenario():
        single = FakeDispatcher("single")
        dual = FakeDispatcher("dual")
        dual.blind.config.dual_rail = True
        controller = Controller(
            {"single": single, "dual": dual},
            (GroupConfig("mixed", "Mixed", ("single", "dual")),),
        )

        for target_id, target_type, action in (
            ("single", "blind", "open_top"),
            ("mixed", "group", "stack_up"),
        ):
            try:
                await controller.submit_command(
                    target_id, action, target_type=target_type
                )
            except ValueError as exc:
                assert "TDBU" in str(exc)
            else:
                raise AssertionError(f"{action} should be rejected before dispatch")

        assert single.commands == []
        assert dual.commands == []

        await controller.submit_command("single", "favorite", target_type="blind")
        group_result = await controller.submit_command(
            "mixed", "favorite", target_type="group"
        )
        assert single.commands[-1].kind is CommandKind.FAVORITE
        assert dual.commands[-1].kind is CommandKind.FAVORITE
        assert group_result.successful

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


@pytest.mark.parametrize("failure", [None, "prepare", "stop", "cancel"])
def test_synchronized_group_stops_busy_member_then_honors_readiness(failure):
    class MovingBlind(FakeBlind):
        def __init__(self):
            super().__init__("moving")
            self.commands = []
            self.move_started = asyncio.Event()
            self.stop_started = asyncio.Event()

        async def execute(self, command):
            self.commands.append(command)
            if command.kind is CommandKind.STOP:
                self.stop_started.set()
                if failure == "stop":
                    return CommandResult(
                        command.id, self.config.id, Outcome.COMPLETION_UNCONFIRMED,
                    )
            elif command.id == "remote":
                self.move_started.set()
                await asyncio.Event().wait()
            return CommandResult(command.id, self.config.id, Outcome.COMPLETED)

        async def close(self):
            pass

    async def scenario():
        moving_blind = MovingBlind()
        moving = BlindDispatcher(moving_blind)
        idle = FakeDispatcher("idle")
        preparing = asyncio.Event()
        release = asyncio.Event()

        async def prepare_idle(*, command_id):
            assert command_id == "group-close:idle"
            preparing.set()
            await release.wait()
            if failure == "prepare":
                raise RuntimeError("idle adapter unavailable")

        idle.blind.prepare = prepare_idle
        group = GroupConfig("group", "Group", ("moving", "idle"), "synchronized", 2, "abort")
        controller = Controller({"moving": moving, "idle": idle}, (group,))
        remote = asyncio.create_task(controller.submit_command(
            "moving", "open", command_id="remote",
        ))
        await asyncio.wait_for(moving_blind.move_started.wait(), 1)
        grouped = asyncio.create_task(controller.submit_command(
            "group", "close", command_id="group-close",
        ))
        try:
            await asyncio.wait_for(preparing.wait(), 1)
            await asyncio.wait_for(moving_blind.stop_started.wait(), 1)
            assert (await remote).outcome is Outcome.FAILED
            assert moving_blind.prepare_calls == 0
            assert idle.commands == []
            assert len(moving_blind.commands) == 2
            assert not grouped.done()

            if failure == "cancel":
                grouped.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await grouped
                assert len(moving_blind.commands) == 2
                direct = await asyncio.wait_for(controller.submit_command("moving", "close"), 1)
                assert direct.outcome is Outcome.COMPLETED
                assert [item.kind for item in moving_blind.commands] == [
                    CommandKind.FULL_OPEN, CommandKind.STOP, CommandKind.FULL_CLOSE,
                ]
                return

            release.set()
            result = await asyncio.wait_for(grouped, 1)
            if failure:
                assert not result.successful
                assert len(moving_blind.commands) == 2
                assert idle.commands == []
            else:
                assert result.successful
                assert [item.kind for item in moving_blind.commands] == [
                    CommandKind.FULL_OPEN, CommandKind.STOP, CommandKind.FULL_CLOSE,
                ]
                assert [item.kind for item in idle.commands] == [CommandKind.FULL_CLOSE]
        finally:
            release.set()
            await moving.close()

    asyncio.run(scenario())


def test_explicit_stop_invalidates_movement_during_synchronized_preparation():
    class PreparingBlind(FakeBlind):
        def __init__(self):
            super().__init__("preparing")
            self.preparing = asyncio.Event()
            self.release = asyncio.Event()
            self.commands = []

        async def prepare(self, *, command_id=None):
            self.preparing.set()
            await self.release.wait()

        async def execute(self, command):
            self.commands.append(command)
            return CommandResult(command.id, self.config.id, Outcome.COMPLETED)

        async def close(self):
            pass

    async def scenario():
        blind = PreparingBlind()
        dispatcher = BlindDispatcher(blind)
        group = GroupConfig("group", "Group", ("preparing",), "synchronized", 2)
        controller = Controller({"preparing": dispatcher}, (group,))
        grouped = asyncio.create_task(controller.submit_command("group", "open"))
        try:
            await asyncio.wait_for(blind.preparing.wait(), 1)
            stopped = await asyncio.wait_for(controller.submit_command("preparing", "stop"), 1)
            assert stopped.outcome is Outcome.COMPLETED
            blind.release.set()
            result = await asyncio.wait_for(grouped, 1)
            assert result.results["preparing"].outcome is Outcome.FAILED
            assert [item.kind for item in blind.commands] == [CommandKind.STOP]
        finally:
            blind.release.set()
            await dispatcher.close()

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
