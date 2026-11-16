import asyncio
import time

from graber.adapter_manager import AdapterManager, ResolvedAdapter
from graber.ble_transport import DiscoveredDevice
from graber.blind import BlindCommand, BlindController, Outcome
from graber.config import AdapterConfig, BlindConfig, RetryConfig
from graber.protocol import (
    AUTH_CHARACTERISTIC, BATTERY_CHARACTERISTIC, BOTTOM_POSITION_CHARACTERISTIC, BOTTOM_TARGET_CHARACTERISTIC,
    TOP_POSITION_CHARACTERISTIC, CommandKind, Rail,
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


def make_controller(connection, *, failures=0, feedback_timeout=0.08, dual=False):
    adapter = AdapterConfig("external", "AA:BB:CC:DD:EE:FF", max_connections=2)
    async def resolver(_):
        return [ResolvedAdapter("external", adapter.controller_address, "hci9", "1-1.2")]
    manager = AdapterManager((adapter,), resolver)
    blind = BlindConfig("blind", "Blind", "11:22:33:44:55:66", dual, "0123456", "external")
    retry = RetryConfig(attempts=3, connect_timeout_s=0.2, gatt_timeout_s=0.2,
                        feedback_timeout_s=feedback_timeout, backoff_s=0, jitter_s=0)
    return BlindController(blind, manager, FakeTransport(connection, failures), retry, warm_connection_s=30)


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
        assert [write for write in connection.writes if write[1] == b"\x14"] == [
            (AUTH_CHARACTERISTIC, b"\x14")
        ]
        assert result.attempts == 1
        await blind.close()
    asyncio.run(scenario())


def test_connection_failure_retries_before_any_command_write():
    async def scenario():
        connection = FakeConnection()
        blind = make_controller(connection, failures=1)
        result = await blind.execute(BlindCommand("retry", CommandKind.SET_POSITION, Rail.BOTTOM, 42))
        assert result.outcome is Outcome.COMPLETED
        assert result.attempts == 2
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
        connection = FakeConnection(reads={
            BOTTOM_POSITION_CHARACTERISTIC: b"\x1f",
            TOP_POSITION_CHARACTERISTIC: b"\x47",
            BATTERY_CHARACTERISTIC: b"\x58",
        })
        blind = make_controller(connection, dual=True)
        state = await blind.refresh_status()
        assert (state.bottom, state.top, state.battery) == (31, 71, 88)
        assert connection.writes == [(AUTH_CHARACTERISTIC, b"0123456")]
        assert blind._transport.connect_calls == 1
        await blind.close()
    asyncio.run(scenario())
