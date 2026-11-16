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
from datetime import datetime, timezone
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
        "command_received", "cold_connection_slot_acquired",
        "connection_attempt_started", "connection_attempt_succeeded",
        "connection_attempt_failed", "connection_ready",
        "gatt_write_acknowledged", "feedback_observed", "feedback_completed",
        "movement_observed", "command_retry_scheduled", "command_completed",
        "command_failed", "command_completion_unconfirmed", "disconnected",
        "group_command_received", "group_member_dispatched",
        "group_preparation_completed", "group_commands_released",
        "group_member_completed", "group_command_completed",
        "dispatcher_command_queued", "dispatcher_command_started",
        "dispatcher_command_completed", "dispatcher_queue_full",
        "dispatcher_command_duplicate",
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


def _bluez_property(runner: CommandRunner, hci_name: str, property_name: str) -> str | None:
    """Read an Adapter1 property using the stable BlueZ object path.

    Some kernels do not publish ``address`` in the Bluetooth class sysfs
    directory.  BlueZ still exposes the controller address and power state on
    ``/org/bluez/hciN``.  This read-only fallback also preserves the mapping
    between a transient HCI name and its USB parent.
    """

    try:
        result = runner((
            "busctl", "--system", "get-property", "org.bluez",
            f"/org/bluez/{hci_name}", "org.bluez.Adapter1", property_name,
        ))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    string_value = re.fullmatch(r'\s*s\s+"([^"]+)"\s*', result.stdout)
    if string_value:
        return string_value.group(1)
    boolean_value = re.fullmatch(r"\s*b\s+(true|false)\s*", result.stdout)
    if boolean_value:
        return boolean_value.group(1)
    return None


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
        if not address:
            address = _bluez_property(runner, entry.name, "Address")
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
                powered=(
                    powered.get(address)
                    if address in powered
                    else {"true": True, "false": False}.get(
                        _bluez_property(runner, entry.name, "Powered") or ""
                    )
                ),
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


LATENCY_FIELDS: dict[str, tuple[str, ...]] = {
    "controller_queue_ms": ("controller_queue_ms",),
    "dispatcher_queue_ms": ("dispatcher_queue_ms", "queue_wait_ms"),
    "dispatcher_execution_ms": ("dispatcher_execution_ms", "execution_ms"),
    "dispatcher_total_ms": ("dispatcher_total_ms",),
    "cold_queue_ms": ("cold_queue_ms",),
    "advertisement_latency_ms": ("advertisement_latency_ms", "discovery_ms"),
    "connection_latency_ms": ("connection_latency_ms", "connect_ms"),
    "notification_setup_latency_ms": ("notification_setup_latency_ms", "notify_setup_ms"),
    "gatt_setup_latency_ms": ("gatt_setup_latency_ms", "gatt_setup_ms"),
    "authentication_latency_ms": ("authentication_latency_ms", "auth_ms"),
    "write_ack_latency_ms": ("write_ack_latency_ms", "command_ack_ms"),
    "movement_start_latency_ms": ("movement_start_latency_ms", "movement_start_ms"),
    "feedback_latency_ms": ("feedback_latency_ms", "feedback_ms"),
    "feedback_first_sample_ms": ("feedback_first_sample_ms",),
    "total_latency_ms": ("total_latency_ms",),
    "connection_duration_ms": (
        "connection_duration_ms", "session_duration_ms", "connection_age_ms",
    ),
    "group_prepare_ms": ("group_prepare_ms", "prepare_ms"),
    "group_duration_ms": ("group_duration_ms", "duration_ms"),
    "dispatch_spread_ms": ("dispatch_spread_ms",),
    "movement_start_spread_ms": ("movement_start_spread_ms",),
}


_PREFERRED_METRIC_EVENTS: dict[str, tuple[str, ...]] = {
    "controller_queue_ms": ("command_received",),
    "dispatcher_queue_ms": ("dispatcher_command_started",),
    "dispatcher_execution_ms": ("dispatcher_command_completed",),
    "dispatcher_total_ms": ("dispatcher_command_completed",),
    "cold_queue_ms": ("connection_attempt_succeeded",),
    "advertisement_latency_ms": ("connection_attempt_succeeded",),
    "connection_latency_ms": ("connection_attempt_succeeded",),
    "notification_setup_latency_ms": ("connection_ready",),
    "gatt_setup_latency_ms": ("connection_ready",),
    "authentication_latency_ms": ("connection_ready",),
    "write_ack_latency_ms": (
        "command_completed", "command_failed", "command_completion_unconfirmed",
    ),
    "movement_start_latency_ms": (
        "command_completed", "command_failed", "command_completion_unconfirmed",
    ),
    "feedback_latency_ms": (
        "command_completed", "command_failed", "command_completion_unconfirmed",
    ),
    "feedback_first_sample_ms": ("feedback_observed",),
    "total_latency_ms": (
        "command_completed", "command_failed", "command_completion_unconfirmed",
    ),
    "connection_duration_ms": ("disconnected",),
    "group_prepare_ms": ("group_preparation_completed",),
    "group_duration_ms": ("group_command_completed",),
    "dispatch_spread_ms": ("group_command_completed",),
    "movement_start_spread_ms": ("group_command_completed",),
}


def _number(row: Mapping[str, Any], names: Sequence[str]) -> float | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    return None


def _metric_rows(
    rows: Sequence[Mapping[str, Any]], metric: str,
) -> list[tuple[Mapping[str, Any], float]]:
    names = LATENCY_FIELDS[metric]
    preferred = _PREFERRED_METRIC_EVENTS.get(metric, ())
    candidates = [
        (row, value) for row in rows
        if (value := _number(row, names)) is not None
    ]
    selected = [(row, value) for row, value in candidates if row.get("event") in preferred]
    if not selected:
        return candidates
    # Preserve pre-stage-event records when old and new rotations are reported
    # together, while dropping duplicate copies on modern terminal events.
    selected_commands = {
        str(row["command_id"]) for row, _value in selected if row.get("command_id") is not None
    }
    legacy = [
        (row, value) for row, value in candidates
        if row.get("event") not in preferred
        and (
            row.get("command_id") is None
            or str(row["command_id"]) not in selected_commands
        )
    ]
    return selected + legacy


def _latency_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    result: dict[str, dict[str, float | int | None]] = {}
    for metric in LATENCY_FIELDS:
        values = [value for _row, value in _metric_rows(rows, metric)]
        result[metric] = {
            "samples": len(values),
            "median": statistics.median(values) if values else None,
            "p95": percentile(values, 0.95),
            "max": max(values) if values else None,
        }
    return result


def _failure_category(row: Mapping[str, Any]) -> str:
    explicit = row.get("error_category")
    if isinstance(explicit, str) and explicit:
        return explicit[:80] if re.fullmatch(r"[A-Za-z0-9_. -]+", explicit[:80]) else "other"
    error = row.get("error")
    if not isinstance(error, str) or not error:
        return "unknown"
    # Keep only the exception/category prefix. Error messages can contain
    # private addresses or BlueZ object paths and never belong in summaries.
    prefix = error.split(":", 1)[0].strip()
    return prefix[:80] if re.fullmatch(r"[A-Za-z0-9_. -]+", prefix) else "other"


def _iso_timestamp(value: object) -> str | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


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
    connection_failures = [row for row in rows if row.get("event") == "connection_attempt_failed"]
    started_commands = {
        str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_starts
    }
    connected_commands = {
        str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_successes
    }

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

    def connection_success(items: Sequence[Mapping[str, Any]]) -> tuple[int, int, float | None]:
        starts = [row for row in items if row.get("event") == "connection_attempt_started"]
        successes = [row for row in items if row.get("event") == "connection_attempt_succeeded"]
        first_starts = sum(int(row.get("attempt", 0)) == 1 for row in starts)
        first_successes = sum(int(row.get("attempt", 0)) == 1 for row in successes)
        return len(starts), len(successes), (
            first_successes / first_starts if first_starts else None
        )

    def scope_summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        item_terminal = [
            row for row in items if row.get("event") in terminal_events or "event" not in row
        ]
        attempts, successes, first_ratio = connection_success(items)
        retry_events = sum(row.get("event") == "command_retry_scheduled" for row in items)
        retries = retry_events or sum(
            max(0, int(row.get("attempt", row.get("attempts", 1))) - 1)
            for row in item_terminal
        )
        return {
            "samples": len(item_terminal),
            "connection_attempts": attempts,
            "connection_successes": successes,
            "first_attempt_connection_success": (
                first_ratio if first_ratio is not None else first_attempt_ratio(item_terminal)
            ),
            "completed": ratio(item_terminal, "completed"),
            "movement_observed": ratio(item_terminal, "movement_observed"),
            "retry_count": retries,
            "latencies": _latency_summary(list(items)),
        }

    by_blind: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("blind_id") is not None:
            by_blind.setdefault(str(row["blind_id"]), []).append(row)
    by_adapter: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("adapter_id") is not None:
            by_adapter.setdefault(str(row["adapter_id"]), []).append(row)

    failures: dict[str, int] = {}
    for row in rows:
        if row.get("event") in {
            "connection_attempt_failed", "command_failed", "command_completion_unconfirmed",
        }:
            category = _failure_category(row)
            failures[category] = failures.get(category, 0) + 1

    timestamps = [
        float(row["recorded_at"]) for row in rows
        if isinstance(row.get("recorded_at"), (int, float))
        and math.isfinite(float(row["recorded_at"]))
    ]
    daily: dict[str, dict[str, int]] = {}
    for row in rows:
        stamp = row.get("recorded_at")
        if not isinstance(stamp, (int, float)) or not math.isfinite(float(stamp)):
            continue
        day = datetime.fromtimestamp(float(stamp), timezone.utc).date().isoformat()
        item = daily.setdefault(day, {
            "records": 0, "commands": 0, "completed": 0, "failed": 0,
            "connection_attempts": 0, "connection_failures": 0, "group_commands": 0,
        })
        item["records"] += 1
        event = row.get("event")
        if event == "command_received":
            item["commands"] += 1
        elif event == "command_completed":
            item["completed"] += 1
        elif event in {"command_failed", "command_completion_unconfirmed"}:
            item["failed"] += 1
        elif event == "connection_attempt_started":
            item["connection_attempts"] += 1
        elif event == "connection_attempt_failed":
            item["connection_failures"] += 1
        elif event == "group_command_received":
            item["group_commands"] += 1

    outliers: list[dict[str, Any]] = []
    outliers_by_stage: dict[str, list[dict[str, Any]]] = {}
    for metric in LATENCY_FIELDS:
        for row, value in _metric_rows(rows, metric):
            item = {
                "stage": metric,
                "duration_ms": value,
                "blind_id": row.get("blind_id"),
                "group_id": row.get("group_id"),
                "adapter_id": row.get("adapter_id"),
                "attempt": row.get("attempt"),
                "recorded_at": _iso_timestamp(row.get("recorded_at")),
            }
            outliers.append(item)
            outliers_by_stage.setdefault(metric, []).append(item)
    outliers.sort(key=lambda item: float(item["duration_ms"]), reverse=True)
    for items in outliers_by_stage.values():
        items.sort(key=lambda item: float(item["duration_ms"]), reverse=True)

    group_terminal = [row for row in rows if row.get("event") == "group_command_completed"]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("group_id") is not None:
            groups.setdefault(str(row["group_id"]), []).append(row)

    return {
        "period": {
            "start": _iso_timestamp(min(timestamps)) if timestamps else None,
            "end": _iso_timestamp(max(timestamps)) if timestamps else None,
            "duration_days": (
                (max(timestamps) - min(timestamps)) / 86400 if len(timestamps) >= 2 else 0
            ),
            "records": len(rows),
        },
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
        "retry_count": (
            sum(row.get("event") == "command_retry_scheduled" for row in rows)
            or sum(max(0, int(row.get("attempt", row.get("attempts", 1))) - 1) for row in terminal)
        ),
        "latencies": _latency_summary(rows),
        "by_blind": {
            blind: scope_summary(items)
            for blind, items in sorted(by_blind.items())
        },
        "by_adapter": {
            adapter: scope_summary(items)
            for adapter, items in sorted(by_adapter.items())
        },
        "failures_by_category": dict(sorted(failures.items())),
        "daily": dict(sorted(daily.items())),
        "slowest_outliers": outliers[:10],
        "slowest_by_stage": {
            stage: items[:3] for stage, items in sorted(outliers_by_stage.items())
        },
        "groups": {
            "samples": len(group_terminal),
            "latencies": _latency_summary(group_terminal),
            "by_group": {
                group: {
                    "samples": sum(row.get("event") == "group_command_completed" for row in items),
                    "failed_members": sum(int(row.get("failed_count", 0)) for row in items),
                    "latencies": _latency_summary(items),
                }
                for group, items in sorted(groups.items())
            },
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


def load_jsonl_files(
    paths: Sequence[str | Path], *, include_rotated: bool = True,
) -> list[Mapping[str, Any]]:
    """Load one or more logs, including each current file's ``.1`` rotation.

    Files are de-duplicated and records are sorted by their recorded timestamp.
    A missing rotated file is normal; an explicitly requested missing file is
    still reported by ``load_jsonl``.
    """

    selected: list[Path] = []
    seen: set[Path] = set()
    for supplied in paths:
        current = Path(supplied)
        candidates = [current.with_suffix(current.suffix + ".1"), current] if include_rotated else [current]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen or (candidate != current and not candidate.exists()):
                continue
            seen.add(resolved)
            selected.append(candidate)
    records = [record for path in selected for record in load_jsonl(path)]
    return sorted(
        records,
        key=lambda row: float(row.get("recorded_at", 0))
        if isinstance(row.get("recorded_at"), (int, float)) else 0,
    )


def parse_time_boundary(value: str) -> float:
    """Parse an epoch number or ISO-8601 timestamp as UTC."""

    try:
        return float(value)
    except ValueError:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def filter_records(
    records: Iterable[Mapping[str, Any]], *, since: float | None = None,
    until: float | None = None,
) -> list[Mapping[str, Any]]:
    """Return recorded rows inside inclusive timestamp boundaries.

    Legacy rows without ``recorded_at`` remain visible only for unbounded
    reports because their age cannot be established safely.
    """

    if since is None and until is None:
        return list(records)
    selected = []
    for row in records:
        stamp = row.get("recorded_at")
        if not isinstance(stamp, (int, float)):
            continue
        value = float(stamp)
        if (since is None or value >= since) and (until is None or value <= until):
            selected.append(row)
    return selected
