import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from graber.portal import ModeSupervisor, create_app, is_command_request


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
        client = TestClient(TestServer(create_app(supervisor, allowed_networks=("127.0.0.0/8",))))
        await client.start_server()
        try:
            denied = await client.post("/api/portal/setup")
            assert denied.status == 403
            supervisor.active_commands = 1
            busy = await client.post(
                "/api/portal/setup", headers={"X-Graber-Control-Token": "private-token"}
            )
            assert busy.status == 409
            supervisor.active_commands = 0
            accepted = await client.post(
                "/api/portal/setup", headers={"X-Graber-Control-Token": "private-token"}
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
        client = TestClient(TestServer(create_app(supervisor, allowed_networks=("127.0.0.0/8",))))
        await client.start_server()
        try:
            response = await client.post("/api/export", json={})
            assert response.status == 200
            assert (await response.json())["mode"] == "control"
            assert supervisor.switched == ["control"]
        finally:
            await client.close()
            await backend.close()

    asyncio.run(scenario())
