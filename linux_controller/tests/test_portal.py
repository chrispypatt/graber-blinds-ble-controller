import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from graber.auth import AuthManager, PasswordStore
from graber.portal import ModeSupervisor, create_app, is_command_request


PASSWORD = "correct horse battery staple"


def make_auth(tmp_path: Path, *, must_change=False) -> AuthManager:
    store = PasswordStore(tmp_path / "auth.json")
    store.set_password("admin", PASSWORD, must_change_password=must_change)
    return AuthManager(store, secure_cookie=False)


async def login(client: TestClient, password: str = PASSWORD) -> dict:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": password}
    )
    assert response.status == 200
    return await response.json()


class RecordingSupervisor(ModeSupervisor):
    def __init__(self, tmp_path: Path):
        token_file = tmp_path / "token"
        token_file.write_text("private-token")
        super().__init__(
            setup_runner=tmp_path / "setup", setup_settings=tmp_path / "setup.env",
            config_path=tmp_path / "config.toml", secrets_path=tmp_path / "secrets.toml",
            token_file=token_file, mode_file=tmp_path / "mode",
            internal_host="127.0.0.1", internal_port=18080,
        )
        self.events = []

    async def _stop_child(self):
        self.events.append("stop")

    async def _start_child(self, mode):
        self.events.append(f"start:{mode}")
        self.process = object()


def test_mode_switch_stops_before_start_and_persists(tmp_path):
    supervisor = RecordingSupervisor(tmp_path)
    asyncio.run(supervisor.switch("control"))
    assert supervisor.events == ["stop", "start:control"]
    assert supervisor.mode_file.read_text() == "control\n"
    assert supervisor.mode_file.stat().st_mode & 0o777 == 0o600


def test_invalid_saved_mode_falls_back_to_setup(tmp_path):
    supervisor = RecordingSupervisor(tmp_path)
    supervisor.mode_file.write_text("unexpected\n")
    assert supervisor.saved_mode() == "setup"


def test_portal_tracks_explicit_blind_and_group_command_routes():
    assert is_command_request("POST", "/api/blinds/shared/commands")
    assert is_command_request("POST", "/api/groups/shared/commands")
    assert not is_command_request("POST", "/api/targets/shared/commands")
    assert not is_command_request("GET", "/api/blinds/shared/commands")


def test_edit_setup_requires_token_and_waits_for_commands(tmp_path):
    class FakeSupervisor:
        mode = "control"
        active_commands = 0
        internal_host = "127.0.0.1"
        internal_port = 1

        def __init__(self):
            self.switched = []

        def control_token(self):
            return "private-token"

        async def start(self):
            pass

        async def close(self):
            pass

        async def switch(self, mode):
            self.mode = mode
            self.switched.append(mode)

        async def ensure_running(self):
            pass

    async def scenario():
        supervisor = FakeSupervisor()
        client = TestClient(TestServer(create_app(
            supervisor, allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            auth = await login(client)
            denied = await client.post("/api/portal/setup")
            assert denied.status == 403
            supervisor.active_commands = 1
            busy = await client.post(
                "/api/portal/setup", headers={"X-CSRF-Token": auth["csrf_token"]}
            )
            assert busy.status == 409
            supervisor.active_commands = 0
            accepted = await client.post(
                "/api/portal/setup", headers={"X-CSRF-Token": auth["csrf_token"]}
            )
            assert accepted.status == 200
            assert supervisor.switched == ["setup"]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_successful_export_switches_to_control(tmp_path):
    class FakeSupervisor:
        mode = "setup"
        active_commands = 0

        def __init__(self):
            self.switched = []

        def control_token(self):
            return "private-token"

        async def start(self):
            pass

        async def close(self):
            pass

        async def switch(self, mode):
            self.mode = mode
            self.switched.append(mode)

        async def ensure_running(self):
            pass

    async def scenario():
        backend_app = web.Application()
        async def export(_request):
            return web.json_response({"saved": True})
        backend_app.router.add_post("/api/export", export)
        backend = TestServer(backend_app)
        await backend.start_server()
        supervisor = FakeSupervisor()
        supervisor.internal_host = backend.host
        supervisor.internal_port = backend.port
        client = TestClient(TestServer(create_app(
            supervisor, allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            auth = await login(client)
            response = await client.post(
                "/api/export", json={}, headers={"X-CSRF-Token": auth["csrf_token"]}
            )
            assert response.status == 200
            assert (await response.json())["mode"] == "control"
            assert supervisor.switched == ["control"]
        finally:
            await client.close()
            await backend.close()

    asyncio.run(scenario())


class StaticSupervisor:
    mode = "control"
    active_commands = 0

    def __init__(self, host="127.0.0.1", port=1):
        self.internal_host = host
        self.internal_port = port
        self.switched = []

    def control_token(self):
        return "private-internal-token"

    async def start(self):
        pass

    async def close(self):
        pass

    async def ensure_running(self):
        pass

    async def switch(self, mode):
        self.mode = mode
        self.switched.append(mode)


def test_portal_redirects_pages_and_rejects_api_without_session(tmp_path):
    async def scenario():
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            page = await client.get("/", allow_redirects=False)
            assert page.status == 303
            assert page.headers["Location"] == "/login"
            state = await client.get("/api/state")
            assert state.status == 401
            assert (await state.json())["error"] == "authentication required"
            login_page = await client.get("/login")
            assert login_page.status == 200
            assert login_page.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in login_page.headers["Content-Security-Policy"]
        finally:
            await client.close()
    asyncio.run(scenario())


def test_login_cookie_is_httponly_strict_and_secure_when_configured(tmp_path):
    async def scenario():
        manager = AuthManager(make_auth(tmp_path).password_store, secure_cookie=True)
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",), auth_manager=manager,
        )))
        await client.start_server()
        try:
            response = await client.post(
                "/api/auth/login", json={"username": "admin", "password": PASSWORD}
            )
            cookie = response.cookies[manager.cookie_name]
            assert cookie["httponly"] is True
            assert cookie["secure"] is True
            assert cookie["samesite"] == "Strict"
            assert cookie["path"] == "/"
        finally:
            await client.close()
    asyncio.run(scenario())


def test_login_is_rate_limited_per_effective_client(tmp_path):
    async def scenario():
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            for _ in range(5):
                denied = await client.post(
                    "/api/auth/login", json={"username": "admin", "password": "wrong"}
                )
                assert denied.status == 401
            limited = await client.post(
                "/api/auth/login", json={"username": "admin", "password": PASSWORD}
            )
            assert limited.status == 429
            assert int(limited.headers["Retry-After"]) > 0
        finally:
            await client.close()
    asyncio.run(scenario())


def test_auth_endpoints_reject_non_object_json(tmp_path):
    async def scenario():
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            login_response = await client.post("/api/auth/login", json=[])
            assert login_response.status == 400
            long_password = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "x" * 257},
            )
            assert long_password.status == 401
            oversized = await client.post(
                "/api/auth/login", data="x" * (17 * 1024),
                headers={"Content-Type": "application/json"},
            )
            assert oversized.status == 413
            auth = await login(client)
            password_response = await client.post(
                "/api/auth/password", json=[],
                headers={"X-CSRF-Token": auth["csrf_token"]},
            )
            assert password_response.status == 400
        finally:
            await client.close()
    asyncio.run(scenario())


def test_csrf_is_required_and_logout_invalidates_session(tmp_path):
    async def scenario():
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            auth = await login(client)
            assert (await client.get("/api/auth/session")).status == 200
            rejected = await client.post("/api/auth/logout")
            assert rejected.status == 403
            accepted = await client.post(
                "/api/auth/logout", headers={"X-CSRF-Token": auth["csrf_token"]}
            )
            assert accepted.status == 200
            assert (await client.get("/api/auth/session")).status == 401
        finally:
            await client.close()
    asyncio.run(scenario())


def test_must_change_password_blocks_proxy_until_changed(tmp_path):
    async def scenario():
        manager = make_auth(tmp_path, must_change=True)
        client = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=manager,
        )))
        await client.start_server()
        try:
            auth = await login(client)
            assert auth["must_change_password"] is True
            blocked = await client.get("/api/state")
            assert blocked.status == 403
            changed = await client.post(
                "/api/auth/password",
                json={"new_password": "a different secure password"},
                headers={"X-CSRF-Token": auth["csrf_token"]},
            )
            assert changed.status == 200
            assert (await changed.json())["must_change_password"] is False
            session = await client.get("/api/auth/session")
            assert session.status == 200
            assert (await session.json())["must_change_password"] is False
        finally:
            await client.close()
    asyncio.run(scenario())


def test_proxy_strips_browser_token_and_injects_internal_token(tmp_path):
    async def scenario():
        observed = []
        backend_app = web.Application()
        async def state(request):
            observed.append(request.headers.get("X-Graber-Control-Token"))
            assert "Cookie" not in request.headers
            assert "X-CSRF-Token" not in request.headers
            response = web.json_response({"ok": True})
            response.set_cookie("graber_session", "child-must-not-overwrite")
            return response
        backend_app.router.add_get("/api/state", state)
        backend = TestServer(backend_app)
        await backend.start_server()
        client = TestClient(TestServer(create_app(
            StaticSupervisor(backend.host, backend.port),
            allowed_networks=("127.0.0.0/8",), auth_manager=make_auth(tmp_path),
        )))
        await client.start_server()
        try:
            await login(client)
            response = await client.get(
                "/api/state", headers={"X-Graber-Control-Token": "browser-supplied"}
            )
            assert response.status == 200
            assert observed == ["private-internal-token"]
            assert "Set-Cookie" not in response.headers
        finally:
            await client.close()
            await backend.close()
    asyncio.run(scenario())


def test_forwarded_client_is_trusted_only_from_configured_proxy(tmp_path):
    async def scenario():
        direct = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            auth_manager=make_auth(tmp_path),
        )))
        proxied = TestClient(TestServer(create_app(
            StaticSupervisor(), allowed_networks=("127.0.0.0/8",),
            trusted_proxies=("127.0.0.0/8",), auth_manager=make_auth(tmp_path / "other"),
        )))
        await direct.start_server()
        await proxied.start_server()
        try:
            spoofed = await direct.get("/login", headers={"X-Forwarded-For": "8.8.8.8"})
            assert spoofed.status == 200
            rejected = await proxied.get("/login", headers={"X-Forwarded-For": "8.8.8.8"})
            assert rejected.status == 403
        finally:
            await direct.close()
            await proxied.close()
    asyncio.run(scenario())
