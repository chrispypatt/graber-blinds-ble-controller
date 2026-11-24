"""Serve setup routes and launch the identification wizard."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from graber.diagnostics.adapters import discover_adapters
from graber.webapp import ASSET_ROOT
from .configuration import load_known_credentials, load_saved_setup
from .jog import jog_device
from .session import SetupSession


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
    template = (ASSET_ROOT / "templates" / "identify.html").read_text(encoding="utf-8")
    stylesheet = (ASSET_ROOT / "static" / "app.css").read_text(encoding="utf-8")
    javascript = (ASSET_ROOT / "static" / "setup.js").read_text(encoding="utf-8")

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
