"""Private, restart-safe battery observations keyed by physical motor address."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from graber.util.private_files import read_private_text, write_private_text

from .commands import PositionState


LOG = logging.getLogger("graber")


def _percentage(value: object) -> bool:
    return type(value) is int and 0 <= value <= 100


def _timestamp(value: object) -> bool:
    # Bound timestamps to the range representable by datetime and browser UIs.
    return type(value) in (int, float) and 0 < value < 253402300800


class BatteryHistory:
    """Load only battery measurements. Restoring one never marks a shade online."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, dict] = {}
        self._writer: asyncio.Task[None] | None = None
        self._write_failed = False
        self._enabled = True
        try:
            saved = json.loads(read_private_text(path, require_owner=True))
            if (not isinstance(saved, dict) or saved.get("version") != 1
                    or not isinstance(saved.get("blinds"), dict)):
                raise ValueError("unsupported battery history format")
            self._records = saved["blinds"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # Leave an inaccessible or unknown-format file intact.
            self._enabled = False
            LOG.warning("Battery history unavailable (%s); using live readings", type(exc).__name__)

    def restore(self, address: str, state: PositionState) -> None:
        key = address.upper()
        if key not in self._records:
            return
        record = self._records[key]
        try:
            level, raw = record["battery"], record["battery_raw"]
            accepted_at, read_at = record["battery_accepted_at"], record["battery_read_at"]
            zeros = record["battery_zero_reads"]
            if not (_percentage(raw) and _timestamp(read_at)):
                raise ValueError
            if type(zeros) is not int or zeros < 0 or (raw == 0) != (zeros > 0):
                raise ValueError
            if level is None:
                if raw != 0 or accepted_at is not None:
                    raise ValueError
            elif not _percentage(level) or not _timestamp(accepted_at):
                raise ValueError
            elif raw != level and (raw != 0 or level <= 10):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            LOG.warning("Ignoring invalid stored battery observation")
            return
        state.battery = level
        state.battery_raw = raw
        state.battery_read_at = read_at
        state.battery_accepted_at = accepted_at
        state.battery_zero_reads = zeros

    def record(self, address: str, state: PositionState) -> None:
        """Queue the latest snapshot without holding a Bluetooth connection for disk I/O."""
        if not self._enabled:
            return
        self._records = {**self._records, address.upper(): state.battery_fields()}
        if self._writer is None:
            self._writer = asyncio.create_task(self._flush_updates(), name="battery-history")

    async def flush(self) -> None:
        if self._writer is not None:
            await asyncio.shield(self._writer)

    async def _flush_updates(self) -> None:
        try:
            while True:
                snapshot = self._records
                try:
                    content = json.dumps({"version": 1, "blinds": snapshot})
                    await asyncio.to_thread(write_private_text, self.path, content)
                    self._write_failed = False
                except (OSError, ValueError) as exc:
                    if not self._write_failed:
                        LOG.warning("Could not save battery history (%s); keeping readings in memory",
                                    type(exc).__name__)
                    self._write_failed = True
                if snapshot is self._records:
                    break
        finally:
            self._writer = None
