"""Read-only command-line diagnostics for the Raspberry Pi controller."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict

from graber.diagnostics import (
    adapter_as_dict,
    adapter_preflight,
    discover_adapters,
    filter_records,
    load_jsonl_files,
    parse_time_boundary,
    run_host_checks,
    select_adapter,
    summarize_operations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graber-diagnostics",
        description="Inspect Bluetooth controllers and offline telemetry; never moves a blind.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    adapters = commands.add_parser("adapters", help="list BlueZ controllers and stable USB identity")
    adapters.add_argument("--show-private", action="store_true", help="show full controller addresses/serials")
    adapters.add_argument("--expected", type=int, default=2)
    commands.add_parser("host", help="run lsusb/BlueZ/rfkill/service read-only checks")
    scan = commands.add_parser("scan", help="scan RRBZ advertisements on one resolved adapter; never connects")
    selector = scan.add_mutually_exclusive_group(required=True)
    selector.add_argument("--controller-address")
    selector.add_argument("--usb-serial")
    selector.add_argument("--usb-path")
    scan.add_argument("--seconds", type=float, default=12.0)
    scan.add_argument("--show-private", action="store_true", help="show advertised names and full addresses")
    report = commands.add_parser("report", help="summarize existing JSONL performance telemetry")
    report.add_argument("jsonl", nargs="+", help="current JSONL path(s); .1 rotations are included")
    time_filter = report.add_mutually_exclusive_group()
    time_filter.add_argument("--since", help="inclusive ISO-8601 or epoch start time")
    time_filter.add_argument("--recent-days", type=float, help="include only the most recent N days")
    report.add_argument("--until", help="inclusive ISO-8601 or epoch end time")
    report.add_argument(
        "--no-rotated", action="store_true", help="do not include adjacent .1 rotation files"
    )
    return parser


async def _scan(args: argparse.Namespace) -> list[dict[str, object]]:
    if args.seconds <= 0 or args.seconds > 300:
        raise ValueError("scan duration must be greater than zero and no more than 300 seconds")
    adapter = select_adapter(
        discover_adapters(),
        address=args.controller_address,
        usb_serial=args.usb_serial,
        usb_path=args.usb_path,
    )
    try:
        from bleak import BleakScanner
    except ImportError as exc:  # pragma: no cover - exercised only on an uninstalled host
        raise RuntimeError("Bleak is not installed; run the Raspberry Pi installer") from exc
    discovered = await BleakScanner.discover(
        timeout=args.seconds,
        return_adv=True,
        adapter=adapter.hci_name,
    )
    result: list[dict[str, object]] = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name or ""
        if not name.startswith("RRBZ_"):
            continue
        opaque_id = hashlib.sha256(device.address.encode("utf-8")).hexdigest()[:10]
        result.append(
            {
                "device": name if args.show_private else f"rrbz-{opaque_id}",
                "address": device.address if args.show_private else "<redacted>",
                "rssi": advertisement.rssi,
                "adapter": adapter.hci_name,
            }
        )
    return sorted(result, key=lambda item: str(item["device"]))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "adapters":
        adapters = discover_adapters()
        output = {
            "adapters": [adapter_as_dict(item, show_private=args.show_private) for item in adapters],
            "checks": [asdict(item) for item in adapter_preflight(adapters, expected=args.expected)],
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if all(check["ok"] for check in output["checks"]) else 1
    if args.command == "host":
        checks = [asdict(item) for item in run_host_checks()]
        print(json.dumps(checks, indent=2, sort_keys=True))
        return 0 if all(check["ok"] for check in checks) else 1
    if args.command == "scan":
        print(json.dumps(asyncio.run(_scan(args)), indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        if args.recent_days is not None and args.recent_days <= 0:
            raise ValueError("--recent-days must be greater than zero")
        since = (
            time.time() - args.recent_days * 86400
            if args.recent_days is not None
            else parse_time_boundary(args.since) if args.since else None
        )
        until = parse_time_boundary(args.until) if args.until else None
        records = load_jsonl_files(args.jsonl, include_rotated=not args.no_rotated)
        print(json.dumps(
            summarize_operations(filter_records(records, since=since, until=until)),
            indent=2, sort_keys=True,
        ))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
