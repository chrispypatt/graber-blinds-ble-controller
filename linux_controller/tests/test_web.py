import asyncio

import pytest

from graber.web import WebControlService, WebRequestError, WebSettings


class FakeController:
    def __init__(self):
        self.commands = []

    async def snapshot(self):
        return {"blinds": {"left": {"bottom_position": 50}}}

    async def submit_command(self, target_id, action, **kwargs):
        self.commands.append((target_id, action, kwargs))
        return {"status": "queued"}


def test_default_interface_is_read_only():
    controller = FakeController()
    service = WebControlService(controller)
    with pytest.raises(WebRequestError, match="disabled") as error:
        asyncio.run(service.dispatch("left", {"action": "open"}))
    assert error.value.status == 403
    assert controller.commands == []


def test_live_command_requires_token_when_configured():
    controller = FakeController()
    service = WebControlService(
        controller, WebSettings(live_controls=True, control_token="private-token")
    )
    with pytest.raises(WebRequestError, match="token"):
        asyncio.run(service.dispatch("left", {"action": "stop"}, supplied_token="wrong"))
    result = asyncio.run(
        service.dispatch("left", {"action": "stop"}, supplied_token="private-token")
    )
    assert result["accepted"] is True
    assert controller.commands[0][1] == "stop"


def test_position_validation_and_command_id_passthrough():
    controller = FakeController()
    service = WebControlService(controller, WebSettings(live_controls=True))
    result = asyncio.run(
        service.dispatch(
            "left",
            {"action": "set_bottom_position", "position": 25, "command_id": "browser-1"},
        )
    )
    assert result["command_id"] == "browser-1"
    assert controller.commands[0][2]["position"] == 25
    with pytest.raises(WebRequestError, match="0 to 100"):
        asyncio.run(
            service.dispatch("left", {"action": "set_bottom_position", "position": 101})
        )


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
