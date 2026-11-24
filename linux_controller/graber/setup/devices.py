"""Device identity and browser-visible setup state."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from graber.bluetooth.protocol import Rail, raw_to_ha_position
from .identify_cli import MotorObservation


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return result or "blind"


def opaque_id(address: str) -> str:
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:12]


@dataclass
class DeviceSetup:
    observation: MotorObservation
    id: str
    friendly_name: str
    dual_rail: bool
    preferred_adapter: str | None
    groups: tuple[str, ...] = ()
    enabled: bool = True
    configured: bool = False
    access_code: str | None = field(default=None, repr=False)
    loaded_from_disk: bool = False

    def browser_dict(self, allow_code_read: bool) -> dict[str, Any]:
        bottom_position = (
            raw_to_ha_position(Rail.BOTTOM, self.observation.bottom_raw)
            if self.observation.bottom_raw is not None else None
        )
        top_position = (
            raw_to_ha_position(Rail.TOP, self.observation.top_raw)
            if self.dual_rail and self.observation.top_raw is not None else None
        )
        return {
            "key": opaque_id(self.observation.address),
            "advertised_name": self.observation.name,
            "id": self.id,
            "name": self.friendly_name,
            "dual_rail": self.dual_rail,
            "preferred_adapter": self.preferred_adapter,
            "groups": list(self.groups),
            "enabled": self.enabled,
            "configured": self.configured,
            "loaded_from_disk": self.loaded_from_disk,
            "bottom_raw": self.observation.bottom_raw,
            "top_raw": self.observation.top_raw,
            "bottom_position": bottom_position,
            "top_position": top_position,
            "rssi_median": self.observation.rssi_median,
            "has_access_code": self.access_code is not None,
            "candidate_code_available": allow_code_read and self.observation.candidate_access_code is not None,
        }
