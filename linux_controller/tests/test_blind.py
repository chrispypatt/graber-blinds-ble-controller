import asyncio
import json
import time

import pytest

from graber.bluetooth.adapter_manager import AdapterManager, ResolvedAdapter
from graber.bluetooth.ble_transport import DiscoveredDevice
from graber.control.blind import BlindController
from graber.control.commands import BlindCommand, Outcome
from graber.config import AdapterConfig, BlindConfig, RetryConfig
from graber.control.dispatcher import BlindDispatcher
from graber.control.group_controller import Controller
from graber.main import publish_command_lifecycle
from graber.bluetooth.protocol import (
    AUTH_CHARACTERISTIC, BATTERY_CHARACTERISTIC, BOTTOM_POSITION_CHARACTERISTIC, BOTTOM_TARGET_CHARACTERISTIC,
    EXECUTE_CHARACTERISTIC, TOP_POSITION_CHARACTERISTIC, TOP_TARGET_CHARACTERISTIC, CommandKind, Rail,
)


class FakeConnection:
    def __init__(self, reads=None, execute=True, fail_on_write=None, delay_on_write=None,
                 disconnect_error=None):
        self.is_up = True
        self.writes = []
        self.notifications = {}
        self.reads = ({BOTTOM_POSITION_CHARACTERISTIC: b"\x2a"} if reads is None else reads)
        self.execute = execute
        self.fail_on_write = fail_on_write
        self.delay_on_write = delay_on_write or {}
        self.write_times = []
        self.disconnect_error = disconnect_error

    @property
    def connected(self):
        return self.is_up

    async def start_notify(self, characteristic, callback):
        self.notifications[characteristic] = callback

    async def write(self, characteristic, value):
        self.writes.append((characteristic, value))
        self.write_times.append(time.monotonic())
        await asyncio.sleep(self.delay_on_write.get(len(self.writes), 0))
        if len(self.writes) == self.fail_on_write:
            raise RuntimeError("simulated write failure")

    async def read(self, characteristic):
        return self.reads[characteristic]

    async def disconnect(self):
        self.is_up = False
        if self.disconnect_error:
            raise self.disconnect_error

    def has_characteristic(self, characteristic):
        return self.execute


class FakeTransport:
    def __init__(self, connection, connect_failures=0):
        self.connection = connection
        self.connect_failures = connect_failures
        self.connect_calls = 0

    async def discover(self, address, adapter, timeout_s):
        return DiscoveredDevice(object(), address, "RRBZ_TEST", -55, {1: b"\x00\x00\x2a\x64"})

    async def connect(self, device, adapter, timeout_s):
        self.connect_calls += 1
        if self.connect_calls <= self.connect_failures:
            raise RuntimeError("temporary BlueZ failure")
        return self.connection


def make_controller(connection, *, failures=0, feedback_timeout=0.08, dual=False,
                    telemetry=None):
    adapter = AdapterConfig("external", "AA:BB:CC:DD:EE:FF", max_connections=2)
    async def resolver(_):
        return [ResolvedAdapter("external", adapter.controller_address, "hci9", "1-1.2")]
    manager = AdapterManager((adapter,), resolver)
    blind = BlindConfig("blind", "Blind", "11:22:33:44:55:66", dual, "0123456", "external")
    retry = RetryConfig(attempts=3, connect_timeout_s=0.2, gatt_timeout_s=0.2,
                        feedback_timeout_s=feedback_timeout, backoff_s=0, jitter_s=0)
    return BlindController(
        blind, manager, FakeTransport(connection, failures), retry,
        warm_connection_s=30, telemetry=telemetry,
    )


def test_connect_subscribe_auth_then_verified_command():
    async def scenario():
        connection = FakeConnection()
        blind = make_controller(connection)
        result = await blind.execute(BlindCommand("one", CommandKind.SET_POSITION, Rail.BOTTOM, 42))
        assert result.outcome is Outcome.COMPLETED
        assert connection.writes == [
            (AUTH_CHARACTERISTIC, b"0123456"),
            (BOTTOM_TARGET_CHARACTERISTIC, b"\x2a"),
            (AUTH_CHARACTERISTIC, b"\x14"),
        ]
        assert BOTTOM_POSITION_CHARACTERISTIC in connection.notifications
        assert result.adapter_id == "external"
        await blind.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_write_fails", [False, True])
def test_dispatcher_conflict_cancels_target_write_and_sends_dual_rail_stop(stop_write_fails):
    class BlockingTargetConnection(FakeConnection):
        def __init__(self):
            super().__init__(reads={
                BOTTOM_POSITION_CHARACTERISTIC: b"\x2a",
                TOP_POSITION_CHARACTERISTIC: b"\x64",
            }, fail_on_write=4 if stop_write_fails else None)
            self.target_started = asyncio.Event()
            self.target_cancelled = False

        async def write(self, characteristic, value):
            await super().write(characteristic, value)
            if characteristic == BOTTOM_TARGET_CHARACTERISTIC:
                self.target_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.target_cancelled = True
                    raise
            elif characteristic == TOP_TARGET_CHARACTERISTIC:
                self.notifications[TOP_POSITION_CHARACTERISTIC](value)

    async def scenario():
        connection = BlockingTargetConnection()
        blind = make_controller(
            connection, dual=True, feedback_timeout=0.5,
            telemetry=lambda event, fields: publish_command_lifecycle(controller, event, fields),
        )
        dispatcher = BlindDispatcher(blind)
        controller = Controller({"blind": dispatcher}, ())
        try:
            movement = asyncio.create_task(controller.submit_command(
                "blind", "set_bottom_position", 42, command_id="first",
            ))
            await asyncio.wait_for(connection.target_started.wait(), 1)
            blind._feedback._last_change = time.monotonic() - 3
            conflict = await asyncio.wait_for(controller.submit_command(
                "blind", "set_top_position", 10, command_id="second",
            ), 2)
            original = await asyncio.wait_for(movement, 1)
            assert original.outcome is Outcome.FAILED
            assert original.error == "superseded by stop command second"
            assert connection.target_cancelled
            assert connection.writes[:4] == [
                (AUTH_CHARACTERISTIC, b"0123456"),
                (BOTTOM_TARGET_CHARACTERISTIC, b"\x2a"),
                (AUTH_CHARACTERISTIC, b"\x10"),
                (EXECUTE_CHARACTERISTIC, b"\x10"),
            ]

            if stop_write_fails:
                assert conflict.outcome is Outcome.COMPLETION_UNCONFIRMED
                assert conflict.error.startswith("STOP was not confirmed")
                assert len(connection.writes) == 4
                assert not connection.connected
                runtime, = await blind._adapters.candidates(blind.config)
                for _ in range(2):
                    await asyncio.wait_for(runtime.connection_slots.acquire(), 1)
                for _ in range(2):
                    runtime.connection_slots.release()
            else:
                assert conflict.outcome is Outcome.COMPLETED
                assert connection.writes[4:] == [
                    (TOP_TARGET_CHARACTERISTIC, b"\x5a"),
                    (EXECUTE_CHARACTERISTIC, b"\x14"),
                ]
                assert connection.connected
            assert controller.diagnostics_for("blind")["active_command_id"] is None
        finally:
            await dispatcher.close()

    asyncio.run(scenario())


def test_command_telemetry_reports_complete_pipeline_without_secrets():
    async def scenario():
        events = []
        blind = make_controller(
            FakeConnection(),
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        result = await blind.execute(BlindCommand(
            "queue-timing", CommandKind.SET_POSITION, Rail.BOTTOM, 42,
            received_at=time.time() - 0.02,
        ))
        acquired = [fields for event, fields in events
                    if event == "cold_connection_slot_acquired"]
        ready = [fields for event, fields in events if event == "connection_ready"]
        write_acks = [fields for event, fields in events
                      if event == "gatt_write_acknowledged"]
        feedback = [fields for event, fields in events if event == "feedback_observed"]
        completed = [fields for event, fields in events if event == "command_completed"]
        assert len(acquired) == 1
        assert acquired[0]["command_id"] == "queue-timing"
        assert acquired[0]["adapter_id"] == "external"
        assert acquired[0]["cold_queue_ms"] >= 0
        assert result.cold_queue_ms == acquired[0]["cold_queue_ms"]
        assert ready[0]["operation"] == "command"
        assert ready[0]["notify_setup_ms"] >= 0
        assert ready[0]["gatt_setup_ms"] >= ready[0]["notify_setup_ms"]
        assert ready[0]["auth_ms"] >= 0
        assert len(write_acks) == 2
        assert all(item["write_ack_ms"] >= 0 for item in write_acks)
        assert [item["effectful"] for item in write_acks] == [False, True]
        assert len(feedback) == 1
        assert feedback[0]["feedback_first_sample_ms"] >= 0
        terminal = completed[0]
        assert terminal["controller_queue_ms"] >= 15
        assert terminal["cold_queue_ms"] == result.cold_queue_ms
        assert terminal["discovery_ms"] >= 0
        assert terminal["connect_ms"] >= 0
        assert terminal["notify_setup_ms"] >= 0
        assert terminal["gatt_setup_ms"] >= terminal["notify_setup_ms"]
        assert terminal["auth_ms"] >= 0
        assert terminal["command_ack_ms"] >= 0
        assert terminal["feedback_first_sample_ms"] >= 0
        assert terminal["feedback_ms"] >= terminal["feedback_first_sample_ms"]
        assert terminal["warm_connection"] is False
        assert "0123456" not in json.dumps(events, default=str)
        await blind.close()
        disconnected = [fields for event, fields in events if event == "disconnected"]
        assert disconnected[-1]["reason"] == "shutdown"
        assert disconnected[-1]["command_count"] == 1

    asyncio.run(scenario())


def test_warm_connection_reuse_is_measured_without_second_connect():
    async def scenario():
        events = []
        blind = make_controller(
            FakeConnection(),
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        first = await blind.execute(
            BlindCommand("first", CommandKind.SET_POSITION, Rail.BOTTOM, 42)
        )
        second = await blind.execute(
            BlindCommand("second", CommandKind.SET_POSITION, Rail.BOTTOM, 42)
        )
        assert first.warm_connection is False
        assert second.warm_connection is True
        assert second.connection_age_ms is not None
        assert second.connection_age_ms >= 0
        assert blind._transport.connect_calls == 1
        reused = [fields for event, fields in events if event == "connection_reused"]
        assert reused[-1]["command_id"] == "second"
        assert reused[-1]["operation"] == "command"
        await blind.close()

    asyncio.run(scenario())


def test_post_command_battery_reads_once_per_connection_even_after_a_recent_refresh():
    async def scenario():
        events = []
        reads = {
            BOTTOM_POSITION_CHARACTERISTIC: bytes([42]),
            BATTERY_CHARACTERISTIC: bytes([88]),
        }
        blind = make_controller(
            FakeConnection(reads=reads),
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        try:
            assert (await blind.refresh_status()).battery == 88
            blind._transport.connection = FakeConnection(
                reads={**reads, BATTERY_CHARACTERISTIC: bytes([89])},
            )
            for command_id in ("open", "warm"):
                result = await blind.execute(
                    BlindCommand(command_id, CommandKind.SET_POSITION, Rail.BOTTOM, 42)
                )
                assert result.outcome is Outcome.COMPLETED
            assert blind.state.battery == 89
            assert blind._transport.connect_calls == 2
            battery_reads = [
                (fields["source"], fields.get("command_id"), fields["value"])
                for event, fields in events if event == "battery_read"
            ]
            assert battery_reads == [("status_refresh", None, 88), ("post_command", "open", 89)]
            completed_index = next(i for i, (event, _) in enumerate(events) if event == "command_completed")
            post_command_index = next(
                i for i, (event, fields) in enumerate(events)
                if event == "battery_read" and fields["source"] == "post_command"
            )
            assert completed_index < post_command_index
        finally:
            await blind.close()
    asyncio.run(scenario())


def test_disconnect_cleanup_error_does_not_mask_completed_command():
    async def scenario():
        connection = FakeConnection(disconnect_error=EOFError())
        blind = make_controller(connection)
        result = await blind.execute(BlindCommand("one", CommandKind.SET_POSITION, Rail.BOTTOM, 42))
        assert result.outcome is Outcome.COMPLETED
        await blind.close()
        assert not connection.connected
    asyncio.run(scenario())


def test_final_ack_without_feedback_is_not_replayed():
    async def scenario():
        connection = FakeConnection(reads={})
        blind = make_controller(connection, feedback_timeout=0.03)
        result = await blind.execute(BlindCommand("one", CommandKind.SET_POSITION, Rail.BOTTOM, 42))
        assert result.outcome is Outcome.COMPLETION_UNCONFIRMED
        assert result.error_category == "completion_unconfirmed"
        assert result.feedback_ms is not None
        assert [write for write in connection.writes if write[1] == b"\x14"] == [
            (AUTH_CHARACTERISTIC, b"\x14")
        ]
        assert result.attempts == 1
        await blind.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("quiet_interval,direct_top,expected", [
    (0.01, 100, Outcome.COMPLETED),
    (0.01, 99, Outcome.COMPLETION_UNCONFIRMED),
    (1.0, 100, Outcome.COMPLETED),
])
def test_full_open_rechecks_both_rails_after_intermediate_feedback(
    monkeypatch, quiet_interval, direct_top, expected,
):
    monkeypatch.setattr("graber.control.feedback.ENDPOINT_RECHECK_INTERVAL_S", quiet_interval)

    class MissingEndpointNotification(FakeConnection):
        async def write(self, characteristic, value):
            await super().write(characteristic, value)
            if len(self.writes) == 2:
                self.notifications[BOTTOM_POSITION_CHARACTERISTIC](bytes([80]))
                self.notifications[TOP_POSITION_CHARACTERISTIC](bytes([100]))

    async def scenario():
        events = []
        connection = MissingEndpointNotification(reads={
            BOTTOM_POSITION_CHARACTERISTIC: bytes([100]),
            TOP_POSITION_CHARACTERISTIC: bytes([direct_top]),
            BATTERY_CHARACTERISTIC: bytes([88]),
        })
        blind = make_controller(
            connection, dual=True, feedback_timeout=0.08,
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        try:
            result = await blind.execute(BlindCommand("open", CommandKind.FULL_OPEN))
            assert result.outcome is expected
            assert result.attempts == 1
            event_name = "endpoint_verified_by_read" if expected is Outcome.COMPLETED else "feedback_timed_out"
            event = next(fields for name, fields in events if name == event_name)
            assert (event["bottom_raw"], event["top_raw"]) == (100, direct_top)
            if expected is Outcome.COMPLETION_UNCONFIRMED:
                assert event["endpoint_read_top_raw"] == direct_top
        finally:
            await blind.close()
    asyncio.run(scenario())


def test_connection_failure_retries_before_any_command_write():
    async def scenario():
        events = []
        connection = FakeConnection()
        blind = make_controller(
            connection, failures=1,
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        result = await blind.execute(BlindCommand("retry", CommandKind.SET_POSITION, Rail.BOTTOM, 42))
        assert result.outcome is Outcome.COMPLETED
        assert result.attempts == 2
        failed = [fields for event, fields in events if event == "connection_attempt_failed"]
        scheduled = [fields for event, fields in events if event == "command_retry_scheduled"]
        assert failed[0]["error_category"] == "bluez"
        assert scheduled[0]["error_category"] == "bluez"
        assert scheduled[0]["next_attempt"] == 2
        assert scheduled[0]["backoff_ms"] == 0
        await blind.close()
    asyncio.run(scenario())


def test_acknowledged_effectful_first_write_is_never_replayed():
    async def scenario():
        connection = FakeConnection(
            reads={BOTTOM_POSITION_CHARACTERISTIC: b"\x00"}, fail_on_write=3
        )
        blind = make_controller(connection, dual=True)
        result = await blind.execute(BlindCommand("close", CommandKind.FULL_CLOSE))
        assert result.outcome is Outcome.COMPLETION_UNCONFIRMED
        # Auth setup, then exactly one Execute 0x12. The failed trailing Auth
        # must not cause the effectful first command write to be replayed.
        assert sum(value == b"\x12" for _, value in connection.writes) == 1
        assert result.attempts == 1
        await blind.close()
    asyncio.run(scenario())


def test_interwrite_delay_is_measured_from_each_submission():
    async def scenario():
        # Slow ACK for the first target must not shorten the second-target to
        # Execute interval below the verified 70 ms policy.
        connection = FakeConnection(reads={}, delay_on_write={2: 0.09})
        blind = make_controller(connection, dual=True, feedback_timeout=0.01)
        await blind.execute(BlindCommand("stack", CommandKind.STACK_UP))
        assert connection.write_times[3] - connection.write_times[2] >= 0.065
        await blind.close()
    asyncio.run(scenario())


def test_favorite_requires_observed_movement_not_only_fresh_reads():
    async def scenario():
        connection = FakeConnection(reads={
            BOTTOM_POSITION_CHARACTERISTIC: b"\x2a",
            TOP_POSITION_CHARACTERISTIC: b"\x64",
        })
        blind = make_controller(connection, dual=True, feedback_timeout=0.03)
        result = await blind.execute(BlindCommand("favorite", CommandKind.FAVORITE))
        assert result.outcome is Outcome.COMPLETION_UNCONFIRMED
        assert not result.movement_observed
        await blind.close()
    asyncio.run(scenario())


def test_passive_advertisement_updates_positions_without_connecting():
    connection = FakeConnection()
    blind = make_controller(connection, dual=True)
    blind.observe_advertisement(DiscoveredDevice(
        object(), "11:22:33:44:55:66", "RRBZ_TEST", -48,
        {1: b"\x00\x00\x1f\x47"},
    ))
    assert blind.state.bottom == 31
    assert blind.state.top == 71
    assert blind.state.rssi == -48
    assert blind._transport.connect_calls == 0


def test_advertisement_during_command_does_not_complete_without_gatt_feedback():
    class SpoofedPosition(FakeConnection):
        async def write(self, characteristic, value):
            await super().write(characteristic, value)
            if len(self.writes) == 3:
                blind.observe_advertisement(DiscoveredDevice(
                    object(), "11:22:33:44:55:66", "RRBZ_TEST", -48,
                    {1: b"\x00\x00\x64\x64"},
                ))

    async def scenario():
        connection = SpoofedPosition(reads={BOTTOM_POSITION_CHARACTERISTIC: b"\x00"})
        nonlocal blind
        blind = make_controller(connection, feedback_timeout=0.03)
        try:
            result = await blind.execute(BlindCommand(
                "spoofed", CommandKind.SET_POSITION, Rail.BOTTOM, 100,
            ))
            assert result.outcome is Outcome.COMPLETION_UNCONFIRMED
            assert connection.writes[-1] == (AUTH_CHARACTERISTIC, b"\x14")
        finally:
            await blind.close()

    blind = None
    asyncio.run(scenario())


def test_tdbu_advertisement_keeps_both_rail_values_in_raw_coordinates():
    blind = make_controller(FakeConnection(), dual=True)
    blind.observe_advertisement(DiscoveredDevice(
        object(), "11:22:33:44:55:66", "RRBZ_TEST", -48,
        {1: b"\x00\x00\x00\x64"},
    ))
    assert (blind.state.bottom, blind.state.top) == (0, 100)

    blind.observe_advertisement(DiscoveredDevice(
        object(), "11:22:33:44:55:66", "RRBZ_TEST", -48,
        {1: b"\x00\x00\x64\x00"},
    ))
    assert (blind.state.bottom, blind.state.top) == (100, 0)


def test_refresh_status_reads_rails_and_battery_without_movement_write():
    async def scenario():
        events = []
        connection = FakeConnection(reads={
            BOTTOM_POSITION_CHARACTERISTIC: b"\x1f",
            TOP_POSITION_CHARACTERISTIC: b"\x47",
            BATTERY_CHARACTERISTIC: b"\x58",
        })
        blind = make_controller(
            connection, dual=True,
            telemetry=lambda event, fields: events.append((event, fields)),
        )
        state = await blind.refresh_status()
        assert (state.bottom, state.top, state.battery) == (31, 71, 88)
        assert connection.writes == [(AUTH_CHARACTERISTIC, b"0123456")]
        assert blind._transport.connect_calls == 1
        assert not connection.connected
        assert blind._session is None
        disconnected = [fields for event, fields in events if event == "disconnected"]
        assert [fields["reason"] for fields in disconnected] == ["status_refresh_finished"]
        refreshed = [fields for event, fields in events if event == "status_refreshed"]
        assert len(refreshed) == 1
        assert refreshed[0]["total_latency_ms"] >= 0
        assert refreshed[0]["rail_read_ms"] >= 0
        assert refreshed[0]["battery_read_ms"] >= 0
        assert refreshed[0]["connect_ms"] >= 0
        ready = [fields for event, fields in events if event == "connection_ready"]
        assert ready[0]["operation"] == "status_refresh"
        await blind.close()
    asyncio.run(scenario())


def test_status_refresh_borrows_movement_session_without_extending_its_hold():
    async def scenario():
        connection = FakeConnection(reads={
            BOTTOM_POSITION_CHARACTERISTIC: b"\x2a",
            BATTERY_CHARACTERISTIC: b"\x58",
        })
        blind = make_controller(connection)
        try:
            command = BlindCommand("one", CommandKind.SET_POSITION, Rail.BOTTOM, 42)
            assert (await blind.execute(command)).outcome is Outcome.COMPLETED
            assert blind.state.battery == 88
            session = blind._session
            close_task = session.close_task
            last_used = session.last_used

            connection.reads[BATTERY_CHARACTERISTIC] = b"\x59"
            assert (await blind.refresh_status()).battery == 89
            assert connection.connected
            assert blind._transport.connect_calls == 1
            assert blind._session is session
            assert session.close_task is close_task
            assert session.last_used == last_used
        finally:
            await blind.close()
    asyncio.run(scenario())


def test_cancelled_status_refresh_releases_its_connection():
    class BlockingBatteryConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.read_started = asyncio.Event()

        async def read(self, characteristic):
            if characteristic == BATTERY_CHARACTERISTIC:
                self.read_started.set()
                await asyncio.Future()
            return await super().read(characteristic)

    async def scenario():
        connection = BlockingBatteryConnection()
        blind = make_controller(connection)
        refresh = asyncio.create_task(blind.refresh_status())
        await asyncio.wait_for(connection.read_started.wait(), timeout=1)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        assert not connection.connected
        assert blind._session is None
    asyncio.run(scenario())


def test_cancellation_does_not_interrupt_status_disconnect():
    class SlowDisconnectConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.disconnect_started = asyncio.Event()
            self.finish_disconnect = asyncio.Event()

        async def disconnect(self):
            self.disconnect_started.set()
            await self.finish_disconnect.wait()
            await super().disconnect()

    async def scenario():
        connection = SlowDisconnectConnection()
        blind = make_controller(connection)
        refresh = asyncio.create_task(blind.refresh_status())
        await asyncio.wait_for(connection.disconnect_started.wait(), timeout=1)
        refresh.cancel()
        await asyncio.sleep(0)
        assert blind._lock.locked()
        connection.finish_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        assert not connection.connected
    asyncio.run(scenario())
