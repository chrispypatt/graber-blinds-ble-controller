import asyncio

from graber.main import publish_command_lifecycle, refresh_battery_periodically


class FakeBlind:
    def __init__(self, *, battery=None, interval_s=0.03, error=None):
        self.config = type("Config", (), {
            "id": "test_blind",
            "battery_interval_s": interval_s,
        })()
        self.state = type("State", (), {"battery": battery})()
        self.battery_read_since_start = battery is not None
        self.error = error
        self.calls = 0
        self.called = asyncio.Event()
        self.cancelled = False

    async def refresh_status(self):
        self.calls += 1
        self.called.set()
        if self.error:
            raise self.error
        self.state.battery = 75
        return self.state


async def cancel_task(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_unknown_battery_refreshes_immediately():
    async def scenario():
        blind = FakeBlind()
        task = asyncio.create_task(refresh_battery_periodically(
            blind, minimum_interval_s=0.001,
        ))
        try:
            await asyncio.wait_for(blind.called.wait(), 0.1)
            assert blind.calls == 1
            assert blind.state.battery == 75
        finally:
            await cancel_task(task)
    asyncio.run(scenario())


def test_cached_battery_still_refreshes_on_startup():
    async def scenario():
        blind = FakeBlind(battery=95, interval_s=3600)
        blind.battery_read_since_start = False
        task = asyncio.create_task(refresh_battery_periodically(blind))
        try:
            await asyncio.wait_for(blind.called.wait(), 0.1)
            assert blind.calls == 1
        finally:
            await cancel_task(task)

    asyncio.run(scenario())


def test_known_battery_waits_for_interval():
    async def scenario():
        blind = FakeBlind(battery=70, interval_s=0.03)
        task = asyncio.create_task(refresh_battery_periodically(
            blind, minimum_interval_s=0.001,
        ))
        try:
            await asyncio.sleep(0.005)
            assert blind.calls == 0
            await asyncio.wait_for(blind.called.wait(), 0.1)
            assert blind.calls == 1
        finally:
            await cancel_task(task)
    asyncio.run(scenario())


def test_failure_is_isolated_and_does_not_retry_tightly():
    async def scenario():
        reports = []
        failing = FakeBlind(interval_s=0.04, error=RuntimeError("offline"))
        healthy = FakeBlind()
        failed_task = asyncio.create_task(refresh_battery_periodically(
            failing, lambda event, fields: reports.append((event, fields)),
            minimum_interval_s=0.001,
        ))
        healthy_task = asyncio.create_task(refresh_battery_periodically(
            healthy, minimum_interval_s=0.001,
        ))
        try:
            await asyncio.wait_for(asyncio.gather(
                failing.called.wait(), healthy.called.wait(),
            ), 0.1)
            await asyncio.sleep(0.01)
            assert failing.calls == 1
            assert healthy.calls == 1
            assert reports == [("battery_refresh_failed", {
                "blind_id": "test_blind",
                "error": "RuntimeError: offline",
            })]
        finally:
            await cancel_task(failed_task)
            await cancel_task(healthy_task)
    asyncio.run(scenario())


def test_cancellation_interrupts_in_flight_refresh():
    class BlockingBlind(FakeBlind):
        async def refresh_status(self):
            self.calls += 1
            self.called.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario():
        blind = BlockingBlind()
        task = asyncio.create_task(refresh_battery_periodically(
            blind, minimum_interval_s=0.001,
        ))
        await asyncio.wait_for(blind.called.wait(), 0.1)
        await cancel_task(task)
        assert blind.cancelled
    asyncio.run(scenario())


class FakeController:
    def __init__(self):
        self.events = []

    def record_lifecycle(self, blind_id, status, command_id):
        self.events.append((blind_id, status, command_id))


def test_final_write_ack_transitions_to_awaiting_feedback():
    controller = FakeController()
    publish_command_lifecycle(controller, "gatt_write_acknowledged", {
        "blind_id": "dining_left",
        "command_id": "move-1",
        "step": 2,
        "steps": 2,
    })
    assert controller.events == [
        ("dining_left", "write_acknowledged", "move-1"),
        ("dining_left", "awaiting_feedback", "move-1"),
    ]


def test_non_write_telemetry_does_not_change_command_lifecycle():
    controller = FakeController()
    publish_command_lifecycle(controller, "movement_observed", {
        "blind_id": "dining_left",
        "command_id": "move-1",
    })
    assert controller.events == []
