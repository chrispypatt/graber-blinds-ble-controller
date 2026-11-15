import asyncio
import stat
from pathlib import Path

from graber.config import load_config
from graber.diagnostics import AdapterIdentity
from graber.identify_cli import MotorObservation
from graber.identify_web import DeviceSetup, SetupSession, opaque_id
from graber.identify_web import SetupWebSettings, create_app


def make_session(tmp_path: Path) -> tuple[SetupSession, str]:
    adapter = AdapterIdentity(
        "hci4", "AA:BB:CC:DD:EE:FF", True, "1234", "5678",
        usb_product="FSC-BP119", usb_serial="adapter-a", usb_path="1-2.3",
    )
    session = SetupSession([adapter], export_dir=tmp_path)
    observation = MotorObservation("RRBZ_TEST", "11:22:33:44:55:66", [-60], 40, 70)
    key = opaque_id(observation.address)
    session.devices[key] = DeviceSetup(
        observation, "living_left", "Living Left", True, "usb_1_2_3"
    )
    return session, key


def test_setup_configuration_never_returns_access_code_to_browser(tmp_path: Path):
    session, key = make_session(tmp_path)
    session.configure(key, {"access_code": "0123456", "name": "Living Left"})
    browser = session.state(allow_jog=False)["devices"][0]
    assert browser["has_access_code"] is True
    assert "access_code" not in browser


def test_candidate_code_requires_explicit_selection(tmp_path: Path):
    session, key = make_session(tmp_path)
    session.allow_code_read = True
    session.devices[key].observation.candidate_access_code = "0123456"
    session.configure(key, {})
    assert session.devices[key].access_code is None
    session.configure(key, {"use_candidate": True})
    assert session.devices[key].access_code == "0123456"


def test_export_writes_valid_split_config_and_mode_0600_secrets(tmp_path: Path):
    session, key = make_session(tmp_path)
    session.configure(key, {"access_code": "0123456", "group": "living room"})
    config_path, secrets_path = session.export()
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    loaded = load_config(config_path, secrets_path)
    assert loaded.blinds[0].id == "living_left"
    assert loaded.blinds[0].access_code == "0123456"
    assert loaded.groups[0].id == "living_room"


def test_export_refuses_missing_credentials(tmp_path: Path):
    session, _ = make_session(tmp_path)
    try:
        session.export()
    except ValueError as exc:
        assert "access code" in str(exc)
    else:
        raise AssertionError("missing credentials should be rejected")


def test_web_requires_token_and_never_jogs_without_launch_gate(tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        session, key = make_session(tmp_path)
        calls = []
        async def fake_jog(_session, device_key):
            calls.append(device_key)
            return {"status": "completed"}
        app = create_app(
            session,
            SetupWebSettings(control_token="0123456789abcdef", allow_jog=False),
            fake_jog,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            assert (await client.get("/api/state")).status == 403
            assert (await client.get(
                "/api/state", headers={"X-Graber-Control-Token": "0123456789abcdef"}
            )).status == 200
            assert (await client.post(f"/api/devices/{key}/configure", json={})).status == 403
            response = await client.post(
                f"/api/devices/{key}/jog",
                headers={"X-Graber-Control-Token": "0123456789abcdef"},
            )
            assert response.status == 403
            assert calls == []
        finally:
            await client.close()
    asyncio.run(scenario())


def test_web_jog_calls_injected_runner_only_when_enabled(tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        session, key = make_session(tmp_path)
        calls = []
        async def fake_jog(_session, device_key):
            calls.append(device_key)
            return {"status": "completed", "restored": True}
        client = TestClient(TestServer(create_app(
            session,
            SetupWebSettings(control_token="0123456789abcdef", allow_jog=True),
            fake_jog,
        )))
        await client.start_server()
        try:
            response = await client.post(
                f"/api/devices/{key}/jog",
                headers={"X-Graber-Control-Token": "0123456789abcdef"},
            )
            assert response.status == 200
            assert calls == [key]
        finally:
            await client.close()
    asyncio.run(scenario())
