import asyncio

from graber.blind import BlindCommand, CommandResult, Outcome
from graber.dispatcher import BlindDispatcher
from graber.group_controller import Controller
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


def test_lifecycle_reports_queued_then_running():
    async def scenario():
        blind = FakeBlind()
        dispatcher = BlindDispatcher(blind)
        events = []
        dispatcher.set_lifecycle_listener(
            lambda blind_id, status, command_id: events.append(
                (blind_id, status, command_id)
            )
        )
        await dispatcher.submit(BlindCommand("move", CommandKind.FULL_OPEN))
        assert events == [
            ("blind", "queued", "move"),
            ("blind", "running", "move"),
        ]
        await dispatcher.close()

    asyncio.run(scenario())


def test_dispatcher_telemetry_measures_queue_and_execution_time():
    class BlockingBlind:
        config = FakeConfig()

        def __init__(self):
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def execute(self, command):
            if command.id == "first":
                self.first_started.set()
                await self.release_first.wait()
            return CommandResult(command.id, "blind", Outcome.COMPLETED)

        async def close(self):
            pass

    async def scenario():
        events = []
        blind = BlockingBlind()
        dispatcher = BlindDispatcher(
            blind, telemetry=lambda event, fields: events.append((event, fields))
        )
        first = asyncio.create_task(dispatcher.submit(
            BlindCommand("first", CommandKind.STOP)
        ))
        await blind.first_started.wait()
        second = asyncio.create_task(dispatcher.submit(
            BlindCommand("second", CommandKind.FULL_CLOSE)
        ))
        await asyncio.sleep(0.02)
        blind.release_first.set()
        await asyncio.gather(first, second)

        started = {
            fields["command_id"]: fields
            for event, fields in events if event == "dispatcher_command_started"
        }
        completed = {
            fields["command_id"]: fields
            for event, fields in events if event == "dispatcher_command_completed"
        }
        assert started["second"]["queue_wait_ms"] >= 15
        assert completed["second"]["queue_wait_ms"] >= 15
        assert completed["second"]["dispatcher_total_ms"] >= 15
        assert completed["second"]["outcome"] == "completed"
        assert completed["second"]["queue_depth_at_enqueue"] == 0
        await dispatcher.close()

    asyncio.run(scenario())


def test_dispatcher_telemetry_records_queue_full_and_stop_supersession():
    class BlockingBlind:
        config = FakeConfig()

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, command):
            if command.id == "first-stop":
                self.started.set()
                await self.release.wait()
            return CommandResult(command.id, "blind", Outcome.COMPLETED)

        async def close(self):
            pass

    async def scenario():
        events = []
        dispatcher = BlindDispatcher(
            BlockingBlind(), max_queue=1,
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        first_stop = asyncio.create_task(dispatcher.submit(
            BlindCommand("first-stop", CommandKind.STOP)
        ))
        await dispatcher.blind.started.wait()
        queued = asyncio.create_task(dispatcher.submit(
            BlindCommand("queued", CommandKind.FULL_CLOSE)
        ))
        await asyncio.sleep(0)
        try:
            await dispatcher.submit(BlindCommand("overflow", CommandKind.FAVORITE))
        except Exception as exc:
            assert "queue full" in str(exc)
        else:
            raise AssertionError("overflow should be rejected")
        stop = asyncio.create_task(dispatcher.submit(
            BlindCommand("stop", CommandKind.STOP)
        ))
        await asyncio.sleep(0)
        dispatcher.blind.release.set()
        await asyncio.gather(first_stop, stop, queued)

        assert any(event == "dispatcher_queue_full" and fields["command_id"] == "overflow"
                   for event, fields in events)
        superseded = [fields for event, fields in events
                      if event == "dispatcher_command_completed" and fields["superseded"]]
        assert {item["command_id"] for item in superseded} == {"queued"}
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
        queued = asyncio.create_task(dispatcher.submit(
            BlindCommand("close", CommandKind.FULL_CLOSE)
        ))
        await blind.started.wait()

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


def test_conflicting_command_stops_motion_and_discards_both_requests():
    class BlockingBlind(FakeBlind):
        def __init__(self):
            self.move_started = asyncio.Event()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()
            self.calls = []

        async def execute(self, command):
            self.calls.append(command)
            if command.id == "move":
                self.move_started.set()
                await asyncio.Event().wait()
            if command.kind is CommandKind.STOP:
                self.stop_started.set()
                await self.release_stop.wait()
            return CommandResult(command.id, "blind", Outcome.COMPLETED)

    async def scenario():
        blind = BlockingBlind()
        dispatcher = BlindDispatcher(blind, dedupe_size=1)
        original = BlindCommand("move", CommandKind.FULL_OPEN)
        running = asyncio.create_task(dispatcher.submit(original))
        await blind.move_started.wait()
        duplicate = asyncio.create_task(dispatcher.submit(original))
        await asyncio.sleep(0)
        assert [command.kind for command in blind.calls] == [CommandKind.FULL_OPEN]

        conflict = BlindCommand("conflict", CommandKind.SET_POSITION, position=70)
        stopping = asyncio.create_task(dispatcher.submit(conflict))
        await asyncio.wait_for(blind.stop_started.wait(), 1)
        burst_result = await dispatcher.submit(BlindCommand("burst", CommandKind.FULL_CLOSE))
        assert burst_result.outcome is Outcome.FAILED
        assert not stopping.done()
        duplicate_conflict = asyncio.create_task(dispatcher.submit(conflict))
        await asyncio.sleep(0)
        assert not duplicate_conflict.done()
        blind.release_stop.set()

        original_result, duplicate_result, conflict_result, duplicate_stop_result = await asyncio.gather(
            running, duplicate, stopping, duplicate_conflict,
        )
        for result in (original_result, duplicate_result, conflict_result,
                       duplicate_stop_result, burst_result):
            assert result.outcome is Outcome.FAILED
            assert result.error == "superseded by stop command conflict"
        assert blind.calls[1].kind is CommandKind.STOP
        assert blind.calls[1].position is None
        assert len(blind.calls) == 2
        assert (await dispatcher.submit(conflict)).outcome is Outcome.DUPLICATE
        subsequent = await dispatcher.submit(BlindCommand("next", CommandKind.FULL_CLOSE))
        assert subsequent.outcome is Outcome.COMPLETED
        await dispatcher.close()

    asyncio.run(scenario())


def test_superseded_result_does_not_hide_newer_running_stop():
    class BlockingBlind:
        config = FakeConfig()

        def __init__(self):
            self.move_started = asyncio.Event()
            self.stop_started = asyncio.Event()
            self.release_stop = asyncio.Event()

        async def execute(self, command):
            if command.kind is CommandKind.STOP:
                self.stop_started.set()
                await self.release_stop.wait()
                return CommandResult(command.id, "blind", Outcome.COMPLETED)
            self.move_started.set()
            await asyncio.Event().wait()

        async def close(self):
            pass

    async def scenario():
        blind = BlockingBlind()
        dispatcher = BlindDispatcher(blind)
        controller = Controller({"blind": dispatcher}, ())
        moving = asyncio.create_task(controller.submit_command(
            "blind", "open", command_id="move", target_type="blind"
        ))
        await blind.move_started.wait()

        stopping = asyncio.create_task(controller.submit_command(
            "blind", "stop", command_id="halt", target_type="blind"
        ))
        await blind.stop_started.wait()
        move_result = await moving
        assert move_result.outcome is Outcome.FAILED
        assert controller.diagnostics_for("blind")["last_command_status"] == "running"
        assert controller.diagnostics_for("blind")["active_command_id"] == "halt"

        blind.release_stop.set()
        stop_result = await stopping
        assert stop_result.outcome is Outcome.COMPLETED
        assert controller.diagnostics_for("blind")["last_command_status"] == "completed"
        assert controller.diagnostics_for("blind")["active_command_id"] is None
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
