import asyncio

import pytest

from graber.webapp.web import WebControlService, WebRequestError, WebSettings, create_app


class FakeController:
    def __init__(self):
        self.commands = []
        self.refreshes = []

    async def snapshot(self):
        return {"blinds": {"left": {"bottom_position": 50}}}

    async def submit_command(self, target_id, action, **kwargs):
        self.commands.append((target_id, action, kwargs))
        return {"status": "queued"}

    async def refresh_status(self, target_id):
        if target_id == "living":
            raise ValueError("status refresh is available only for individual blinds")
        self.refreshes.append(target_id)
        return {"bottom": 42, "battery": 88}


def test_default_interface_is_read_only():
    controller = FakeController()
    service = WebControlService(controller)
    with pytest.raises(WebRequestError, match="disabled") as error:
        asyncio.run(service.dispatch("left", {"action": "open"}, target_type="blind"))
    assert error.value.status == 403
    assert controller.commands == []


def test_live_command_requires_token_when_configured():
    controller = FakeController()
    service = WebControlService(
        controller, WebSettings(live_controls=True, control_token="private-token")
    )
    with pytest.raises(WebRequestError, match="token"):
        asyncio.run(service.dispatch(
            "left", {"action": "stop"}, target_type="blind", supplied_token="wrong"
        ))
    result = asyncio.run(
        service.dispatch(
            "left", {"action": "stop"}, target_type="blind",
            supplied_token="private-token",
        )
    )
    assert result["accepted"] is True
    assert controller.commands[0][1] == "stop"
    assert controller.commands[0][2]["target_type"] == "blind"


def test_position_validation_and_command_id_passthrough():
    controller = FakeController()
    service = WebControlService(controller, WebSettings(live_controls=True))
    result = asyncio.run(
        service.dispatch(
            "left",
            {"action": "set_bottom_position", "position": 25, "command_id": "browser-1"},
            target_type="blind",
        )
    )
    assert result["command_id"] == "browser-1"
    assert controller.commands[0][2]["position"] == 25
    with pytest.raises(WebRequestError, match="0 to 100"):
        asyncio.run(
            service.dispatch(
                "left", {"action": "set_bottom_position", "position": 101},
                target_type="blind",
            )
        )


def test_status_refresh_requires_token_and_rejects_groups():
    controller = FakeController()
    service = WebControlService(
        controller, WebSettings(live_controls=False, control_token="private-token")
    )
    with pytest.raises(WebRequestError, match="token"):
        asyncio.run(service.dispatch(
            "left", {"action": "refresh_status"}, target_type="blind"
        ))
    result = asyncio.run(service.dispatch(
        "left", {"action": "refresh_status"}, target_type="blind",
        supplied_token="private-token",
    ))
    assert result["result"] == {"bottom": 42, "battery": 88}
    assert controller.refreshes == ["left"]
    with pytest.raises(WebRequestError, match="individual") as error:
        asyncio.run(service.dispatch(
            "living", {"action": "refresh_status"}, target_type="group",
            supplied_token="private-token",
        ))
    assert error.value.status == 400


def test_allowed_networks_reject_public_addresses():
    service = WebControlService(FakeController())
    assert service.client_allowed("192.168.1.25")
    assert service.client_allowed("127.0.0.1")
    assert not service.client_allowed("8.8.8.8")
    assert not service.client_allowed(None)


def test_snapshot_is_read_only_and_available_without_live_gate():
    service = WebControlService(FakeController())
    state = asyncio.run(service.snapshot())
    assert state["blinds"]["left"]["bottom_position"] == 50
    assert state["_web"] == {"live_controls": False, "token_required": False}


def test_control_assets_are_served_without_caching():
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        client = TestClient(TestServer(create_app(FakeController())))
        await client.start_server()
        try:
            stylesheet = await client.get("/assets/app.css")
            javascript = await client.get("/assets/control.js")
            assert stylesheet.status == javascript.status == 200
            assert "no-store" in stylesheet.headers["Cache-Control"]
            assert "no-store" in javascript.headers["Cache-Control"]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_command_routes_are_explicit_and_legacy_target_route_is_removed():
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        controller = FakeController()
        app = create_app(controller, WebSettings(
            live_controls=True, control_token="private-token"
        ))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            headers = {"X-Graber-Control-Token": "private-token"}
            blind = await client.post(
                "/api/blinds/shared/commands", json={"action": "stop"}, headers=headers
            )
            group = await client.post(
                "/api/groups/shared/commands", json={"action": "stop"}, headers=headers
            )
            legacy = await client.post(
                "/api/targets/shared/commands", json={"action": "stop"}, headers=headers
            )
            assert blind.status == group.status == 202
            assert legacy.status == 404
            assert [item[2]["target_type"] for item in controller.commands] == ["blind", "group"]
        finally:
            await client.close()

    asyncio.run(scenario())
