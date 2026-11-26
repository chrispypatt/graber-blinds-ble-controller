"""Command requests, outcomes and position snapshots shared by the controller interfaces."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum

from graber.bluetooth.protocol import CommandKind, Rail


class Outcome(str, Enum):
    RECEIVED = "received"
    CONNECTED = "connected"
    WRITE_ACKNOWLEDGED = "write_acknowledged"
    MOVEMENT_OBSERVED = "movement_observed"
    COMPLETED = "completed"
    COMPLETION_UNCONFIRMED = "completion_unconfirmed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class BlindCommand:
    id: str
    kind: CommandKind
    rail: Rail | None = None
    position: int | None = None  # raw motor coordinate; boundaries convert HA values
    received_at: float = field(default_factory=time.time)
    internal: bool = False


@dataclass
class PositionState:
    bottom: int | None = None
    top: int | None = None
    battery: int | None = None
    rssi: int | None = None
    updated_at: float | None = None
    battery_raw: int | None = None
    battery_read_at: float | None = None
    battery_accepted_at: float | None = None
    battery_zero_reads: int = 0

    def observe_battery(self, level: int, read_at: float) -> None:
        """Keep a credible baseline when a motor suddenly starts returning zero."""
        self.battery_raw = level
        self.battery_read_at = read_at
        self.battery_zero_reads = self.battery_zero_reads + 1 if level == 0 else 0
        # Genuine low-to-empty readings remain actionable. Repeated high-to-zero
        # readings can persist for days, so their count alone cannot confirm empty.
        if level > 0 or (self.battery is not None and self.battery <= 10):
            self.battery = level
            self.battery_accepted_at = read_at

    @property
    def battery_status(self) -> str:
        if self.battery_raw is None:
            return "unread"
        if self.battery_raw == 0 and self.battery != 0:
            return "zero_repeated" if self.battery_zero_reads > 1 else "zero_unconfirmed"
        return "reported"

    def battery_fields(self) -> dict[str, object]:
        return {
            "battery": self.battery,
            "battery_raw": self.battery_raw,
            "battery_read_at": self.battery_read_at,
            "battery_accepted_at": self.battery_accepted_at,
            "battery_zero_reads": self.battery_zero_reads,
            "battery_status": self.battery_status,
        }


@dataclass
class CommandResult:
    command_id: str
    blind_id: str
    outcome: Outcome
    adapter_id: str | None = None
    attempts: int = 0
    received_at: float = 0.0
    controller_queue_ms: float | None = None
    cold_queue_ms: float | None = None
    discovery_ms: float | None = None
    connect_ms: float | None = None
    notify_setup_ms: float | None = None
    gatt_setup_ms: float | None = None
    auth_ms: float | None = None
    connection_age_ms: float | None = None
    warm_connection: bool = False
    command_ack_ms: float | None = None
    feedback_ms: float | None = None
    feedback_first_sample_ms: float | None = None
    movement_start_ms: float | None = None
    finished_at: float = field(default_factory=time.time)
    movement_observed: bool = False
    error_category: str | None = None
    error: str | None = None

    def record_connection_timings(self, timings: dict[str, float]) -> None:
        self.cold_queue_ms = timings.get("cold_queue_ms", self.cold_queue_ms)
        self.discovery_ms = timings.get("discovery_ms", self.discovery_ms)
        self.connect_ms = timings.get("connect_ms", self.connect_ms)
        self.notify_setup_ms = timings.get("notify_setup_ms", self.notify_setup_ms)
        self.gatt_setup_ms = timings.get("gatt_setup_ms", self.gatt_setup_ms)
        self.auth_ms = timings.get("auth_ms", self.auth_ms)
        self.connection_age_ms = timings.get(
            "connection_age_ms", self.connection_age_ms
        )
        self.warm_connection = bool(timings.get("warm_connection", False))

    def telemetry_fields(self) -> dict[str, object]:
        fields = asdict(self)
        fields["outcome"] = self.outcome.value
        fields["total_latency_ms"] = max(0.0, (self.finished_at - self.received_at) * 1000)
        return fields


class CommandFailure(RuntimeError):
    pass


class CompletionUnconfirmed(CommandFailure):
    """Final write was acknowledged, so automatic replay is unsafe."""
