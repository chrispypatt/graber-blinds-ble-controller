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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapter_manager import AdapterManager, ResolvedAdapter
from .ble_transport import BleakTransport
from .blind import BlindCommand, BlindController, Outcome
from .config import AdapterConfig, BlindConfig, RetryConfig
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


class SetupSession:
    def __init__(self, adapters: list[AdapterIdentity], *, allow_code_read: bool = False,
                 export_dir: str | Path | None = None,
                 known_credentials: dict[str, str] | None = None,
                 group_names: tuple[str, ...] = ()):
        self.adapters = {item.address or item.hci_name: item for item in adapters}
        self.allow_code_read = allow_code_read
        self.export_dir = Path(export_dir).resolve() if export_dir else None
        self.known_credentials = dict(known_credentials or {})
        self.groups: dict[str, str] = {}
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
                    self.devices[key] = DeviceSetup(
                        motor, default_id, motor.name,
                        motor.motor_type_candidate == "tdbu_candidate", adapter_id,
                        configured=False,
                        access_code=self.known_credentials.get(motor.address.upper()),
                    )

    async def start_scan(self, seconds: float) -> None:
        if self.scan_task and not self.scan_task.done():
            raise ValueError("a scan is already running")
        task = asyncio.create_task(self.scan_all(seconds))
        self.scan_task = task
        try:
            await task
        finally:
            if self.scan_task is task:
                self.scan_task = None

    async def cancel_scan(self) -> bool:
        task = self.scan_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

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
        lines = ["[controller]", "warm_connection_s = 30", "", "[retry]", "attempts = 3", ""]
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
        lines.extend(["[mqtt]", "enabled = false", "", "[web]", "enabled = false", ""])
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        secret_lines: list[str] = []
        for item in selected:
            secret_lines.extend([
                f"[blinds.{item.id}]", f'address = {json.dumps(item.observation.address)}',
                f'access_code = {json.dumps(item.access_code)}', "",
            ])
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(secrets_path, flags, 0o600)
        os.chmod(secrets_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(secret_lines))
        os.chmod(secrets_path, 0o600)
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

    async def configure(request: Any) -> Any:
        authorize(request)
        key = request.match_info["key"]
        session.configure(key, await request.json())
        return web.json_response({
            "saved": True,
            "device": session.devices[key].browser_dict(session.allow_code_read),
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
    parser.add_argument("--controller-address", action="append", required=True,
                        help="repeat for each external USB controller")
    parser.add_argument("--allow-code-read", action="store_true")
    parser.add_argument("--allow-jog", action="store_true")
    parser.add_argument("--export-dir")
    parser.add_argument("--known-secrets",
                        help="mode-0600 controller secrets TOML used to prefill codes by address")
    parser.add_argument("--group-name", action="append", default=[],
                        help="friendly group choice; repeat for each configured group")
    args = parser.parse_args(argv)
    token = os.environ.get("GRABER_SETUP_TOKEN")
    if not token or len(token) < 16:
        raise SystemExit("set GRABER_SETUP_TOKEN to at least 16 characters")
    adapters = [item for item in discover_adapters() if item.is_usb and item.powered is True]
    wanted = {value.upper() for value in args.controller_address}
    adapters = [item for item in adapters if item.address in wanted]
    if {item.address for item in adapters} != wanted:
        missing = sorted(wanted - {item.address for item in adapters})
        raise SystemExit(f"configured USB controller(s) unavailable: {missing}")
    if not adapters:
        raise SystemExit("no matching powered USB Bluetooth adapters")
    networks = tuple(args.allowed_network or (["127.0.0.0/8", "::1/128"] if args.host in {"127.0.0.1", "::1"} else []))
    if not networks:
        raise SystemExit("at least one --allowed-network is required for a non-loopback bind")
    settings = SetupWebSettings(args.host, args.port, networks, args.allow_jog, token)
    known_credentials = load_known_credentials(args.known_secrets) if args.known_secrets else {}
    group_names = tuple(args.group_name or ())
    web.run_app(create_app(SetupSession(adapters, allow_code_read=args.allow_code_read,
                                        export_dir=args.export_dir,
                                        known_credentials=known_credentials,
                                        group_names=group_names), settings),
                host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
