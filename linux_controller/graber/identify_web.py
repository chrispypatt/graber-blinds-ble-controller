"""LAN-restricted browser wizard for identification and private configuration."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import secrets
import stat
import time
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapter_manager import AdapterManager, ResolvedAdapter
from .ble_transport import BleakTransport
from .blind import BlindCommand, BlindController, Outcome
from .config import AdapterConfig, BlindConfig, RetryConfig, load_config
from .diagnostics import AdapterIdentity, discover_adapters
from .identify_cli import MotorObservation, inspect_motor, scan
from .protocol import CommandKind, Rail, raw_to_ha_position


COMMON_GROUP_NAMES = (
    "Living Room", "Family Room", "Great Room", "Dining Room", "Kitchen",
    "Primary Bedroom", "Bedroom", "Guest Bedroom", "Nursery", "Office", "Den",
    "Media Room", "Game Room", "Sunroom", "Basement", "Upstairs", "Downstairs",
    "Front of House", "Back of House", "All Bedrooms", "All Common Areas", "Whole House",
)


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return result or "blind"


def opaque_id(address: str) -> str:
    import hashlib
    return hashlib.sha256(address.encode("utf-8")).hexdigest()[:12]


def load_known_credentials(path: str | Path) -> dict[str, str]:
    """Load address-to-code mappings without exposing them to browser state.

    The accepted format is the controller's normal private secrets TOML. Blind
    table names are informational; Bluetooth addresses are the match key.
    """

    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError("known-secrets path must be a readable regular file, not a symlink") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("known-secrets path must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError("known-secrets file must be owned by the wizard user")
        if stat.S_IMODE(metadata.st_mode) & 0o177:
            raise ValueError("known-secrets file must have mode 0600 or stricter")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = tomllib.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    blinds = raw.get("blinds", {})
    if not isinstance(blinds, dict):
        raise ValueError("known-secrets file requires a [blinds] table")
    credentials: dict[str, str] = {}
    for blind_id, item in blinds.items():
        if not isinstance(item, dict):
            raise ValueError(f"known secret for {blind_id} must be a table")
        address = str(item.get("address", "")).upper()
        code = str(item.get("access_code", ""))
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ValueError(f"known secret for {blind_id} has an invalid Bluetooth address")
        if re.fullmatch(r"[0-9]{7}", code) is None:
            raise ValueError(f"known secret for {blind_id} has an invalid access code")
        if address in credentials:
            raise ValueError("known-secrets file contains a duplicate Bluetooth address")
        credentials[address] = code
    return credentials


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


@dataclass(frozen=True)
class SavedDeviceSetup:
    id: str
    friendly_name: str
    dual_rail: bool
    preferred_adapter: str | None
    groups: tuple[str, ...]
    access_code: str = field(repr=False)
    config: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SavedSetup:
    devices: dict[str, SavedDeviceSetup]
    groups: dict[str, str]
    config_base: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets_base: dict[str, Any] = field(default_factory=dict, repr=False)
    adapters: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    group_configs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)


def load_saved_setup(config_path: str | Path, secrets_path: str | Path) -> SavedSetup:
    """Load a previously exported setup without exposing credentials to the UI."""

    config = load_config(config_path, secrets_path)
    with Path(config_path).open("rb") as stream:
        config_base = tomllib.load(stream)
    with Path(secrets_path).open("rb") as stream:
        secrets_base = tomllib.load(stream)
    adapters = {row["id"]: row for row in config_base.pop("adapters", [])}
    blind_configs = {row["id"]: row for row in config_base.pop("blinds", [])}
    group_configs = {row["id"]: row for row in config_base.pop("groups", [])}
    blind_secrets = secrets_base.pop("blinds", {})
    memberships: dict[str, list[str]] = {blind.id: [] for blind in config.blinds}
    groups = {group.id: group.name for group in config.groups}
    for group in config.groups:
        for blind_id in group.members:
            memberships[blind_id].append(group.id)
    devices = {
        blind.address.upper(): SavedDeviceSetup(
            id=blind.id,
            friendly_name=blind.name,
            dual_rail=blind.dual_rail,
            preferred_adapter=blind.preferred_adapter,
            groups=tuple(memberships[blind.id]),
            access_code=blind.access_code,
            config=blind_configs[blind.id],
            secrets=blind_secrets[blind.id],
        )
        for blind in config.blinds
    }
    return SavedSetup(devices=devices, groups=groups,
                      config_base=config_base, secrets_base=secrets_base, adapters=adapters,
                      group_configs=group_configs)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            raise ValueError("nested TOML table arrays are not supported in preserved settings")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported preserved TOML value type: {type(value).__name__}")


def _serialize_toml_tables(raw: dict[str, Any]) -> list[str]:
    """Serialize non-topology settings loaded by tomllib without moving secrets."""

    lines: list[str] = []

    def write_table(path: tuple[str, ...], table: dict[str, Any]) -> None:
        scalars = {key: value for key, value in table.items() if not isinstance(value, dict)}
        children = {key: value for key, value in table.items() if isinstance(value, dict)}
        if path:
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        if path or scalars:
            lines.append("")
        for key, value in children.items():
            write_table((*path, str(key)), value)

    write_table((), raw)
    return lines


def _serialize_toml_row(table: str, row: dict[str, Any]) -> list[str]:
    return [f"[[{table}]]", *(
        f"{_toml_key(key)} = {_toml_value(value)}" for key, value in row.items()
    ), ""]


def _write_text_no_follow(path: Path, content: str, mode: int) -> None:
    """Replace a generated file without following an attacker-controlled symlink."""

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
        self.export_dir.mkdir(parents=True, exist_ok=True)
        config_path, secrets_path = self.export_dir / "config.toml", self.export_dir / "secrets.toml"
        adapter_ids: list[str] = []
        base_config = deepcopy(self.saved_setup.config_base)
        lines = _serialize_toml_tables(base_config)
        if not base_config:
            lines = ["[controller]", "warm_connection_s = 30", "",
                     "[retry]", "attempts = 3", ""]
        for adapter_id, adapter in self.adapters.items():
            adapter_ids.append(adapter_id)
            settings = {"max_connections": 5, "max_cold_connections": 1}
            settings.update(self.saved_setup.adapters.get(adapter_id, {}))
            settings.update(id=adapter_id, controller_address=adapter.address)
            if adapter.usb_path:
                settings["usb_path"] = adapter.usb_path
            lines.extend(_serialize_toml_row("adapters", settings))
        secret_rows: dict[str, dict[str, Any]] = {}
        groups: dict[str, list[str]] = {}
        for item in selected:
            address = item.observation.address.upper()
            saved = self.saved_setup.devices.get(address)
            settings = dict(saved.config) if saved else {}
            if not saved or saved.preferred_adapter != item.preferred_adapter:
                settings["fallback_adapters"] = [
                    value for value in adapter_ids if value != item.preferred_adapter
                ]
            settings.update(id=item.id, name=item.friendly_name, dual_rail=item.dual_rail)
            if item.preferred_adapter is None:
                settings.pop("preferred_adapter", None)
            else:
                settings["preferred_adapter"] = item.preferred_adapter
            lines.extend(_serialize_toml_row("blinds", settings))
            credentials = dict(saved.secrets) if saved else {}
            credentials.update(address=address, access_code=item.access_code)
            secret_rows[item.id] = credentials
        for item in selected:
            for group in item.groups:
                groups.setdefault(group, []).append(item.id)
        for group, members in groups.items():
            settings = {"mode": "best_effort", **self.saved_setup.group_configs.get(group, {})}
            existing_order = settings.get("members", [])
            ordered = [member for member in existing_order if member in members]
            ordered.extend(member for member in members if member not in ordered)
            settings.update(id=group, name=self.groups.get(group, group.replace("_", " ").title()),
                            members=ordered)
            lines.extend(_serialize_toml_row("groups", settings))
        if not base_config:
            lines.extend(["[mqtt]", "enabled = false", "", "[web]", "enabled = false", ""])
        secret_lines = _serialize_toml_tables({**self.saved_setup.secrets_base, "blinds": secret_rows})
        config_content = "\n".join(lines) + "\n"
        secrets_content = "\n".join(secret_lines)
        _write_text_no_follow(config_path, config_content, 0o600)
        _write_text_no_follow(secrets_path, secrets_content, 0o600)
        return config_path, secrets_path


async def jog_device(session: SetupSession, key: str) -> dict[str, Any]:
    setup = session.devices[key]
    if not setup.access_code:
        raise ValueError("save a verified access code before jogging")
    current = setup.observation.bottom_raw
    if current is None:
        raise ValueError("a current bottom position is required before jogging")
    adapter = session.adapter_for(setup.preferred_adapter)
    adapter_config = AdapterConfig(setup.preferred_adapter, adapter.address or "", adapter.usb_path,
                                   max_connections=1, max_cold_connections=1)

    async def resolver(_configs: tuple[AdapterConfig, ...]) -> list[ResolvedAdapter]:
        return [ResolvedAdapter(adapter_config.id, adapter_config.controller_address,
                                adapter.hci_name, adapter.usb_path)]

    manager = AdapterManager((adapter_config,), resolver)
    await manager.refresh()
    config = BlindConfig(setup.id, setup.friendly_name, setup.observation.address,
                         setup.dual_rail, setup.access_code, setup.preferred_adapter)
    blind = BlindController(config, manager, BleakTransport(),
                            RetryConfig(attempts=2, feedback_timeout_s=20), warm_connection_s=2)
    target = current + 5 if current <= 95 else current - 5
    try:
        outward = await blind.execute(BlindCommand(
            f"identify-{secrets.token_hex(8)}", CommandKind.SET_POSITION, Rail.BOTTOM, target
        ))
        if outward.outcome is not Outcome.COMPLETED:
            await blind.execute(BlindCommand(f"identify-stop-{secrets.token_hex(8)}", CommandKind.STOP))
            return {"status": outward.outcome.value, "restored": False, "error": outward.error}
        await asyncio.sleep(0.5)
        restored = await blind.execute(BlindCommand(
            f"identify-restore-{secrets.token_hex(8)}", CommandKind.SET_POSITION, Rail.BOTTOM, current
        ))
        if restored.outcome is not Outcome.COMPLETED:
            await blind.execute(BlindCommand(f"identify-stop-{secrets.token_hex(8)}", CommandKind.STOP))
        return {"status": outward.outcome.value, "restored": restored.outcome is Outcome.COMPLETED,
                "restore_status": restored.outcome.value}
    finally:
        await blind.close()


@dataclass(frozen=True)
class SetupWebSettings:
    host: str = "127.0.0.1"
    port: int = 8090
    allowed_networks: tuple[str, ...] = ("127.0.0.0/8", "::1/128")
    allow_jog: bool = False
    control_token: str | None = field(default=None, repr=False)


def create_app(session: SetupSession, settings: SetupWebSettings,
               jogger: Callable[[SetupSession, str], Awaitable[dict[str, Any]]] = jog_device) -> Any:
    from aiohttp import web
    networks = tuple(ipaddress.ip_network(value) for value in settings.allowed_networks)
    package_root = Path(__file__).parent
    template = (package_root / "templates" / "identify.html").read_text(encoding="utf-8")
    stylesheet = (package_root / "static" / "app.css").read_text(encoding="utf-8")
    javascript = (package_root / "static" / "setup.js").read_text(encoding="utf-8")

    @web.middleware
    async def guard(request: Any, handler: Any) -> Any:
        try:
            remote = ipaddress.ip_address((request.remote or "").split("%", 1)[0])
        except ValueError:
            return web.json_response({"error": "invalid client address"}, status=403)
        if not any(remote in network for network in networks):
            return web.json_response({"error": "outside allowed networks"}, status=403)
        try:
            response = await handler(request)
        except (KeyError, ValueError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    def authorize(request: Any) -> None:
        if not settings.control_token or not secrets.compare_digest(
            request.headers.get("X-Graber-Control-Token", ""), settings.control_token
        ):
            raise web.HTTPForbidden(text="invalid control token")

    async def index(_request: Any) -> Any:
        return web.Response(text=template, content_type="text/html")

    async def css(_request: Any) -> Any:
        return web.Response(text=stylesheet, content_type="text/css")

    async def js(_request: Any) -> Any:
        return web.Response(text=javascript, content_type="application/javascript")

    async def state(_request: Any) -> Any:
        authorize(_request)
        return web.json_response(session.state(allow_jog=settings.allow_jog))

    async def adapters(request: Any) -> Any:
        authorize(request)
        await session.refresh_adapters()
        return web.json_response(session.adapter_state())

    async def add_adapter(request: Any) -> Any:
        authorize(request)
        await session.add_adapter(request.match_info["adapter_id"])
        return web.json_response(session.adapter_state())

    async def start_scan(request: Any) -> Any:
        authorize(request)
        body = await request.json()
        cancelled = False
        try:
            await session.start_scan(float(body.get("seconds", 12)))
        except asyncio.CancelledError:
            cancelled = True
        payload = session.state(allow_jog=settings.allow_jog)
        payload["cancelled"] = cancelled
        return web.json_response(payload)

    async def cancel_scan(request: Any) -> Any:
        authorize(request)
        return web.json_response({"cancelled": await session.cancel_scan()})

    async def clear_scan(request: Any) -> Any:
        authorize(request)
        cleared = session.clear_scan_results()
        payload = session.state(allow_jog=settings.allow_jog)
        payload["cleared"] = cleared
        return web.json_response(payload)

    async def configure(request: Any) -> Any:
        authorize(request)
        key = request.match_info["key"]
        session.configure(key, await request.json())
        return web.json_response({
            "saved": True,
            "device": session.devices[key].browser_dict(session.allow_code_read),
            "groups": [{"id": group_id, "name": name}
                       for group_id, name in session.groups.items()],
        })

    async def inspect(request: Any) -> Any:
        authorize(request)
        await session.inspect(request.match_info["key"])
        return web.json_response({"inspected": True})

    async def jog(request: Any) -> Any:
        authorize(request)
        if not settings.allow_jog:
            raise web.HTTPForbidden(text="jogging disabled at launch")
        key = request.match_info["key"]
        if session.ble_lock.locked():
            raise web.HTTPConflict(text="another setup Bluetooth operation is already running")
        async with session.ble_lock:
            try:
                result = await asyncio.wait_for(jogger(session, key), timeout=75)
            except asyncio.TimeoutError as exc:
                raise web.HTTPGatewayTimeout(
                    text="jog timed out; the setup lock was released"
                ) from exc
            except Exception as exc:
                raise web.HTTPInternalServerError(
                    text=f"jog failed during {type(exc).__name__}; the setup lock was released"
                ) from exc
            return web.json_response(result)

    async def export(request: Any) -> Any:
        authorize(request)
        await session.refresh_adapters()
        config_path, secrets_path = session.export()
        return web.json_response({"config": str(config_path), "secrets": str(secrets_path)})

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/assets/app.css", css)
    app.router.add_get("/assets/setup.js", js)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/adapters", adapters)
    app.router.add_post("/api/adapters/{adapter_id}", add_adapter)
    app.router.add_post("/api/scan", start_scan)
    app.router.add_post("/api/scan/cancel", cancel_scan)
    app.router.add_post("/api/scan/clear", clear_scan)
    app.router.add_post("/api/devices/{key}/configure", configure)
    app.router.add_post("/api/devices/{key}/inspect", inspect)
    app.router.add_post("/api/devices/{key}/jog", jog)
    app.router.add_post("/api/export", export)
    return app


def main(argv: list[str] | None = None) -> None:
    from aiohttp import web
    parser = argparse.ArgumentParser(description="LAN Graber identification/setup wizard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--allowed-network", action="append", default=[])
    parser.add_argument("--controller-address", action="append",
                        help="external USB controller address; repeat to select several "
                             "(default: all powered USB controllers)")
    parser.add_argument("--allow-code-read", action="store_true")
    parser.add_argument("--allow-jog", action="store_true")
    parser.add_argument("--export-dir")
    parser.add_argument("--known-secrets",
                        help="mode-0600 controller secrets TOML used to prefill codes by address")
    parser.add_argument("--existing-config",
                        help="existing exported controller config to restore after scanning")
    parser.add_argument("--existing-secrets",
                        help="existing exported mode-0600 secrets paired with --existing-config")
    parser.add_argument("--group-name", action="append", default=[],
                        help="friendly group choice; repeat for each configured group")
    args = parser.parse_args(argv)
    token = os.environ.get("GRABER_SETUP_TOKEN")
    if not token or len(token) < 16:
        raise SystemExit("set GRABER_SETUP_TOKEN to at least 16 characters")
    networks = tuple(args.allowed_network or (["127.0.0.0/8", "::1/128"] if args.host in {"127.0.0.1", "::1"} else []))
    if not networks:
        raise SystemExit("at least one --allowed-network is required for a non-loopback bind")
    settings = SetupWebSettings(args.host, args.port, networks, args.allow_jog, token)
    known_credentials = load_known_credentials(args.known_secrets) if args.known_secrets else {}
    if bool(args.existing_config) != bool(args.existing_secrets):
        raise SystemExit("--existing-config and --existing-secrets must be provided together")
    saved_setup = (
        load_saved_setup(args.existing_config, args.existing_secrets)
        if args.existing_config else None
    )
    group_names = tuple(args.group_name or ())
    try:
        session = SetupSession(
            discover_adapters(), allow_code_read=args.allow_code_read, export_dir=args.export_dir,
            known_credentials=known_credentials, group_names=group_names,
            saved_setup=saved_setup, controller_addresses=args.controller_address,
            adapter_discovery=discover_adapters,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    web.run_app(create_app(session, settings),
                host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
