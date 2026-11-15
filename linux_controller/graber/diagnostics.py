"""Read-only Linux Bluetooth discovery and controller performance summaries.

The service must not bind configuration to ``hci0``/``hci1`` because the
kernel may enumerate USB controllers in a different order after a reboot.
This module therefore exposes the controller address and the USB parent's
serial/physical path alongside the transient HCI name.

Nothing in this module writes a Bluetooth property, resets an adapter, or
connects to a blind.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AdapterIdentity:
    """Stable and transient identity observed for one BlueZ controller."""

    hci_name: str
    address: str | None
    powered: bool | None
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    usb_manufacturer: str | None = None
    usb_product: str | None = None
    usb_serial: str | None = None
    usb_path: str | None = None
    sysfs_path: str | None = None

    @property
    def stable_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        if self.address:
            keys.append(f"address:{self.address.upper()}")
        if self.usb_serial:
            keys.append(f"usb_serial:{self.usb_serial}")
        if self.usb_path:
            keys.append(f"usb_path:{self.usb_path}")
        return tuple(keys)

    @property
    def is_usb(self) -> bool:
        return bool(self.usb_vendor_id and self.usb_product_id)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class JsonlMetricsWriter:
    """Small bounded terminal-operation log suitable for microSD storage."""

    RECORDED_EVENTS = {
        "connection_attempt_started", "connection_attempt_succeeded",
        "connection_attempt_failed", "movement_observed", "command_completed",
        "command_failed", "command_completion_unconfirmed", "disconnected",
    }

    def __init__(self, path: str | Path, max_bytes: int = 2_000_000):
        self.path = Path(path)
        self.max_bytes = max_bytes

    def record(self, event: str, fields: Mapping[str, Any]) -> None:
        if event not in self.RECORDED_EVENTS:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            backup = self.path.with_suffix(self.path.suffix + ".1")
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
            self.path.replace(backup)
        row = {"event": event, "recorded_at": time.time(), **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value or None


def _usb_parent(device_path: Path) -> Path | None:
    """Return the nearest sysfs ancestor that describes a USB device."""

    current = device_path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "idVendor").is_file() and (candidate / "idProduct").is_file():
            return candidate
    return None


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )


def _powered_by_address(runner: CommandRunner) -> dict[str, bool]:
    """Read BlueZ power state without changing it."""

    try:
        listed = runner(("bluetoothctl", "list"))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    addresses = re.findall(r"^Controller\s+([0-9A-Fa-f:]{17})\b", listed.stdout, re.MULTILINE)
    states: dict[str, bool] = {}
    for address in addresses:
        try:
            shown = runner(("bluetoothctl", "show", address))
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"^\s*Powered:\s*(yes|no)\s*$", shown.stdout, re.MULTILINE)
        if match:
            states[address.upper()] = match.group(1) == "yes"
    return states


def discover_adapters(
    sysfs_root: str | Path = "/sys/class/bluetooth",
    *,
    runner: CommandRunner = _default_runner,
) -> list[AdapterIdentity]:
    """Discover BlueZ HCI controllers and their stable USB identity.

    ``sysfs_root`` is injectable so the mapping can be tested without Bluetooth
    hardware. Missing sysfs or missing ``bluetoothctl`` returns an empty or
    partially populated result rather than changing system state.
    """

    root = Path(sysfs_root)
    powered = _powered_by_address(runner)
    adapters: list[AdapterIdentity] = []
    try:
        hci_entries = sorted(root.glob("hci*"), key=lambda item: item.name)
    except OSError:
        hci_entries = []
    for entry in hci_entries:
        address = _read_text(entry / "address")
        address = address.upper() if address else None
        device_link = entry / "device"
        try:
            device_path = device_link.resolve(strict=True)
        except (FileNotFoundError, OSError):
            device_path = entry.resolve()
        usb = _usb_parent(device_path)
        adapters.append(
            AdapterIdentity(
                hci_name=entry.name,
                address=address,
                powered=powered.get(address) if address else None,
                usb_vendor_id=_read_text(usb / "idVendor") if usb else None,
                usb_product_id=_read_text(usb / "idProduct") if usb else None,
                usb_manufacturer=_read_text(usb / "manufacturer") if usb else None,
                usb_product=_read_text(usb / "product") if usb else None,
                usb_serial=_read_text(usb / "serial") if usb else None,
                usb_path=usb.name if usb else None,
                sysfs_path=str(device_path),
            )
        )
    return adapters


async def resolve_configured_adapters(configs: tuple[Any, ...]) -> list[Any]:
    """Resolve configured stable selectors to transient BlueZ adapter names.

    Imports are local to keep this read-only diagnostics module usable on its
    own and to avoid a package import cycle.
    """
    import asyncio
    from .adapter_manager import ResolvedAdapter

    discovered = await asyncio.to_thread(discover_adapters)
    resolved = []
    for config in configs:
        try:
            match = select_adapter(
                discovered, address=config.controller_address, usb_path=config.usb_path
            )
        except LookupError:
            continue
        if not match.is_usb or match.powered is not True:
            continue
        resolved.append(ResolvedAdapter(config.id, match.address or "", match.hci_name, match.usb_path))
    return resolved


def select_adapter(
    adapters: Iterable[AdapterIdentity],
    *,
    address: str | None = None,
    usb_serial: str | None = None,
    usb_path: str | None = None,
) -> AdapterIdentity:
    """Resolve exactly one adapter from one or more stable selectors."""

    if not any((address, usb_serial, usb_path)):
        raise ValueError("an address, usb_serial, or usb_path selector is required")
    normalized_address = address.upper() if address else None
    matches = [
        adapter
        for adapter in adapters
        if (normalized_address is None or adapter.address == normalized_address)
        and (usb_serial is None or adapter.usb_serial == usb_serial)
        and (usb_path is None or adapter.usb_path == usb_path)
    ]
    if not matches:
        raise LookupError("no Bluetooth controller matches the stable selector")
    if len(matches) > 1:
        raise LookupError("stable selector is ambiguous; add controller address or USB path")
    return matches[0]


def adapter_preflight(
    adapters: Sequence[AdapterIdentity],
    *,
    expected: int = 2,
    require_usb: bool = True,
) -> list[CheckResult]:
    usb = [adapter for adapter in adapters if adapter.is_usb]
    selected = usb if require_usb else list(adapters)
    results = [
        CheckResult(
            "controller_count",
            len(selected) >= expected,
            f"found {len(selected)} eligible controller(s); expected at least {expected}",
        ),
        CheckResult(
            "unique_addresses",
            len({item.address for item in selected if item.address}) == len(selected),
            "eligible controller addresses must be present and unique",
        ),
        CheckResult(
            "stable_identity",
            all(item.address and (item.usb_serial or item.usb_path) for item in selected),
            "each controller needs an address plus USB serial or physical path",
        ),
        CheckResult(
            "powered",
            bool(selected) and all(item.powered is True for item in selected),
            "each eligible controller must report Powered: yes",
        ),
    ]
    return results


def run_host_checks(runner: CommandRunner = _default_runner) -> list[CheckResult]:
    """Run the documented, non-mutating host inspection commands."""

    checks = (
        ("usb", ("lsusb",)),
        ("bluez_controllers", ("bluetoothctl", "list")),
        ("rfkill", ("rfkill", "list", "bluetooth")),
        ("bluetooth_service", ("systemctl", "is-active", "bluetooth.service")),
    )
    results: list[CheckResult] = []
    for name, command in checks:
        try:
            completed = runner(command)
            detail = (completed.stdout or completed.stderr).strip()
            results.append(CheckResult(name, completed.returncode == 0, detail[:2000]))
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            results.append(CheckResult(name, False, f"{type(exc).__name__}: {exc}"))
    return results


def redact_address(address: str | None) -> str | None:
    if not address:
        return address
    parts = address.split(":")
    return ":".join((*parts[:3], "XX", "XX", parts[-1])) if len(parts) == 6 else "<redacted>"


def adapter_as_dict(adapter: AdapterIdentity, *, show_private: bool = False) -> dict[str, Any]:
    value = asdict(adapter)
    if not show_private:
        value["address"] = redact_address(adapter.address)
        if value["usb_serial"]:
            value["usb_serial"] = "<redacted>"
        value["sysfs_path"] = None
    value["stable_keys"] = list(adapter.stable_keys) if show_private else []
    return value


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize_operations(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize controller JSONL telemetry without treating writes as movement.

    Expected records may contain ``blind_id``, ``attempt``, ``connected``,
    ``write_acknowledged``, ``movement_observed``, ``completed``, and latency
    values ending in ``_ms``. Unknown fields are ignored so telemetry can evolve.
    """

    rows = list(records)
    terminal_events = {"command_completed", "command_failed", "command_completion_unconfirmed"}
    terminal = [row for row in rows if row.get("event") in terminal_events or "event" not in row]
    connection_starts = [row for row in rows if row.get("event") == "connection_attempt_started"]
    connection_successes = [row for row in rows if row.get("event") == "connection_attempt_succeeded"]
    started_commands = {str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_starts}
    connected_commands = {str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_successes}
    def ratio(items: Sequence[Mapping[str, Any]], key: str) -> float | None:
        if not items:
            return None
        def observed(item: Mapping[str, Any]) -> bool:
            if key in item:
                return bool(item[key])
            if key == "connected":
                return item.get("connect_ms") is not None
            if key == "write_acknowledged":
                return item.get("command_ack_ms") is not None
            if key == "completed":
                return item.get("outcome") == "completed" or item.get("event") == "command_completed"
            return False
        return sum(observed(item) for item in items) / len(items)

    def first_attempt_ratio(items: Sequence[Mapping[str, Any]]) -> float | None:
        if not items:
            return None
        successes = 0
        for item in items:
            explicit = item.get("first_attempt_connected")
            if explicit is not None:
                successes += bool(explicit)
            else:
                connected = bool(item.get("connected", item.get("connect_ms") is not None))
                attempt = item.get("attempt", item.get("attempts", 1))
                successes += connected and int(attempt) == 1
        return successes / len(items)

    latency_keys = (
        "advertisement_latency_ms",
        "connection_latency_ms",
        "authentication_latency_ms",
        "gatt_setup_latency_ms",
        "write_ack_latency_ms",
        "movement_start_latency_ms",
        "total_latency_ms",
        "connection_duration_ms",
    )
    latencies: dict[str, dict[str, float | int | None]] = {}
    for key in latency_keys:
        aliases = {
            "advertisement_latency_ms": "discovery_ms",
            "connection_latency_ms": "connect_ms",
            "authentication_latency_ms": "auth_ms",
            "gatt_setup_latency_ms": "gatt_setup_ms",
            "write_ack_latency_ms": "command_ack_ms",
            "movement_start_latency_ms": "movement_start_ms",
        }
        values = [
            float(row.get(key, row.get(aliases.get(key, ""))))
            for row in rows
            if isinstance(row.get(key, row.get(aliases.get(key, ""))), (int, float))
        ]
        latencies[key] = {
            "samples": len(values),
            "median": statistics.median(values) if values else None,
            "p95": percentile(values, 0.95),
        }
    by_blind: dict[str, list[Mapping[str, Any]]] = {}
    for row in terminal:
        by_blind.setdefault(str(row.get("blind_id", "unknown")), []).append(row)
    connection_by_blind: dict[str, tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]] = {}
    for blind in {str(row.get("blind_id", "unknown")) for row in connection_starts + connection_successes}:
        connection_by_blind[blind] = (
            [row for row in connection_starts if str(row.get("blind_id", "unknown")) == blind],
            [row for row in connection_successes if str(row.get("blind_id", "unknown")) == blind],
        )
    return {
        "samples": len(terminal),
        "connection_attempts": len(connection_starts),
        "first_attempt_connection_success": (
            sum(int(row.get("attempt", 0)) == 1 for row in connection_successes) /
            sum(int(row.get("attempt", 0)) == 1 for row in connection_starts)
            if any(int(row.get("attempt", 0)) == 1 for row in connection_starts)
            else first_attempt_ratio(terminal)
        ),
        "eventual_connection_success": (
            len(connected_commands) / len(started_commands) if started_commands else ratio(terminal, "connected")
        ),
        "gatt_write_acknowledged": ratio(terminal, "write_acknowledged"),
        "movement_observed": ratio(terminal, "movement_observed"),
        "completed": ratio(terminal, "completed"),
        "retry_count": sum(max(0, int(row.get("attempt", row.get("attempts", 1))) - 1) for row in terminal),
        "latencies": latencies,
        "by_blind": {
            blind: {
                "samples": len(items),
                "first_attempt_connection_success": (
                    sum(int(row.get("attempt", 0)) == 1 for row in connection_by_blind.get(blind, ([], []))[1]) /
                    sum(int(row.get("attempt", 0)) == 1 for row in connection_by_blind.get(blind, ([], []))[0])
                    if any(int(row.get("attempt", 0)) == 1 for row in connection_by_blind.get(blind, ([], []))[0])
                    else first_attempt_ratio(items)
                ),
                "completed": ratio(items, "completed"),
            }
            for blind, items in sorted(by_blind.items())
        },
    }


def load_jsonl(path: str | Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            records.append(value)
    return records
