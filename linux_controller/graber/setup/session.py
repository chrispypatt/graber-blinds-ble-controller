"""Track discovered adapters, scans, and proposed blind assignments."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable

from graber.diagnostics.adapters import AdapterIdentity
from .configuration import SavedSetup, write_configuration
from .devices import DeviceSetup, opaque_id, slug
from .identify_cli import MotorObservation, inspect_motor, scan


COMMON_GROUP_NAMES = (
    "Living Room", "Family Room", "Great Room", "Dining Room", "Kitchen",
    "Primary Bedroom", "Bedroom", "Guest Bedroom", "Nursery", "Office", "Den",
    "Media Room", "Game Room", "Sunroom", "Basement", "Upstairs", "Downstairs",
    "Front of House", "Back of House", "All Bedrooms", "All Common Areas", "Whole House",
)


def select_usb_adapters(discovered: list[AdapterIdentity],
                        controller_addresses: list[str] | None, *,
                        allow_partial: bool = False) -> list[AdapterIdentity]:
    """Select powered USB controllers, or validate an explicit address selection."""

    available = [item for item in discovered if item.is_usb and item.powered is True]
    if not controller_addresses:
        if not available and not allow_partial:
            raise ValueError("no powered USB Bluetooth adapters found")
        return available
    wanted = {value.upper() for value in controller_addresses}
    selected = [item for item in available if (item.address or "").upper() in wanted]
    found = {(item.address or "").upper() for item in selected}
    if found != wanted and not allow_partial:
        raise ValueError(f"configured USB controller(s) unavailable: {sorted(wanted - found)}")
    return selected


class SetupSession:
    def __init__(self, adapters: list[AdapterIdentity], *, allow_code_read: bool = False,
                 export_dir: str | Path | None = None,
                 known_credentials: dict[str, str] | None = None,
                 group_names: tuple[str, ...] = (),
                 saved_setup: SavedSetup | None = None,
                 controller_addresses: list[str] | None = None,
                 adapter_discovery: Callable[[], list[AdapterIdentity]] | None = None):
        self.allow_code_read = allow_code_read
        self.export_dir = Path(export_dir).resolve() if export_dir else None
        self.known_credentials = dict(known_credentials or {})
        self.saved_setup = saved_setup or SavedSetup({}, {})
        self._controller_addresses = {address.upper() for address in controller_addresses or ()}
        self._adapter_discovery = adapter_discovery
        self._known_adapters: dict[str, AdapterIdentity] = {}
        self._added_adapters: set[str] = set()
        self._detected_adapters: dict[str, AdapterIdentity] = {}
        self.adapters: dict[str, AdapterIdentity] = {}
        self._update_adapters(adapters)
        self.groups: dict[str, str] = {}
        for group_id, name in self.saved_setup.groups.items():
            self.groups[group_id] = name
        for name in (*group_names, *COMMON_GROUP_NAMES):
            friendly = name.strip()
            if not friendly:
                continue
            group_id = slug(friendly)
            if group_id in self.groups:
                continue
            self.groups[group_id] = friendly
        self.devices = self._saved_devices()
        self.ble_lock = asyncio.Lock()
        self._adapter_refresh_lock = asyncio.Lock()
        self.scan_task: asyncio.Task[None] | None = None
        self.scan_started_at: float | None = None
        self.scan_duration_s: float | None = None

    def _update_adapters(self, discovered: list[AdapterIdentity]) -> None:
        known = {
            key: (str(row["controller_address"]).upper(), row.get("usb_path"))
            for key, row in self.saved_setup.adapters.items()
        }
        for key, adapter in self._known_adapters.items():
            known[key] = (adapter.address, adapter.usb_path)
        available = [
            item for item in select_usb_adapters(discovered, None, allow_partial=True)
            if item.address
        ]
        detected: dict[str, AdapterIdentity] = {}
        for item in available:
            matches = [key for key, (address, _) in known.items() if address == item.address]
            if len(matches) > 1 or sum(usb.address == item.address for usb in available) > 1:
                matches = [key for key in matches if known[key][1] == item.usb_path]
            adapter_id = matches[0] if len(matches) == 1 else slug(
                f"usb_{item.usb_path or opaque_id(item.address)}"
            )
            if not matches and adapter_id in known:
                adapter_id = f"{adapter_id}_{opaque_id(item.address)}"
            if adapter_id in detected or (not matches and adapter_id in known):
                raise ValueError(f"ambiguous USB adapter assignment for {adapter_id}")
            detected[adapter_id] = item
        self._known_adapters.update(detected)
        self._detected_adapters = detected
        self.adapters = {
            key: item for key, item in detected.items()
            if not self._controller_addresses or item.address in self._controller_addresses
            or key in self.saved_setup.adapters or key in self._added_adapters
        }

    async def _discover_adapters(self, *, only_if_idle: bool = False) -> None:
        if self._adapter_discovery:
            discovered = await asyncio.to_thread(self._adapter_discovery)
            if not only_if_idle or not self.ble_lock.locked():
                self._update_adapters(discovered)

    async def refresh_adapters(self) -> None:
        async with self._adapter_refresh_lock:
            if not self.ble_lock.locked():
                await self._discover_adapters(only_if_idle=True)

    async def add_adapter(self, adapter_id: str) -> None:
        if self.ble_lock.locked():
            raise ValueError("wait for the active Bluetooth operation to finish")
        async with self.ble_lock:
            await self._discover_adapters()
            if adapter_id not in self._detected_adapters:
                raise ValueError("this adapter is no longer available")
            if adapter_id not in self.adapters:
                self._added_adapters.add(adapter_id)
                self.adapters[adapter_id] = self._detected_adapters[adapter_id]

    def _missing_adapter_ids(self) -> set[str]:
        required = set(self.saved_setup.adapters) | self._added_adapters
        required.update(device.preferred_adapter for device in self.devices.values()
                        if device.enabled and device.preferred_adapter)
        return required.difference(self.adapters)

    @property
    def missing_adapters(self) -> int:
        missing = self._missing_adapter_ids()
        known_addresses = {item.address for item in self.adapters.values()}
        known_addresses.update(str(self.saved_setup.adapters[key]["controller_address"]).upper()
                               for key in missing if key in self.saved_setup.adapters)
        return len(missing) + len(self._controller_addresses.difference(known_addresses))

    def _saved_devices(self) -> dict[str, DeviceSetup]:
        return {
            opaque_id(address): DeviceSetup(
                MotorObservation(saved.friendly_name, address),
                saved.id, saved.friendly_name, saved.dual_rail, saved.preferred_adapter,
                groups=saved.groups, configured=True, access_code=saved.access_code,
                loaded_from_disk=True,
            )
            for address, saved in self.saved_setup.devices.items()
        }

    async def scan_all(self, seconds: float = 12) -> None:
        async with self.ble_lock:
            await self._discover_adapters()
            if not self.adapters:
                raise ValueError("no powered USB Bluetooth adapters available for scanning")
            results = await asyncio.gather(*(scan(item, seconds) for item in self.adapters.values()))
            for adapter_id, motors in zip(self.adapters, results):
                for motor in motors:
                    key = opaque_id(motor.address)
                    if key in self.devices:
                        existing = self.devices[key]
                        existing.observation = motor
                        if existing.access_code is None:
                            existing.access_code = self.known_credentials.get(motor.address.upper())
                        continue
                    default_id = slug(motor.name)
                    saved = self.saved_setup.devices.get(motor.address.upper())
                    preferred_adapter = (
                        saved.preferred_adapter
                        if saved and (saved.preferred_adapter in self.adapters or
                                      saved.preferred_adapter in self.saved_setup.adapters)
                        else adapter_id
                    )
                    self.devices[key] = DeviceSetup(
                        motor,
                        saved.id if saved else default_id,
                        saved.friendly_name if saved else motor.name,
                        saved.dual_rail if saved else motor.motor_type_candidate == "tdbu_candidate",
                        preferred_adapter,
                        groups=saved.groups if saved else (),
                        configured=saved is not None,
                        access_code=(saved.access_code if saved else
                                     self.known_credentials.get(motor.address.upper())),
                    )

    async def start_scan(self, seconds: float) -> None:
        if self.scan_task and not self.scan_task.done():
            raise ValueError("a scan is already running")
        task = asyncio.create_task(self.scan_all(seconds))
        self.scan_task = task
        self.scan_started_at = time.monotonic()
        self.scan_duration_s = seconds
        task.add_done_callback(self._scan_finished)
        await asyncio.shield(task)

    def _scan_finished(self, task: asyncio.Task[None]) -> None:
        if self.scan_task is task:
            self.scan_task = None
            self.scan_started_at = None
            self.scan_duration_s = None
        if not task.cancelled():
            task.exception()

    async def cancel_scan(self) -> bool:
        task = self.scan_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def clear_scan_results(self) -> int:
        if self.ble_lock.locked():
            raise ValueError("wait for the active Bluetooth operation to finish")
        count = len(self.devices)
        self.devices = self._saved_devices()
        return count

    def scan_state(self) -> dict[str, Any]:
        active = self.scan_task is not None and not self.scan_task.done()
        duration = self.scan_duration_s or 0
        elapsed = time.monotonic() - self.scan_started_at if active and self.scan_started_at else 0
        return {
            "active": active,
            "duration_s": duration,
            "remaining_s": max(0, duration - elapsed),
        }

    def adapter_state(self) -> dict[str, Any]:
        def row(adapter_id: str, adapter: AdapterIdentity) -> dict[str, Any]:
            return {
                "id": adapter_id, "usb_path": adapter.usb_path,
                "product": adapter.usb_product, "powered": adapter.powered,
            }

        missing = self.missing_adapters
        missing_rows = []
        for adapter_id in sorted(self._missing_adapter_ids()):
            adapter = self._known_adapters.get(adapter_id)
            saved_path = self.saved_setup.adapters.get(adapter_id, {}).get("usb_path")
            missing_rows.append({
                "id": adapter_id,
                "usb_path": adapter.usb_path if adapter else saved_path,
                "product": adapter.usb_product if adapter else None,
                "powered": False,
            })
        return {
            "adapters": [row(key, adapter) for key, adapter in self.adapters.items()] + missing_rows,
            "available_adapters": [
                row(key, adapter) for key, adapter in self._detected_adapters.items()
                if key not in self.adapters
            ],
            "can_export": bool(self.adapters) and self.export_dir is not None and not missing,
            "missing_adapters": missing,
        }

    def state(self, *, allow_jog: bool) -> dict[str, Any]:
        return {
            **self.adapter_state(),
            "devices": [item.browser_dict(self.allow_code_read) for item in self.devices.values()],
            "groups": [{"id": group_id, "name": name}
                       for group_id, name in self.groups.items()],
            "allow_jog": allow_jog,
            "busy": self.ble_lock.locked(),
            "scan": self.scan_state(),
            "allow_code_read": self.allow_code_read,
            "can_exit_setup": bool(self.saved_setup.adapters),
            "loaded_saved_devices": len(self.saved_setup.devices),
        }

    def configure(self, key: str, body: dict[str, Any]) -> None:
        device = self.devices[key]
        old_name = device.friendly_name
        device.friendly_name = str(body.get("name", device.friendly_name)).strip() or device.friendly_name
        generated_id = device.id if device.friendly_name == old_name else slug(device.friendly_name)
        if any(other is not device and other.id == generated_id for other in self.devices.values()):
            raise ValueError(f"another device already uses generated id {generated_id!r}")
        device.id = generated_id
        device.dual_rail = bool(body.get("dual_rail", device.dual_rail))
        requested = body.get("preferred_adapter", device.preferred_adapter) or None
        if requested is not None and requested not in self.adapters and not (
            requested == device.preferred_adapter and requested in self.saved_setup.adapters
        ):
            raise ValueError("unknown adapter assignment")
        device.preferred_adapter = requested
        requested_groups = body.get("groups")
        if requested_groups is None and "group" in body:
            legacy_group = str(body.get("group", "")).strip()
            requested_groups = [legacy_group] if legacy_group else []
        if requested_groups is None:
            requested_groups = [self.groups.get(group_id, group_id) for group_id in device.groups]
        if not isinstance(requested_groups, list) or len(requested_groups) > 20:
            raise ValueError("groups must be a list of at most 20 names")
        normalized_groups: list[str] = []
        for value in requested_groups:
            friendly = " ".join(str(value).split())
            if not friendly:
                continue
            if len(friendly) > 60 or not re.search(r"[A-Za-z0-9]", friendly):
                raise ValueError("group names must contain a letter or number and be at most 60 characters")
            group_id = next((key for key, name in self.groups.items() if name == friendly),
                            slug(friendly))
            if group_id not in self.groups:
                self.groups[group_id] = friendly
            if group_id not in normalized_groups:
                normalized_groups.append(group_id)
        device.groups = tuple(normalized_groups)
        device.enabled = bool(body.get("enabled", device.enabled))
        device.configured = True
        code = str(body.get("access_code", ""))
        if code:
            if len(code) != 7 or not code.isascii() or not code.isdigit():
                raise ValueError("access code must be seven ASCII digits")
            device.access_code = code
        elif (self.allow_code_read and body.get("use_candidate") is True and
              device.observation.candidate_access_code):
            device.access_code = device.observation.candidate_access_code

    async def inspect(self, key: str) -> None:
        async with self.ble_lock:
            device = self.devices[key]
            adapter = self.adapter_for(device.preferred_adapter)
            rescanned = await scan(adapter, 5)
            matching = next((item for item in rescanned if item.address == device.observation.address), None)
            if matching is None:
                raise ValueError("device was not rediscovered on the selected adapter")
            device.observation = matching
            await inspect_motor(device.observation, adapter, self.allow_code_read)

    def adapter_for(self, adapter_id: str) -> AdapterIdentity:
        try:
            return self.adapters[adapter_id]
        except KeyError as exc:
            raise ValueError(f"Bluetooth adapter {adapter_id} is unavailable") from exc

    def export(self) -> tuple[Path, Path]:
        if self.export_dir is None:
            raise ValueError("setup saving was not enabled at launch")
        if self.ble_lock.locked():
            raise ValueError("wait for the active Bluetooth operation to finish")
        if self.missing_adapters:
            raise ValueError("Restore all configured Bluetooth adapters before saving.")
        if not self.adapters:
            raise ValueError("no powered USB Bluetooth adapters available for saving")
        selected = [item for item in self.devices.values() if item.enabled]
        if not selected:
            raise ValueError("no devices have been discovered")
        if any(not item.access_code for item in selected):
            raise ValueError("selected shades need access codes before setup can be saved")
        blind_ids = [item.id for item in selected]
        if len(set(blind_ids)) != len(blind_ids):
            raise ValueError("device ids must be unique")

        return write_configuration(self.export_dir, self.adapters, selected, self.groups, self.saved_setup)
