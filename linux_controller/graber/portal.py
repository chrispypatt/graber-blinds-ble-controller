"""Single-port supervisor for mutually exclusive setup and control modes."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Mapping

from aiohttp import ClientSession, ClientTimeout, web

from .auth import (
    MAXIMUM_PASSWORD_LENGTH,
    AuthError,
    AuthManager,
    PasswordStore,
    Session,
)


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
PRIVATE_PROXY_HEADERS = {"cookie", "x-csrf-token", "x-graber-control-token"}
PRIVATE_RESPONSE_HEADERS = {"set-cookie"}


def _request_client(request: web.Request, trusted_proxies: tuple[Any, ...]) -> str:
    """Resolve a client address without trusting forwarded headers from LAN peers."""
    try:
        peer = ipaddress.ip_address((request.remote or "").split("%", 1)[0])
    except ValueError:
        return ""
    if not any(peer in network for network in trusted_proxies):
        return str(peer)
    forwarded: list[Any] = []
    for value in request.headers.get("X-Forwarded-For", "").split(","):
        try:
            forwarded.append(ipaddress.ip_address(value.strip().split("%", 1)[0]))
        except ValueError:
            continue
    for candidate in reversed(forwarded):
        if not any(candidate in network for network in trusted_proxies):
            return str(candidate)
    return str(forwarded[0] if forwarded else peer)


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


def create_app(supervisor: ModeSupervisor, *, allowed_networks: tuple[str, ...],
               auth_manager: AuthManager,
               trusted_proxies: tuple[str, ...] = ()) -> web.Application:
    networks = tuple(ipaddress.ip_network(value) for value in allowed_networks)
    proxy_networks = tuple(ipaddress.ip_network(value) for value in trusted_proxies)
    public = {
        ("GET", "/login"),
        ("GET", "/assets/auth.js"),
        ("GET", "/assets/app.css"),
        ("POST", "/api/auth/login"),
    }
    setup_routes = {
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/account"),
        ("POST", "/api/auth/logout"),
    }

    def security_headers(response: web.StreamResponse) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if auth_manager.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"

    @web.middleware
    async def hardened(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            security_headers(exc)
            raise
        security_headers(response)
        return response

    @web.middleware
    async def lan_only(request: web.Request, handler: Any) -> web.StreamResponse:
        client = _request_client(request, proxy_networks)
        request["graber_client"] = client
        try:
            address = ipaddress.ip_address(client)
        except ValueError:
            return web.json_response({"error": "client is outside allowed networks"}, status=403)
        if not any(address in network for network in networks):
            return web.json_response({"error": "client is outside allowed networks"}, status=403)
        return await handler(request)

    def unauthorized(request: web.Request, *, setup_required: bool = False) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            payload = {"error": "authentication required"}
            if setup_required:
                payload = {"error": "owner account setup required", "setup_required": True}
            return web.json_response(payload, status=403 if setup_required else 401)
        raise web.HTTPSeeOther("/login")

    @web.middleware
    async def authenticated(request: web.Request, handler: Any) -> web.StreamResponse:
        route = (request.method, request.path)
        if route in public:
            return await handler(request)
        token = request.cookies.get(auth_manager.cookie_name)
        session = auth_manager.get_session(token)
        if session is None:
            return unauthorized(request)
        request["graber_session"] = session
        request["graber_session_token"] = token
        if session.setup_required and route not in setup_routes:
            return unauthorized(request, setup_required=True)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session.csrf_token):
                return web.json_response({"error": "invalid CSRF token"}, status=403)
        return await handler(request)

    app = web.Application(
        middlewares=[hardened, lan_only, authenticated], client_max_size=16 * 1024
    )
    app[web.AppKey("supervisor", ModeSupervisor)] = supervisor
    client_key = web.AppKey("proxy_client", ClientSession)
    password_checks = asyncio.Semaphore(2)

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

    def session_payload(session: Session) -> dict[str, object]:
        return {
            "authenticated": True,
            "username": session.username,
            "role": session.role,
            "csrf_token": session.csrf_token,
            "setup_required": session.setup_required,
        }

    def set_session_cookie(response: web.StreamResponse, token: str) -> None:
        response.set_cookie(
            auth_manager.cookie_name, token,
            max_age=int(auth_manager.session_ttl_s), path="/",
            secure=auth_manager.secure_cookie, httponly=True, samesite="Strict",
        )

    def clear_session_cookie(response: web.StreamResponse) -> None:
        response.set_cookie(
            auth_manager.cookie_name, "", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT", path="/",
            secure=auth_manager.secure_cookie, httponly=True, samesite="Strict",
        )

    async def login_page(_request: web.Request) -> web.Response:
        template = (Path(__file__).parent / "templates" / "auth.html").read_text(encoding="utf-8")
        return web.Response(text=template, content_type="text/html")

    async def auth_javascript(_request: web.Request) -> web.Response:
        value = (Path(__file__).parent / "static" / "auth.js").read_text(encoding="utf-8")
        return web.Response(text=value, content_type="application/javascript")

    async def stylesheet(_request: web.Request) -> web.Response:
        value = (Path(__file__).parent / "static" / "app.css").read_text(encoding="utf-8")
        return web.Response(text=value, content_type="text/css")

    async def login(request: web.Request) -> web.Response:
        retry_after = auth_manager.limiter.retry_after(request["graber_client"])
        if retry_after:
            return web.json_response(
                {"error": "too many login attempts"}, status=429,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        if not isinstance(body, Mapping):
            return web.json_response({"error": "invalid request"}, status=400)
        username, password = str(body.get("username", "")), str(body.get("password", ""))
        if not 1 <= len(username) <= 64 or not 1 <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
            return web.json_response({"error": "invalid username or password"}, status=401)
        try:
            await asyncio.wait_for(password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            result = await asyncio.to_thread(
                auth_manager.login, request["graber_client"], username, password
            )
        finally:
            password_checks.release()
        if result is None:
            return web.json_response({"error": "invalid username or password"}, status=401)
        token, session = result
        response = web.json_response(session_payload(session))
        set_session_cookie(response, token)
        return response

    async def auth_session(request: web.Request) -> web.Response:
        return web.json_response(session_payload(request["graber_session"]))

    async def logout(request: web.Request) -> web.Response:
        await asyncio.to_thread(auth_manager.sessions.remove, request["graber_session_token"])
        response = web.json_response({"authenticated": False})
        clear_session_cookie(response)
        return response

    async def change_password(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                raise TypeError
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        current_password = body.get("current_password")
        new_password = str(body.get("new_password", ""))
        if current_password is not None:
            current_password = str(current_password)
        if len(new_password) > MAXIMUM_PASSWORD_LENGTH or (
            current_password is not None and len(current_password) > MAXIMUM_PASSWORD_LENGTH
        ):
            return web.json_response({"error": "invalid password"}, status=400)
        try:
            await asyncio.wait_for(password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            token, session = await asyncio.to_thread(
                auth_manager.change_password, request["graber_session"],
                current_password, new_password,
            )
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            password_checks.release()
        response = web.json_response(session_payload(session))
        set_session_cookie(response, token)
        return response

    async def configure_account(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                raise TypeError
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if not 1 <= len(username) <= 64 or len(password) > MAXIMUM_PASSWORD_LENGTH:
            return web.json_response({"error": "invalid account"}, status=400)
        try:
            await asyncio.wait_for(password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            token, session = await asyncio.to_thread(
                auth_manager.configure_owner, request["graber_session"], username, password
            )
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            password_checks.release()
        response = web.json_response(session_payload(session))
        set_session_cookie(response, token)
        return response

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
            request.path == "/api/scan" or request.path.startswith("/api/devices/")
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

    app.router.add_get("/login", login_page)
    app.router.add_get("/assets/auth.js", auth_javascript)
    app.router.add_get("/assets/app.css", stylesheet)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/auth/session", auth_session)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_post("/api/auth/password", change_password)
    app.router.add_post("/api/auth/account", configure_account)
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
