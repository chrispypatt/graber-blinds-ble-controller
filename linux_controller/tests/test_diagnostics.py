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
    load_jsonl,
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
    writer.record("connection_ready", {"blind_id": "left"})
    assert not path.exists()
    writer.record("command_completed", {"blind_id": "left", "outcome": "completed"})
    assert load_jsonl(path)[0]["blind_id"] == "left"
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
