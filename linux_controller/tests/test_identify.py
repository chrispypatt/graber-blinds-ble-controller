import stat
from pathlib import Path

from graber.diagnostics import AdapterIdentity
from graber.setup.identify_cli import (
    MotorObservation, decode_candidate_access_code, update_observation,
    write_private_inventory,
)


class Advertisement:
    rssi = -61
    manufacturer_data = {123: b"\xaa\xbb\x1f\x47"}
    service_uuids = ["service-b", "service-a"]


def test_observation_extracts_candidate_rails_without_claiming_verified_type():
    item = MotorObservation("RRBZ_TEST", "11:22:33:44:55:66")
    update_observation(item, Advertisement())
    assert (item.bottom_raw, item.top_raw) == (31, 71)
    assert item.motor_type_candidate == "tdbu_candidate"
    assert item.rssi_median == -61


def test_zero_top_position_does_not_claim_single_or_tdbu_type():
    item = MotorObservation("RRBZ_TEST", "11:22:33:44:55:66", top_raw=0)
    assert item.motor_type_candidate == "unknown"

    item.top_raw = None
    assert item.motor_type_candidate == "unknown"


def test_access_code_requires_exact_ascii_digits():
    assert decode_candidate_access_code(b"0123456") == "0123456"
    assert decode_candidate_access_code(b"\x10") is None
    assert decode_candidate_access_code("１２３４５６７".encode()) is None


def test_private_capture_is_mode_0600_and_contains_full_identity(tmp_path: Path):
    adapter = AdapterIdentity(
        "hci7", "AA:BB:CC:DD:EE:FF", True, "1234", "5678",
        usb_serial="adapter-a", usb_path="1-2.3",
    )
    item = MotorObservation("RRBZ_TEST", "11:22:33:44:55:66", candidate_access_code="0123456")
    path = tmp_path / "inventory.json"
    write_private_inventory(path, adapter, [item])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    content = path.read_text()
    assert "11:22:33:44:55:66" in content
    assert "0123456" in content
