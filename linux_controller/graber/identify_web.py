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
    preferred_adapter: str
    groups: tuple[str, ...] = ()
    enabled: bool = True
    configured: bool = False
    access_code: str | None = field(default=None, repr=False)

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


@dataclass(frozen=True)
class SavedSetup:
    devices: dict[str, SavedDeviceSetup]
    groups: dict[str, str]
    config_base: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets_base: dict[str, Any] = field(default_factory=dict, repr=False)


def load_saved_setup(config_path: str | Path, secrets_path: str | Path) -> SavedSetup:
    """Load a previously exported setup without exposing credentials to the UI."""

    config = load_config(config_path, secrets_path)
    with Path(config_path).open("rb") as stream:
        config_base = tomllib.load(stream)
    with Path(secrets_path).open("rb") as stream:
        secrets_base = tomllib.load(stream)
    for topology_key in ("adapters", "blinds", "groups"):
        config_base.pop(topology_key, None)
    secrets_base.pop("blinds", None)
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
        )
        for blind in config.blinds
    }
    return SavedSetup(devices=devices, groups=groups,
                      config_base=config_base, secrets_base=secrets_base)


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
                        controller_addresses: list[str] | None) -> list[AdapterIdentity]:
    """Select powered USB controllers, or validate an explicit address selection."""

    available = [item for item in discovered if item.is_usb and item.powered is True]
    if not controller_addresses:
        if not available:
            raise ValueError("no powered USB Bluetooth adapters found")
        return available
    wanted = {value.upper() for value in controller_addresses}
    selected = [item for item in available if (item.address or "").upper() in wanted]
    found = {(item.address or "").upper() for item in selected}
    if found != wanted:
        raise ValueError(f"configured USB controller(s) unavailable: {sorted(wanted - found)}")
    return selected


class SetupSession:
    def __init__(self, adapters: list[AdapterIdentity], *, allow_code_read: bool = False,
                 export_dir: str | Path | None = None,
                 known_credentials: dict[str, str] | None = None,
                 group_names: tuple[str, ...] = (),
                 saved_setup: SavedSetup | None = None):
        self.adapters = {item.address or item.hci_name: item for item in adapters}
        self.allow_code_read = allow_code_read
        self.export_dir = Path(export_dir).resolve() if export_dir else None
        self.known_credentials = dict(known_credentials or {})
        self.saved_setup = saved_setup or SavedSetup({}, {})
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
        self.devices: dict[str, DeviceSetup] = {}
        self.ble_lock = asyncio.Lock()
        self.scan_task: asyncio.Task[None] | None = None
        self.scan_started_at: float | None = None
        self.scan_duration_s: float | None = None

    async def scan_all(self, seconds: float = 12) -> None:
        async with self.ble_lock:
            results = await asyncio.gather(*(scan(item, seconds) for item in self.adapters.values()))
            for adapter, motors in zip(self.adapters.values(), results):
                adapter_id = slug(f"usb_{adapter.usb_path or adapter.address or adapter.hci_name}")
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
                    valid_adapters = {
                        slug(f"usb_{item.usb_path or item.address or item.hci_name}")
                        for item in self.adapters.values()
                    }
                    preferred_adapter = (
                        saved.preferred_adapter
                        if saved and saved.preferred_adapter in valid_adapters
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
        self.devices.clear()
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

    def state(self, *, allow_jog: bool) -> dict[str, Any]:
        return {
            "adapters": [
                {
                    "id": slug(f"usb_{item.usb_path or item.address or item.hci_name}"),
                    "usb_path": item.usb_path,
                    "product": item.usb_product,
                    "powered": item.powered,
                }
                for item in self.adapters.values()
            ],
            "devices": [item.browser_dict(self.allow_code_read) for item in self.devices.values()],
            "groups": [{"id": group_id, "name": name}
                       for group_id, name in self.groups.items()],
            "allow_jog": allow_jog,
            "busy": self.ble_lock.locked(),
            "scan": self.scan_state(),
            "allow_code_read": self.allow_code_read,
            "can_export": self.export_dir is not None,
        }

    def configure(self, key: str, body: dict[str, Any]) -> None:
        device = self.devices[key]
        device.friendly_name = str(body.get("name", device.friendly_name)).strip() or device.friendly_name
        generated_id = slug(device.friendly_name)
        if any(other is not device and other.id == generated_id for other in self.devices.values()):
            raise ValueError(f"another device already uses generated id {generated_id!r}")
        device.id = generated_id
        device.dual_rail = bool(body.get("dual_rail", device.dual_rail))
        requested = str(body.get("preferred_adapter", device.preferred_adapter))
        valid = {slug(f"usb_{x.usb_path or x.address or x.hci_name}") for x in self.adapters.values()}
        if requested not in valid:
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
            group_id = slug(friendly)
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
        for item in self.adapters.values():
            if slug(f"usb_{item.usb_path or item.address or item.hci_name}") == adapter_id:
                return item
        raise KeyError(adapter_id)

    def export(self) -> tuple[Path, Path]:
        if self.export_dir is None:
            raise ValueError("setup saving was not enabled at launch")
        selected = [item for item in self.devices.values() if item.enabled]
        if not selected:
            raise ValueError("no devices have been discovered")
        if any(not item.access_code for item in selected):
            raise ValueError("selected shades need access codes before setup can be saved")
        if len({item.id for item in selected}) != len(selected):
            raise ValueError("device ids must be unique")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        config_path, secrets_path = self.export_dir / "config.toml", self.export_dir / "secrets.toml"
        adapter_ids: list[str] = []
        base_config = deepcopy(self.saved_setup.config_base)
        lines = _serialize_toml_tables(base_config)
        if not base_config:
            lines = ["[controller]", "warm_connection_s = 30", "",
                     "[retry]", "attempts = 3", ""]
        for adapter in self.adapters.values():
            adapter_id = slug(f"usb_{adapter.usb_path or adapter.address or adapter.hci_name}")
            adapter_ids.append(adapter_id)
            adapter_lines = [
                "[[adapters]]", f'id = {json.dumps(adapter_id)}',
                f'controller_address = {json.dumps(adapter.address)}',
            ]
            if adapter.usb_path:
                adapter_lines.append(f'usb_path = {json.dumps(adapter.usb_path)}')
            adapter_lines.extend(["max_connections = 5", "max_cold_connections = 1", ""])
            lines.extend(adapter_lines)
        for item in selected:
            fallbacks = [value for value in adapter_ids if value != item.preferred_adapter]
            lines.extend([
                "[[blinds]]", f'id = {json.dumps(item.id)}', f'name = {json.dumps(item.friendly_name)}',
                f'dual_rail = {str(item.dual_rail).lower()}',
                f'preferred_adapter = {json.dumps(item.preferred_adapter)}',
                f'fallback_adapters = {json.dumps(fallbacks)}', "",
            ])
        groups: dict[str, list[str]] = {}
        for item in selected:
            for group in item.groups:
                groups.setdefault(group, []).append(item.id)
        for group, members in groups.items():
            lines.extend([
                "[[groups]]", f'id = {json.dumps(group)}',
                f'name = {json.dumps(self.groups.get(group, group.replace("_", " ").title()))}',
                f'members = {json.dumps(members)}', 'mode = "best_effort"', "",
            ])
        if not base_config:
            lines.extend(["[mqtt]", "enabled = false", "", "[web]", "enabled = false", ""])
        secret_lines = _serialize_toml_tables(deepcopy(self.saved_setup.secrets_base))
        for item in selected:
            secret_lines.extend([
                f"[blinds.{item.id}]", f'address = {json.dumps(item.observation.address)}',
                f'access_code = {json.dumps(item.access_code)}', "",
            ])
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
        config_path, secrets_path = session.export()
        return web.json_response({"config": str(config_path), "secrets": str(secrets_path)})

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/assets/app.css", css)
    app.router.add_get("/assets/setup.js", js)
    app.router.add_get("/api/state", state)
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
    try:
        adapters = select_usb_adapters(discover_adapters(), args.controller_address)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
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
    web.run_app(create_app(SetupSession(adapters, allow_code_read=args.allow_code_read,
                                        export_dir=args.export_dir,
                                        known_credentials=known_credentials,
                                        group_names=group_names,
                                        saved_setup=saved_setup), settings),
                host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
