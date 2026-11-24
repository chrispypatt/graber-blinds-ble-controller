"""Aggregate command outcomes, connection timing, and group telemetry."""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


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
    "bluez_first_link_ms": ("bluez_first_link_ms",),
    "bluez_service_resolution_ms": ("bluez_service_resolution_ms",),
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
    "bluez_first_link_ms": ("connection_ready", "connection_phase_failed"),
    "bluez_service_resolution_ms": ("connection_ready", "connection_phase_failed"),
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


TERMINAL_EVENTS = {"command_completed", "command_failed", "command_completion_unconfirmed"}


def _ratio(items: Sequence[Mapping[str, Any]], key: str) -> float | None:
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


def _first_attempt_ratio(items: Sequence[Mapping[str, Any]]) -> float | None:
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


def _connection_success(items: Sequence[Mapping[str, Any]]) -> tuple[int, int, float | None]:
    starts = [row for row in items if row.get("event") == "connection_attempt_started"]
    successes = [row for row in items if row.get("event") == "connection_attempt_succeeded"]
    first_starts = sum(int(row.get("attempt", 0)) == 1 for row in starts)
    first_successes = sum(int(row.get("attempt", 0)) == 1 for row in successes)
    return len(starts), len(successes), (
        first_successes / first_starts if first_starts else None
    )


def _scope_summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    item_terminal = [
        row for row in items if row.get("event") in TERMINAL_EVENTS or "event" not in row
    ]
    attempts, successes, first_ratio = _connection_success(items)
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
            first_ratio if first_ratio is not None else _first_attempt_ratio(item_terminal)
        ),
        "completed": _ratio(item_terminal, "completed"),
        "movement_observed": _ratio(item_terminal, "movement_observed"),
        "retry_count": retries,
        "latencies": _latency_summary(list(items)),
    }


def _daily_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
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
    return dict(sorted(daily.items()))


def _outliers(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
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
    return outliers, outliers_by_stage


def _group_by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get(key) is not None:
            groups.setdefault(str(row[key]), []).append(row)
    return groups


def summarize_operations(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize controller JSONL telemetry without treating writes as movement.

    Expected records may contain ``blind_id``, ``attempt``, ``connected``,
    ``write_acknowledged``, ``movement_observed``, ``completed``, and latency
    values ending in ``_ms``. Unknown fields are ignored so telemetry can evolve.
    """

    rows = list(records)
    terminal = [row for row in rows if row.get("event") in TERMINAL_EVENTS or "event" not in row]
    connection_starts = [row for row in rows if row.get("event") == "connection_attempt_started"]
    connection_successes = [row for row in rows if row.get("event") == "connection_attempt_succeeded"]
    started_commands = {
        str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_starts
    }
    connected_commands = {
        str(row.get("command_id", row.get("blind_id", "unknown"))) for row in connection_successes
    }

    failures: dict[str, int] = {}
    group_phase_failures: dict[str, int] = {}
    for row in rows:
        if row.get("event") == "connection_phase_failed" and row.get("operation") == "group_prepare":
            phase = str(row.get("phase", "unknown"))
            key = f"{phase}:{_failure_category(row)}"
            group_phase_failures[key] = group_phase_failures.get(key, 0) + 1
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
    outliers, outliers_by_stage = _outliers(rows)
    group_terminal = [row for row in rows if row.get("event") == "group_command_completed"]
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
        "group_connection_phase_failures": group_phase_failures,
        "first_attempt_connection_success": (
            sum(int(row.get("attempt", 0)) == 1 for row in connection_successes) /
            sum(int(row.get("attempt", 0)) == 1 for row in connection_starts)
            if any(int(row.get("attempt", 0)) == 1 for row in connection_starts)
            else _first_attempt_ratio(terminal)
        ),
        "eventual_connection_success": (
            len(connected_commands) / len(started_commands) if started_commands else _ratio(terminal, "connected")
        ),
        "gatt_write_acknowledged": _ratio(terminal, "write_acknowledged"),
        "movement_observed": _ratio(terminal, "movement_observed"),
        "completed": _ratio(terminal, "completed"),
        "retry_count": (
            sum(row.get("event") == "command_retry_scheduled" for row in rows)
            or sum(max(0, int(row.get("attempt", row.get("attempts", 1))) - 1) for row in terminal)
        ),
        "latencies": _latency_summary(rows),
        "by_blind": {
            blind: _scope_summary(items)
            for blind, items in sorted(_group_by(rows, "blind_id").items())
        },
        "by_adapter": {
            adapter: _scope_summary(items)
            for adapter, items in sorted(_group_by(rows, "adapter_id").items())
        },
        "failures_by_category": dict(sorted(failures.items())),
        "daily": _daily_counts(rows),
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
                for group, items in sorted(_group_by(rows, "group_id").items())
            },
        },
    }
