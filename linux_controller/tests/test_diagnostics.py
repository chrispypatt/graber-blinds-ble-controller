from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from graber.diagnostics import (
    AdapterIdentity,
    JsonlMetricsWriter,
    adapter_as_dict,
    adapter_preflight,
    discover_adapters,
    filter_records,
    load_jsonl,
    load_jsonl_files,
    parse_time_boundary,
    select_adapter,
    summarize_operations,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_discovers_usb_identity_independently_of_hci_number(tmp_path: Path) -> None:
    usb = tmp_path / "devices" / "pci0000:00" / "usb1" / "1-1.3"
    hci_device = usb / "1-1.3:1.0" / "bluetooth" / "hci7"
    hci_device.mkdir(parents=True)
    for name, value in {
        "idVendor": "0a12",
        "idProduct": "0001",
        "manufacturer": "Feasycom",
        "product": "FSC-BP119",
        "serial": "adapter-A",
    }.items():
        (usb / name).write_text(value)
    classes = tmp_path / "class" / "bluetooth"
    classes.mkdir(parents=True)
    entry = classes / "hci7"
    entry.mkdir()
    (entry / "address").write_text("AA:BB:CC:DD:EE:01")
    (entry / "device").symlink_to(hci_device)

    def runner(command):
        if tuple(command) == ("bluetoothctl", "list"):
            return _completed("Controller AA:BB:CC:DD:EE:01 test [default]\n")
        return _completed("\tPowered: yes\n")

    adapter = discover_adapters(classes, runner=runner)[0]
    assert adapter.hci_name == "hci7"
    assert adapter.usb_path == "1-1.3"
    assert adapter.usb_serial == "adapter-A"
    assert adapter.powered is True
    assert adapter.is_usb


def test_discovers_address_from_bluez_when_sysfs_omits_it(tmp_path: Path) -> None:
    usb = tmp_path / "devices" / "usb1" / "1-2"
    hci_device = usb / "1-2:1.0" / "bluetooth" / "hci4"
    hci_device.mkdir(parents=True)
    (usb / "idVendor").write_text("0a12")
    (usb / "idProduct").write_text("0001")
    classes = tmp_path / "class" / "bluetooth"
    classes.mkdir(parents=True)
    entry = classes / "hci4"
    entry.mkdir()
    (entry / "device").symlink_to(hci_device)

    def runner(command):
        command = tuple(command)
        if command == ("bluetoothctl", "list"):
            return _completed("")
        if command[-1] == "Address":
            return _completed('s "AA:BB:CC:DD:EE:04"\n')
        if command[-1] == "Powered":
            return _completed("b true\n")
        return _completed(returncode=1)

    adapter = discover_adapters(classes, runner=runner)[0]
    assert adapter.address == "AA:BB:CC:DD:EE:04"
    assert adapter.powered is True
    assert adapter.usb_path == "1-2"


def test_select_adapter_requires_stable_selector_and_rejects_ambiguity() -> None:
    adapters = [
        AdapterIdentity("hci0", "AA:00:00:00:00:01", True, usb_serial="same", usb_path="1-1"),
        AdapterIdentity("hci2", "AA:00:00:00:00:02", True, usb_serial="same", usb_path="1-2"),
    ]
    assert select_adapter(adapters, address="aa:00:00:00:00:02").hci_name == "hci2"
    assert select_adapter(adapters, usb_serial="same", usb_path="1-1").hci_name == "hci0"
    with pytest.raises(ValueError):
        select_adapter(adapters)
    with pytest.raises(LookupError, match="ambiguous"):
        select_adapter(adapters, usb_serial="same")


def test_preflight_excludes_onboard_controller_and_requires_powered_usb() -> None:
    onboard = AdapterIdentity("hci0", "AA:00:00:00:00:00", True)
    external = AdapterIdentity(
        "hci1", "AA:00:00:00:00:01", True, "0a12", "0001", usb_path="1-1"
    )
    checks = adapter_preflight([onboard, external], expected=1)
    assert all(check.ok for check in checks)
    failed = adapter_preflight([onboard, external], expected=2)
    assert not failed[0].ok


def test_redacted_adapter_output_hides_address_serial_and_sysfs() -> None:
    adapter = AdapterIdentity(
        "hci1",
        "AA:BB:CC:DD:EE:FF",
        True,
        "0a12",
        "0001",
        usb_serial="secret-serial",
        usb_path="1-1",
        sysfs_path="/sys/devices/private",
    )
    output = adapter_as_dict(adapter)
    assert output["address"] == "AA:BB:CC:XX:XX:FF"
    assert output["usb_serial"] == "<redacted>"
    assert output["sysfs_path"] is None
    assert output["stable_keys"] == []


def test_summary_keeps_write_ack_and_observed_movement_distinct() -> None:
    summary = summarize_operations(
        [
            {
                "blind_id": "living_left",
                "attempt": 1,
                "connected": True,
                "write_acknowledged": True,
                "movement_observed": False,
                "completed": False,
                "connection_latency_ms": 1000,
            },
            {
                "blind_id": "living_left",
                "attempt": 2,
                "connected": True,
                "write_acknowledged": True,
                "movement_observed": True,
                "completed": True,
                "connection_latency_ms": 2000,
            },
        ]
    )
    assert summary["gatt_write_acknowledged"] == 1.0
    assert summary["movement_observed"] == 0.5
    assert summary["first_attempt_connection_success"] == 0.5
    assert summary["retry_count"] == 1
    assert summary["latencies"]["connection_latency_ms"]["median"] == 1500
    assert summary["latencies"]["connection_latency_ms"]["p95"] == 1950


def test_load_jsonl_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text(json.dumps({"completed": True}) + "\n")
    assert load_jsonl(path) == [{"completed": True}]
    path.write_text("[]\n")
    with pytest.raises(ValueError, match="not a JSON object"):
        load_jsonl(path)


def test_bounded_writer_records_only_terminal_operations(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    writer = JsonlMetricsWriter(path, max_bytes=1)
    writer.record("unrecorded_debug_noise", {"blind_id": "left"})
    assert not path.exists()
    writer.record("connection_ready", {"blind_id": "left", "gatt_setup_ms": 10})
    assert load_jsonl(path)[0]["gatt_setup_ms"] == 10
    writer.record("command_completed", {"blind_id": "left", "outcome": "completed"})
    assert load_jsonl(path)[0]["event"] == "command_completed"
    writer.record("command_failed", {"blind_id": "right", "outcome": "failed"})
    assert path.with_suffix(".jsonl.1").exists()
    assert load_jsonl(path)[0]["blind_id"] == "right"


def test_connection_events_measure_first_attempt_independently_of_command_result() -> None:
    summary = summarize_operations([
        {"event": "connection_attempt_started", "blind_id": "left", "attempt": 1},
        {"event": "connection_attempt_failed", "blind_id": "left", "attempt": 1},
        {"event": "connection_attempt_started", "blind_id": "left", "attempt": 2},
        {"event": "connection_attempt_succeeded", "blind_id": "left", "attempt": 2},
        {"event": "command_completed", "blind_id": "left", "outcome": "completed", "attempts": 2},
    ])
    assert summary["connection_attempts"] == 2
    assert summary["first_attempt_connection_success"] == 0
    assert summary["eventual_connection_success"] == 1.0


def test_continuous_summary_breaks_down_stages_blinds_adapters_and_failures() -> None:
    start = 1_700_000_000.0
    records = [
        {"event": "command_received", "recorded_at": start, "command_id": "c1",
         "blind_id": "left", "controller_queue_ms": 12},
        {"event": "connection_attempt_started", "recorded_at": start + 1,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 1},
        {"event": "connection_attempt_failed", "recorded_at": start + 2,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 1,
         "error": "TimeoutError: /org/bluez/private-device"},
        {"event": "command_retry_scheduled", "recorded_at": start + 3,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 1},
        {"event": "connection_attempt_started", "recorded_at": start + 4,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 2},
        {"event": "connection_attempt_succeeded", "recorded_at": start + 5,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 2,
         "cold_queue_ms": 300, "discovery_ms": 2, "connect_ms": 2500},
        {"event": "connection_ready", "recorded_at": start + 6,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempt": 2,
         "notify_setup_ms": 60, "gatt_setup_ms": 170, "auth_ms": 100,
         # Duplicate connection fields must not create extra samples.
         "connect_ms": 2500},
        {"event": "feedback_observed", "recorded_at": start + 7,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a",
         "feedback_first_sample_ms": 700},
        {"event": "command_completed", "recorded_at": start + 8,
         "command_id": "c1", "blind_id": "left", "adapter_id": "a", "attempts": 2,
         "outcome": "completed", "movement_observed": True, "command_ack_ms": 150,
         "movement_start_ms": 800, "feedback_ms": 900, "total_latency_ms": 5000},
        {"event": "group_command_received", "recorded_at": start + 86400,
         "command_id": "g1", "group_id": "main", "member_count": 1},
        {"event": "group_command_completed", "recorded_at": start + 86401,
         "command_id": "g1", "group_id": "main", "duration_ms": 5100,
         "movement_start_spread_ms": 0, "dispatch_spread_ms": 4, "failed_count": 0},
    ]
    summary = summarize_operations(records)
    assert summary["period"]["records"] == len(records)
    assert len(summary["daily"]) == 2
    assert summary["first_attempt_connection_success"] == 0
    assert summary["eventual_connection_success"] == 1
    assert summary["retry_count"] == 1
    assert summary["failures_by_category"] == {"TimeoutError": 1}
    assert "/org/bluez" not in json.dumps(summary)
    assert summary["latencies"]["connection_latency_ms"] == {
        "samples": 1, "median": 2500, "p95": 2500, "max": 2500,
    }
    assert summary["by_blind"]["left"]["latencies"]["cold_queue_ms"]["max"] == 300
    assert summary["by_adapter"]["a"]["connection_attempts"] == 2
    assert summary["groups"]["by_group"]["main"]["samples"] == 1
    assert summary["slowest_by_stage"]["connection_latency_ms"][0]["blind_id"] == "left"
    assert summary["slowest_by_stage"]["authentication_latency_ms"][0]["duration_ms"] == 100


def test_loads_rotated_and_current_logs_then_filters_by_time(tmp_path: Path) -> None:
    current = tmp_path / "metrics.jsonl"
    rotated = tmp_path / "metrics.jsonl.1"
    rotated.write_text(json.dumps({"event": "old", "recorded_at": 100}) + "\n")
    current.write_text(json.dumps({"event": "new", "recorded_at": 200}) + "\n")
    records = load_jsonl_files([current])
    assert [row["event"] for row in records] == ["old", "new"]
    assert [row["event"] for row in filter_records(records, since=150)] == ["new"]
    assert [row["event"] for row in filter_records(records, until=150)] == ["old"]
    assert parse_time_boundary("1970-01-01T00:01:40Z") == 100


def test_mixed_legacy_and_stage_records_keep_old_sample_without_new_duplicates() -> None:
    summary = summarize_operations([
        {"event": "command_completed", "command_id": "legacy", "blind_id": "left",
         "connect_ms": 1000, "outcome": "completed"},
        {"event": "connection_attempt_succeeded", "command_id": "modern",
         "blind_id": "right", "attempt": 1, "connect_ms": 2000},
        {"event": "connection_ready", "command_id": "modern", "blind_id": "right",
         "connect_ms": 2000, "gatt_setup_ms": 50},
        {"event": "command_completed", "command_id": "modern", "blind_id": "right",
         "connect_ms": 2000, "gatt_setup_ms": 50, "outcome": "completed"},
    ])
    timing = summary["latencies"]["connection_latency_ms"]
    assert timing["samples"] == 2
    assert timing["median"] == 1500


@pytest.mark.asyncio
async def test_runtime_recovers_moved_adapter_and_preserves_logical_id(monkeypatch):
    from graber.config import AdapterConfig
    from graber.diagnostics import resolve_configured_adapters

    adapter = AdapterIdentity("hci1", "AA:BB:CC:DD:EE:01", False, "0a12", "0001", usb_path="3-1")
    monkeypatch.setattr("graber.diagnostics.discover_adapters", lambda: [adapter])
    attempts = []

    def recover(candidate):
        attempts.append(candidate)
        return True

    default = AdapterConfig("living", adapter.address, "1-2")
    assert await resolve_configured_adapters((default,), recover_power=recover) == []
    assert attempts == []
    opted_in = AdapterConfig("living", adapter.address, "1-2", auto_recover_power=True)
    [resolved] = await resolve_configured_adapters((opted_in,), recover_power=recover)
    assert (resolved.id, resolved.device, resolved.usb_path) == ("living", "hci1", "3-1")
    assert attempts == [adapter]

    duplicate = AdapterIdentity("hci2", adapter.address, True, "0a12", "0001", usb_path="2-1")
    monkeypatch.setattr("graber.diagnostics.discover_adapters", lambda: [adapter, duplicate])
    assert await resolve_configured_adapters((opted_in,), recover_power=recover) == []
    assert attempts == [adapter]
