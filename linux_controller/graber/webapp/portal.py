"""Serve the authenticated portal and proxy its active mode."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from graber.security import AuthManager, PasswordStore
from .accounts import AccountRoutes
from .middleware import portal_middleware
from .supervisor import ModeSupervisor


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
PRIVATE_PROXY_HEADERS = {"cookie", "x-csrf-token", "x-graber-control-token"}
PRIVATE_RESPONSE_HEADERS = {"set-cookie"}


def is_command_request(method: str, path: str) -> bool:
    return (
        method == "POST"
        and path.endswith("/commands")
        and (path.startswith("/api/blinds/") or path.startswith("/api/groups/"))
    )


def create_app(supervisor: ModeSupervisor, *, allowed_networks: tuple[str, ...],
               auth_manager: AuthManager,
               trusted_proxies: tuple[str, ...] = ()) -> web.Application:
    app = web.Application(
        middlewares=portal_middleware(allowed_networks, trusted_proxies, auth_manager),
        client_max_size=16 * 1024,
    )
    app[web.AppKey("supervisor", ModeSupervisor)] = supervisor
    client_key = web.AppKey("proxy_client", ClientSession)

    async def lifecycle(application: web.Application):
        application[client_key] = ClientSession(
            timeout=ClientTimeout(total=None), auto_decompress=False
        )
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.close()
            await application[client_key].close()

    app.cleanup_ctx.append(lifecycle)

    async def portal_status(_request: web.Request) -> web.Response:
        return web.json_response({"mode": supervisor.mode})

    changing_mode = False

    async def change_mode(mode: str) -> web.Response:
        nonlocal changing_mode
        if changing_mode or supervisor.active_commands:
            raise web.HTTPConflict(text="wait for the active portal operation to finish")
        changing_mode = True
        try:
            await supervisor.switch(mode)
            return web.json_response({"mode": mode})
        finally:
            changing_mode = False

    async def enter_setup(_request: web.Request) -> web.Response:
        return await change_mode("setup")

    async def exit_setup(_request: web.Request) -> web.Response:
        if not (supervisor.config_path.is_file() and supervisor.secrets_path.is_file()):
            raise web.HTTPConflict(text="save a controller configuration before leaving setup")
        return await change_mode("control")

    async def proxy(request: web.Request) -> web.Response:
        nonlocal changing_mode
        if changing_mode:
            raise web.HTTPConflict(text="wait for the portal to finish switching modes")
        exporting = supervisor.mode == "setup" and request.method == "POST" and request.path == "/api/export"
        if exporting and supervisor.active_commands:
            raise web.HTTPConflict(text="wait for the active setup operation to finish")
        setup_operation = request.method == "POST" and (
            request.path == "/api/scan" or request.path.startswith(("/api/devices/", "/api/adapters/"))
        )
        command_request = is_command_request(request.method, request.path) or setup_operation
        if exporting:
            changing_mode = True
        if command_request:
            supervisor.active_commands += 1
        try:
            await supervisor.ensure_running()
            body = await request.read()
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in HOP_BY_HOP | PRIVATE_PROXY_HEADERS}
            headers["X-Graber-Control-Token"] = supervisor.control_token()
            target = f"http://{supervisor.internal_host}:{supervisor.internal_port}{request.rel_url}"
            async with app[client_key].request(
                request.method, target, data=body, headers=headers, allow_redirects=False
            ) as upstream:
                response_body = await upstream.read()
                status = upstream.status
                response_headers = {key: value for key, value in upstream.headers.items()
                                    if key.lower() not in HOP_BY_HOP | PRIVATE_RESPONSE_HEADERS}
            if exporting and 200 <= status < 300:
                await supervisor.switch("control")
                try:
                    payload = json.loads(response_body)
                    payload["mode"] = "control"
                    response_body = json.dumps(payload).encode("utf-8")
                    response_headers["Content-Type"] = "application/json"
                except (TypeError, ValueError):
                    pass
            return web.Response(body=response_body, status=status, headers=response_headers)
        except (OSError, asyncio.TimeoutError) as exc:
            raise web.HTTPServiceUnavailable(text="active mode is unavailable") from exc
        finally:
            if command_request:
                supervisor.active_commands -= 1
            if exporting:
                changing_mode = False

    AccountRoutes(auth_manager).register(app)
    app.router.add_get("/api/portal/status", portal_status)
    app.router.add_post("/api/portal/setup", enter_setup)
    app.router.add_post("/api/portal/control", exit_setup)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allowed-network", action="append", required=True)
    parser.add_argument("--internal-host", default="127.0.0.1")
    parser.add_argument("--internal-port", type=int, default=18080)
    parser.add_argument("--setup-runner", required=True, type=Path)
    parser.add_argument("--setup-settings", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--secrets", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--auth-file", required=True, type=Path)
    parser.add_argument("--session-ttl-s", type=float, default=12 * 60 * 60)
    parser.add_argument("--secure-cookie", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--trusted-proxy", action="append", default=[])
    parser.add_argument("--mode-file", required=True, type=Path)
    args = parser.parse_args(argv)
    supervisor = ModeSupervisor(
        setup_runner=args.setup_runner, setup_settings=args.setup_settings,
        config_path=args.config, secrets_path=args.secrets, token_file=args.token_file,
        mode_file=args.mode_file, internal_host=args.internal_host, internal_port=args.internal_port,
    )
    auth_manager = AuthManager(
        PasswordStore(args.auth_file), secure_cookie=args.secure_cookie,
        session_ttl_s=args.session_ttl_s,
        session_path=args.auth_file.with_name("sessions.json"),
    )
    web.run_app(create_app(
                    supervisor, allowed_networks=tuple(args.allowed_network),
                    trusted_proxies=tuple(args.trusted_proxy), auth_manager=auth_manager,
                ),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
