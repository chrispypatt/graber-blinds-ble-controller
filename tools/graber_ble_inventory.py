#!/usr/bin/env python3
"""Discover Graber RRBZ motors and optionally read candidate access codes.

This tool never writes a GATT characteristic. On macOS, ``address`` is usually
a CoreBluetooth UUID rather than the six-octet BLE address; use the ESP32
identifier firmware when a real MAC is required by ESPHome.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner


AUTH_CHARACTERISTIC = "5B026510-4088-C297-46D8-BE6CB63A8691"


@dataclass
class Found:
    name: str
    address: str
    rssi: int | None
    device: object


async def scan(seconds: float) -> list[Found]:
    discovered = await BleakScanner.discover(timeout=seconds, return_adv=True)
    found: list[Found] = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name or ""
        if name.startswith("RRBZ_"):
            found.append(Found(name, device.address, advertisement.rssi, device))
    return sorted(found, key=lambda item: item.name)


async def read_candidate(item: Found) -> str:
    try:
        async with BleakClient(item.device, timeout=20) as client:
            raw = bytes(await client.read_gatt_char(AUTH_CHARACTERISTIC))
    except Exception as exc:
        return f"read_error={type(exc).__name__}: {exc}"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        text = ""
    if len(raw) == 7 and text.isdigit():
        return f"candidate_access_code={text}"
    return f"not_a_code bytes={raw.hex(' ')}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-seconds", type=float, default=12)
    parser.add_argument("--name", help="limit to one advertised RRBZ_* name")
    parser.add_argument(
        "--read-candidate-codes",
        action="store_true",
        help="connect read-only and print sensitive candidate access codes",
    )
    args = parser.parse_args()
    items = await scan(args.scan_seconds)
    if args.name:
        items = [item for item in items if item.name == args.name]
    if not items:
        raise SystemExit("No matching RRBZ_* advertisements found")
    for item in items:
        rssi = "unknown" if item.rssi is None else str(item.rssi)
        line = f"{item.name} address={item.address} rssi={rssi}"
        if args.read_candidate_codes:
            line += " " + await read_candidate(item)
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
