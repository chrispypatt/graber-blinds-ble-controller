"""Read-only Graber motor identification and private inventory capture.

The tool never writes a GATT characteristic and never resets, pairs, excludes,
or changes a blind.  Normal output redacts BLE identities.  Full identity and
optional candidate access-code reads require an explicit private capture file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graber.diagnostics import AdapterIdentity, discover_adapters, select_adapter
from graber.bluetooth.protocol import AUTH_CHARACTERISTIC, BATTERY_CHARACTERISTIC, parse_manufacturer_data


FIRMWARE_CHARACTERISTIC = "00002a26-0000-1000-8000-00805f9b34fb"


@dataclass
class MotorObservation:
    name: str
    address: str
    rssi_samples: list[int] = field(default_factory=list)
    bottom_raw: int | None = None
    top_raw: int | None = None
    manufacturer_data: dict[str, str] = field(default_factory=dict)
    service_uuids: list[str] = field(default_factory=list)
    firmware: str | None = None
    battery: int | None = None
    candidate_access_code: str | None = field(default=None, repr=False)
    inspection_errors: list[str] = field(default_factory=list)
    device: object | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        opaque = hashlib.sha256(self.address.encode("utf-8")).hexdigest()[:10]
        return {
            "device": f"rrbz-{opaque}",
            "motor_type_candidate": self.motor_type_candidate,
            "rssi_median": self.rssi_median,
            "rssi_min": min(self.rssi_samples) if self.rssi_samples else None,
            "rssi_max": max(self.rssi_samples) if self.rssi_samples else None,
            "bottom_raw": self.bottom_raw,
            "top_raw": self.top_raw,
            "firmware": self.firmware,
            "battery": self.battery,
            "inspection_errors": self.inspection_errors,
        }

    def private_dict(self) -> dict[str, Any]:
        value = self.public_dict()
        value.update({
            "name": self.name,
            "address": self.address,
            "manufacturer_data": self.manufacturer_data,
            "service_uuids": self.service_uuids,
            "candidate_access_code": self.candidate_access_code,
        })
        return value

    @property
    def rssi_median(self) -> float | None:
        return statistics.median(self.rssi_samples) if self.rssi_samples else None

    @property
    def motor_type_candidate(self) -> str:
        return "tdbu_candidate" if self.top_raw is not None and self.top_raw > 0 else "unknown"


def decode_candidate_access_code(raw: bytes) -> str | None:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if len(text) == 7 and text.isdigit() else None


def update_observation(observation: MotorObservation, advertisement: object) -> None:
    rssi = getattr(advertisement, "rssi", None)
    if isinstance(rssi, int):
        observation.rssi_samples.append(rssi)
    manufacturer = dict(getattr(advertisement, "manufacturer_data", {}))
    parsed = parse_manufacturer_data(manufacturer, tdbu=True)
    if parsed:
        observation.bottom_raw, observation.top_raw = parsed.bottom, parsed.top
    elif (parsed := parse_manufacturer_data(manufacturer, tdbu=False)):
        observation.bottom_raw = parsed.bottom
    observation.manufacturer_data = {str(key): bytes(value).hex() for key, value in manufacturer.items()}
    observation.service_uuids = sorted(str(item) for item in (getattr(advertisement, "service_uuids", None) or ()))


async def scan(adapter: AdapterIdentity, seconds: float) -> list[MotorObservation]:
    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Bleak is not installed; install linux_controller/requirements.txt") from exc
    found: dict[str, MotorObservation] = {}

    def detected(device: object, advertisement: object) -> None:
        name = getattr(advertisement, "local_name", None) or getattr(device, "name", None) or ""
        if not str(name).startswith("RRBZ_"):
            return
        address = str(getattr(device, "address"))
        item = found.setdefault(address, MotorObservation(str(name), address, device=device))
        item.device = device
        update_observation(item, advertisement)

    scanner = BleakScanner(detection_callback=detected, adapter=adapter.hci_name)
    async with scanner:
        await asyncio.sleep(seconds)
    return sorted(found.values(), key=lambda item: item.name)


async def inspect_motor(item: MotorObservation, adapter: AdapterIdentity,
                        read_candidate_code: bool) -> None:
    try:
        from bleak import BleakClient
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Bleak is not installed") from exc
    if item.device is None:
        item.inspection_errors.append("scan object unavailable")
        return
    try:
        async with BleakClient(item.device, adapter=adapter.hci_name, timeout=20) as client:
            try:
                raw = bytes(await client.read_gatt_char(FIRMWARE_CHARACTERISTIC))
                item.firmware = raw.decode("utf-8", errors="replace").strip("\x00") or None
            except Exception as exc:
                item.inspection_errors.append(f"firmware:{type(exc).__name__}")
            try:
                raw = bytes(await client.read_gatt_char(BATTERY_CHARACTERISTIC))
                if len(raw) == 1 and raw[0] <= 100:
                    item.battery = raw[0]
            except Exception as exc:
                item.inspection_errors.append(f"battery:{type(exc).__name__}")
            if read_candidate_code:
                try:
                    item.candidate_access_code = decode_candidate_access_code(
                        bytes(await client.read_gatt_char(AUTH_CHARACTERISTIC))
                    )
                    if item.candidate_access_code is None:
                        item.inspection_errors.append("access_code:not_a_seven_digit_value")
                except Exception as exc:
                    item.inspection_errors.append(f"access_code:{type(exc).__name__}")
    except Exception as exc:
        item.inspection_errors.append(f"connect:{type(exc).__name__}: {exc}")


def write_private_inventory(path: str | Path, adapter: AdapterIdentity,
                            motors: list[MotorObservation]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "adapter": {
            "controller_address": adapter.address,
            "usb_serial": adapter.usb_serial,
            "usb_path": adapter.usb_path,
            "runtime_hci": adapter.hci_name,
        },
        "motors": [item.private_dict() for item in motors],
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        os.chmod(target, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    finally:
        os.chmod(target, 0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graber-identify",
        description="Identify RRBZ motors through one explicitly selected external adapter; never writes GATT.",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--controller-address")
    selector.add_argument("--usb-serial")
    selector.add_argument("--usb-path")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--inspect", action="store_true",
                        help="connect read-only to collect firmware and battery")
    parser.add_argument("--read-candidate-access-code", action="store_true",
                        help="also read the auth characteristic; requires --capture-private")
    parser.add_argument("--capture-private", metavar="PATH",
                        help="write identities/raw data to a mode-0600 JSON file")
    return parser


async def run(args: argparse.Namespace) -> int:
    if not 0 < args.seconds <= 300:
        raise ValueError("--seconds must be greater than zero and at most 300")
    if args.read_candidate_access_code and not args.capture_private:
        raise ValueError("--read-candidate-access-code requires --capture-private")
    adapter = select_adapter(
        discover_adapters(), address=args.controller_address,
        usb_serial=args.usb_serial, usb_path=args.usb_path,
    )
    if not adapter.is_usb:
        raise RuntimeError("selected controller is not identified as USB; onboard Bluetooth is refused")
    motors = await scan(adapter, args.seconds)
    if args.inspect or args.read_candidate_access_code:
        for item in motors:
            await inspect_motor(item, adapter, args.read_candidate_access_code)
    print(json.dumps([item.public_dict() for item in motors], indent=2, sort_keys=True))
    if args.capture_private:
        write_private_inventory(args.capture_private, adapter, motors)
        print(f"private inventory written with mode 0600: {args.capture_private}")
    return 0 if motors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ValueError, LookupError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
