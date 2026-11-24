"""Record, rotate, and read bounded controller telemetry."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class JsonlMetricsWriter:
    """Small bounded terminal-operation log suitable for microSD storage."""

    RECORDED_EVENTS = {
        "command_received", "cold_connection_slot_acquired",
        "connection_attempt_started", "connection_attempt_succeeded",
        "connection_attempt_failed", "connection_ready", "connection_reused",
        "connection_phase_failed", "prepared", "prepare_failed",
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
        if event == "prepare_failed":
            # BlueZ exception text may embed a device path. Keep the category.
            row.pop("error", None)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


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
