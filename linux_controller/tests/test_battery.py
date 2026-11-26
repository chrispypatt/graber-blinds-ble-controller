import asyncio
import json
import stat

from graber.battery import BatteryHistory
from graber.blind import PositionState


def test_repeated_high_to_zero_reports_preserve_level_and_its_age_until_recovery():
    state = PositionState()
    state.observe_battery(95, 1)
    for index in range(1, 55):
        state.observe_battery(0, index + 1)
        assert state.battery == 95
        assert state.battery_accepted_at == 1
        assert state.battery_read_at == index + 1
        assert state.battery_zero_reads == index
        assert state.battery_status == ("zero_unconfirmed" if index == 1 else "zero_repeated")
    state.observe_battery(91, 56)
    assert state.battery_fields() == {
        "battery": 91, "battery_raw": 91, "battery_read_at": 56, "battery_accepted_at": 56,
        "battery_zero_reads": 0, "battery_status": "reported",
    }


def test_unknown_zero_and_low_to_empty_are_distinct():
    state = PositionState()
    assert state.battery_status == "unread"
    state.observe_battery(0, 1)
    state.observe_battery(0, 2)
    assert state.battery is None and state.battery_accepted_at is None
    assert state.battery_status == "zero_repeated"
    state.observe_battery(11, 3)
    state.observe_battery(0, 4)
    assert state.battery == 11 and state.battery_status == "zero_unconfirmed"
    state.observe_battery(10, 5)
    state.observe_battery(0, 6)
    assert state.battery == 0 and state.battery_status == "reported"
    assert state.battery_accepted_at == 6


def test_battery_history_survives_concurrent_blind_updates_and_preserves_observation_times(tmp_path):
    path = tmp_path / "battery.json"
    history = BatteryHistory(path)
    left, right = PositionState(), PositionState()
    left.observe_battery(95, 1)
    left.observe_battery(0, 2)
    right.observe_battery(0, 3)

    async def save_both():
        history.record("aa", left)
        history.record("bb", right)
        await history.flush()
    asyncio.run(save_both())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restored = BatteryHistory(path)
    for address, original in (("AA", left), ("BB", right)):
        state = PositionState()
        restored.restore(address, state)
        assert state.battery_fields() == original.battery_fields()
        assert state.updated_at is None
    replacement = PositionState()
    restored.restore("CC", replacement)
    assert replacement.battery is None and replacement.battery_status == "unread"


def test_invalid_history_entry_does_not_supply_a_baseline(tmp_path):
    path = tmp_path / "battery.json"
    state = PositionState()
    state.observe_battery(0, 5)
    bad_record = {**state.battery_fields(), "battery": 800, "battery_accepted_at": 5}
    path.write_text(json.dumps({"version": 1, "blinds": {"AA": bad_record}}))
    path.chmod(0o600)
    BatteryHistory(path).restore("AA", state)
    assert state.battery is None


def test_unsupported_history_file_is_preserved(tmp_path):
    path = tmp_path / "battery.json"
    contents = '{"version":2,"blinds":{}}'
    path.write_text(contents)
    path.chmod(0o600)
    history = BatteryHistory(path)
    state = PositionState()
    state.observe_battery(80, 1)
    history.record("AA", state)
    asyncio.run(history.flush())
    assert path.read_text() == contents
