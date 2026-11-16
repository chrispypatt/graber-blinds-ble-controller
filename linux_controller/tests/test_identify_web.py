import asyncio
import stat
from pathlib import Path

from graber.config import load_config
from graber.diagnostics import AdapterIdentity
from graber.identify_cli import MotorObservation
from graber.identify_web import DeviceSetup, SetupSession, load_known_credentials, opaque_id
from graber.identify_web import SetupWebSettings, create_app


def make_session(tmp_path: Path) -> tuple[SetupSession, str]:
    adapter = AdapterIdentity(
        "hci4", "AA:BB:CC:DD:EE:FF", True, "1234", "5678",
        usb_product="FSC-BP119", usb_serial="adapter-a", usb_path="1-2.3",
    )
    session = SetupSession(
        [adapter], export_dir=tmp_path, group_names=("Living Room", "Dining")
    )
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
    assert browser["bottom_position"] == 40
    assert browser["top_position"] == 30
    assert "firmware" not in browser
    assert "battery" not in browser
    assert "motor_type_candidate" not in browser
    assert session.state(allow_jog=False)["groups"][:2] == [
        {"id": "living_room", "name": "Living Room"},
        {"id": "dining", "name": "Dining"},
    ]


def test_name_generates_id_and_groups_allow_multiple_custom_values(tmp_path: Path):
    session, key = make_session(tmp_path)
    session.configure(key, {
        "name": "Living East", "id": "client_override",
        "groups": ["Dining", "Whole House", "Window Wall"],
    })
    device = session.devices[key]
    assert device.id == "living_east"
    assert device.groups == ("dining", "whole_house", "window_wall")
    assert session.groups["window_wall"] == "Window Wall"
    assert device.configured is True


def test_generated_id_collision_is_rejected_on_save(tmp_path: Path):
    session, key = make_session(tmp_path)
    second = MotorObservation("RRBZ_OTHER", "11:22:33:44:55:77", [-60], 20, 80)
    second_key = opaque_id(second.address)
    session.devices[second_key] = DeviceSetup(
        second, "other", "Other", False, "usb_1_2_3", groups=()
    )
    try:
        session.configure(second_key, {"name": "Living Left"})
    except ValueError as exc:
        assert "living_left" in str(exc)
    else:
        raise AssertionError("duplicate generated id should be rejected")


def test_known_credentials_prefill_by_address_but_never_reach_browser(tmp_path: Path):
    secrets_path = tmp_path / "known-secrets.toml"
    secrets_path.write_text(
        '[blinds.previous_name]\naddress = "11:22:33:44:55:66"\naccess_code = "0123456"\n'
    )
    secrets_path.chmod(0o600)
    credentials = load_known_credentials(secrets_path)
    session, _ = make_session(tmp_path)
    session.known_credentials = credentials

    async def fake_scan(_adapter, _seconds):
        return [MotorObservation("RRBZ_TEST", "11:22:33:44:55:66", [-55], 10, 70)]

    import graber.identify_web as identify_web
    original = identify_web.scan
    identify_web.scan = fake_scan
    try:
        asyncio.run(session.scan_all())
    finally:
        identify_web.scan = original
    device = session.state(allow_jog=False)["devices"][0]
    assert device["has_access_code"] is True
    assert "access_code" not in device
    assert "0123456" not in repr(session.devices[next(iter(session.devices))])
    config_path, exported_secrets = session.export()
    assert stat.S_IMODE(exported_secrets.stat().st_mode) == 0o600
    assert load_config(config_path, exported_secrets).blinds[0].access_code == "0123456"


def test_scan_suggests_tdbu_only_for_nonzero_top_position(tmp_path: Path):
    adapter = AdapterIdentity(
        "hci4", "AA:BB:CC:DD:EE:FF", True, "1234", "5678",
        usb_product="FSC-BP119", usb_serial="adapter-a", usb_path="1-2.3",
    )
    session = SetupSession([adapter], export_dir=tmp_path)

    async def fake_scan(_adapter, _seconds):
        return [
            MotorObservation("RRBZ_TDBU", "11:22:33:44:55:66", [-55], 10, 70),
            MotorObservation("RRBZ_AMBIGUOUS", "11:22:33:44:55:77", [-55], 10, 0),
        ]

    import graber.identify_web as identify_web
    original = identify_web.scan
    identify_web.scan = fake_scan
    try:
        asyncio.run(session.scan_all())
    finally:
        identify_web.scan = original

    devices = {item.observation.name: item for item in session.devices.values()}
    assert devices["RRBZ_TDBU"].dual_rail is True
    assert devices["RRBZ_AMBIGUOUS"].dual_rail is False


def test_scan_can_be_cancelled_and_cleaned_up(tmp_path: Path):
    session, _ = make_session(tmp_path)
    started = asyncio.Event()

    async def fake_scan_all(_seconds):
        started.set()
        await asyncio.Event().wait()

    session.scan_all = fake_scan_all

    async def scenario():
        operation = asyncio.create_task(session.start_scan(30))
        await started.wait()
        assert await session.cancel_scan() is True
        try:
            await operation
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled scan should propagate cancellation")
        assert session.scan_task is None
        assert await session.cancel_scan() is False

    asyncio.run(scenario())


def test_scan_survives_request_task_cancellation(tmp_path: Path):
    session, _ = make_session(tmp_path)
    started = asyncio.Event()

    async def fake_scan_all(_seconds):
        started.set()
        await asyncio.Event().wait()

    session.scan_all = fake_scan_all

    async def scenario():
        request_task = asyncio.create_task(session.start_scan(30))
        await started.wait()
        request_task.cancel()
        try:
            await request_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("request task should be cancelled")
        assert session.scan_state()["active"] is True
        assert await session.cancel_scan() is True
        assert session.scan_state()["active"] is False

    asyncio.run(scenario())


def test_known_credentials_reject_insecure_permissions(tmp_path: Path):
    secrets_path = tmp_path / "known-secrets.toml"
    secrets_path.write_text(
        '[blinds.test]\naddress = "11:22:33:44:55:66"\naccess_code = "0123456"\n'
    )
    secrets_path.chmod(0o644)
    try:
        load_known_credentials(secrets_path)
    except ValueError as exc:
        assert "0600" in str(exc)
    else:
        raise AssertionError("insecure known-secrets permissions should be rejected")


def test_known_credentials_reject_symlink_unicode_code_and_duplicate_address(tmp_path: Path):
    target = tmp_path / "target.toml"
    target.write_text('[blinds.test]\naddress="11:22:33:44:55:66"\naccess_code="0123456"\n')
    target.chmod(0o600)
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    for path in (link,):
        try:
            load_known_credentials(path)
        except ValueError:
            pass
        else:
            raise AssertionError("symlink should be rejected")

    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[blinds.test]\naddress="11:22:33:44:55:66"\naccess_code="012345٦"\n')
    invalid.chmod(0o600)
    try:
        load_known_credentials(invalid)
    except ValueError as exc:
        assert "invalid access code" in str(exc)
    else:
        raise AssertionError("non-ASCII code should be rejected")

    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(
        '[blinds.one]\naddress="11:22:33:44:55:66"\naccess_code="0123456"\n'
        '[blinds.two]\naddress="11:22:33:44:55:66"\naccess_code="7654321"\n'
    )
    duplicate.chmod(0o600)
    try:
        load_known_credentials(duplicate)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate address should be rejected")


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
    session.configure(key, {
        "access_code": "0123456", "groups": ["Living Room", "Whole House"],
    })
    config_path, secrets_path = session.export()
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    loaded = load_config(config_path, secrets_path)
    assert loaded.blinds[0].id == "living_left"
    assert loaded.blinds[0].access_code == "0123456"
    assert loaded.groups[0].id == "living_room"
    assert loaded.groups[0].name == "Living Room"
    assert loaded.groups[1].id == "whole_house"
    assert loaded.groups[1].members == ("living_left",)


def test_export_omits_group_for_no_group_selection(tmp_path: Path):
    session, key = make_session(tmp_path)
    session.configure(key, {"access_code": "0123456", "name": "Solo", "groups": []})
    config_path, secrets_path = session.export()
    loaded = load_config(config_path, secrets_path)
    assert loaded.blinds[0].id == "solo"
    assert loaded.groups == ()


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
            asset = await client.get("/assets/setup.js")
            assert asset.status == 200
            assert "no-store" in asset.headers["Cache-Control"]
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
            state = await client.get(
                "/api/state", headers={"X-Graber-Control-Token": "0123456789abcdef"}
            )
            assert (await state.json())["busy"] is False
            response = await client.post(
                f"/api/devices/{key}/jog",
                headers={"X-Graber-Control-Token": "0123456789abcdef"},
            )
            assert response.status == 200
            assert calls == [key]
        finally:
            await client.close()
    asyncio.run(scenario())
