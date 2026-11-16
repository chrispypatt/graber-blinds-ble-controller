"""Single-port supervisor for mutually exclusive setup and control modes."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def is_command_request(method: str, path: str) -> bool:
    return (
        method == "POST"
        and path.endswith("/commands")
        and (path.startswith("/api/blinds/") or path.startswith("/api/groups/"))
    )


class ModeSupervisor:
    def __init__(self, *, setup_runner: Path, setup_settings: Path,
                 config_path: Path, secrets_path: Path, token_file: Path,
                 mode_file: Path, internal_host: str, internal_port: int) -> None:
        self.setup_runner = setup_runner
        self.setup_settings = setup_settings
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.token_file = token_file
        self.mode_file = mode_file
        self.internal_host = internal_host
        self.internal_port = internal_port
        self.mode = "setup"
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.active_commands = 0

    def saved_mode(self) -> str:
        try:
            value = self.mode_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "setup"
        return value if value in {"setup", "control"} else "setup"

    def control_token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    async def start(self) -> None:
        self.mode = self.saved_mode()
        if self.mode == "control" and not (self.config_path.exists() and self.secrets_path.exists()):
            self.mode = "setup"
        await self._start_child(self.mode)

    async def close(self) -> None:
        async with self.lock:
            await self._stop_child()

    async def ensure_running(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self._start_child(self.mode)

    async def switch(self, mode: str) -> None:
        if mode not in {"setup", "control"}:
            raise ValueError("invalid portal mode")
        async with self.lock:
            if mode == self.mode and self.process is not None and self.process.returncode is None:
                return
            previous = self.mode
            await self._stop_child()
            try:
                await self._start_child(mode)
            except Exception:
                await self._start_child(previous)
                raise
            self.mode = mode
            self.mode_file.parent.mkdir(parents=True, exist_ok=True)
            self.mode_file.write_text(mode + "\n", encoding="utf-8")
            os.chmod(self.mode_file, 0o600)

    async def _start_child(self, mode: str) -> None:
        environment = os.environ.copy()
        if mode == "setup":
            environment.update({
                "GRABER_SETUP_BIND_HOST": self.internal_host,
                "GRABER_SETUP_BIND_PORT": str(self.internal_port),
                "GRABER_SETUP_BIND_ALLOWED_NETWORKS": "127.0.0.0/8,::1/128",
                "GRABER_SETUP_EXISTING_CONFIG": str(self.config_path),
                "GRABER_SETUP_EXISTING_SECRETS": str(self.secrets_path),
            })
            command = (str(self.setup_runner), str(self.setup_settings))
        else:
            command = (
                sys.executable, "-m", "graber.main",
                "--config", str(self.config_path), "--secrets", str(self.secrets_path),
                "--portal-host", self.internal_host, "--portal-port", str(self.internal_port),
                "--portal-allowed-network", "127.0.0.0/8",
                "--portal-allowed-network", "::1/128",
                "--portal-control-token-file", str(self.token_file),
            )
        self.process = await asyncio.create_subprocess_exec(*command, env=environment)
        await self._wait_ready(mode)

    async def _wait_ready(self, mode: str) -> None:
        deadline = asyncio.get_running_loop().time() + 20
        headers = {"X-Graber-Control-Token": self.control_token()} if mode == "setup" else {}
        async with ClientSession(timeout=ClientTimeout(total=1)) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self.process is None or self.process.returncode is not None:
                    raise RuntimeError(f"{mode} service exited during startup")
                try:
                    async with client.get(
                        f"http://{self.internal_host}:{self.internal_port}/api/state",
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            return
                except (OSError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.2)
        await self._stop_child()
        raise RuntimeError(f"{mode} service did not become ready")

    async def _stop_child(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def create_app(supervisor: ModeSupervisor, *, allowed_networks: tuple[str, ...]) -> web.Application:
    networks = tuple(ipaddress.ip_network(value) for value in allowed_networks)

    @web.middleware
    async def lan_only(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            address = ipaddress.ip_address((request.remote or "").split("%", 1)[0])
        except ValueError:
            raise web.HTTPForbidden(text="client is outside allowed networks")
        if not any(address in network for network in networks):
            raise web.HTTPForbidden(text="client is outside allowed networks")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    app = web.Application(middlewares=[lan_only])
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

    def authorize(request: web.Request) -> None:
        supplied = request.headers.get("X-Graber-Control-Token", "")
        if not secrets.compare_digest(supplied, supervisor.control_token()):
            raise web.HTTPForbidden(text="invalid control token")

    async def portal_status(_request: web.Request) -> web.Response:
        return web.json_response({"mode": supervisor.mode})

    async def enter_setup(request: web.Request) -> web.Response:
        authorize(request)
        if supervisor.active_commands:
            raise web.HTTPConflict(text="wait for the active shade command to finish")
        await supervisor.switch("setup")
        return web.json_response({"mode": "setup"})

    async def proxy(request: web.Request) -> web.Response:
        await supervisor.ensure_running()
        command_request = is_command_request(request.method, request.path)
        if command_request:
            supervisor.active_commands += 1
        try:
            body = await request.read()
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in HOP_BY_HOP}
            target = f"http://{supervisor.internal_host}:{supervisor.internal_port}{request.rel_url}"
            async with app[client_key].request(
                request.method, target, data=body, headers=headers, allow_redirects=False
            ) as upstream:
                response_body = await upstream.read()
                status = upstream.status
                response_headers = {key: value for key, value in upstream.headers.items()
                                    if key.lower() not in HOP_BY_HOP}
            if (supervisor.mode == "setup" and request.method == "POST" and
                    request.path == "/api/export" and 200 <= status < 300):
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

    app.router.add_get("/api/portal/status", portal_status)
    app.router.add_post("/api/portal/setup", enter_setup)
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
    parser.add_argument("--mode-file", required=True, type=Path)
    args = parser.parse_args(argv)
    supervisor = ModeSupervisor(
        setup_runner=args.setup_runner, setup_settings=args.setup_settings,
        config_path=args.config, secrets_path=args.secrets, token_file=args.token_file,
        mode_file=args.mode_file, internal_host=args.internal_host, internal_port=args.internal_port,
    )
    web.run_app(create_app(supervisor, allowed_networks=tuple(args.allowed_network)),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
