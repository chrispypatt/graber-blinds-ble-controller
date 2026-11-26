import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import paho.mqtt.client as paho

from graber.config import AdapterConfig, BlindConfig, ControllerConfig, MqttConfig
from graber.blind import PositionState
from graber.group_controller import Controller
from graber.mqtt_service import MqttService, start_mqtt


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)



def test_mqtt_start_reconnect_result_and_shutdown(monkeypatch):
    client = Mock()
    client.publish.return_value = SimpleNamespace(rc=0, is_published=lambda: True)
    factory = Mock(return_value=client)
    monkeypatch.setattr(paho, "Client", factory)
    config = ControllerConfig(
        adapters=(AdapterConfig("radio", "00:00:00:00:00:01"),),
        blinds=(BlindConfig("shade", "Shade", "00:00:00:00:00:02", True, "0000000"),),
        groups=(),
        mqtt=MqttConfig(
            enabled=True, host="broker.example", port=8883, topic_prefix="test",
            tls=True, ca_certs="broker-ca.pem", username="test", password="test",
        ),
    )
    state = PositionState(bottom=0, top=20, battery=80, updated_at=1)
    dispatcher = SimpleNamespace(blind=SimpleNamespace(state=state))
    controller = Controller({"shade": dispatcher}, ())

    async def scenario():
        service = start_mqtt(config, controller, lambda: {"radio": {"status": "online"}}, Mock())
        factory.assert_called_once_with(paho.CallbackAPIVersion.VERSION2, client_id=config.mqtt.client_id)
        client.username_pw_set.assert_called_once_with("test", "test")
        client.tls_set.assert_called_once_with(ca_certs="broker-ca.pem", certfile=None, keyfile=None)
        client.tls_insecure_set.assert_called_once_with(False)
        client.will_set.assert_called_once_with("test/status", "offline", qos=1, retain=True)
        client.connect_async.assert_called_once_with("broker.example", 8883)
        client.loop_start.assert_called_once()

        # A new broker connection republishes the current state, including top-rail inversion.
        client.on_connect(client, None, {}, 0)

        def published():
            return {entry.args[0]: entry.args[1] for entry in client.publish.call_args_list}

        await wait_until(lambda: "test/blind/shade/status" in published())
        messages = published()
        assert messages["test/status"] == messages["test/blind/shade/status"] == "online"
        assert messages["test/blind/shade/bottom/state"] == "closed"
        assert messages["test/blind/shade/top/position"] == "80"
        assert json.loads(messages["test/blind/shade/diagnostics"])["battery"] == 80
        config_topic = "homeassistant/sensor/graber_rpi_controller_shade_battery/config"
        await wait_until(lambda: config_topic in published())
        discovery = json.loads(published()[config_topic])
        assert discovery["json_attributes_topic"] == discovery["state_topic"] == "test/blind/shade/battery"

        client.publish.reset_mock()
        controller.record_lifecycle("shade", "running", "move-1")
        await wait_until(lambda: "test/blind/shade/diagnostics" in published())
        messages = published()
        assert json.loads(messages["test/blind/shade/diagnostics"])["active_command_id"] == "move-1"
        assert "test/adapter/radio/diagnostics" not in messages

        client.publish.reset_mock()
        for index in range(1000):
            state.updated_at = 2.0 + index
            service.publish_state("shade", state)
        await asyncio.sleep(0.2)
        assert not client.publish.called

        battery_topic = "test/blind/shade/battery"
        state.observe_battery(0, state.updated_at)
        service.publish_state("shade", state)
        await wait_until(lambda: battery_topic in published())
        battery = json.loads(published()[battery_topic])
        assert battery["battery"] == 80 and battery["battery_raw"] == 0

        client.publish.reset_mock()
        message = SimpleNamespace(topic="homeassistant/status", payload=b"online")
        client.on_message(client, None, message)
        await wait_until(lambda: "test/blind/shade/top/position" in published())
        assert published()["test/blind/shade/top/position"] == "80"
        # A fresh install with no credible baseline must clear a prior HA value.
        new_state = PositionState()
        new_state.observe_battery(0, 2000)
        service.publish_state("shade", new_state)
        await wait_until(lambda: json.loads(published().get(battery_topic, "{}")).get("battery", -1) is None)

        service.close()
        assert client.method_calls[-3:] == [
            call.publish("test/status", "offline", qos=1, retain=True),
            call.disconnect(),
            call.loop_stop(),
        ]

    asyncio.run(scenario())


def test_coalesced_contact_flush_and_recovery_snapshot():
    state = PositionState(bottom=0, updated_at=20)
    state.observe_battery(91, 20)
    controller = Controller({"shade": SimpleNamespace(blind=SimpleNamespace(state=state))}, ())
    bridge, outbox = Mock(), Mock()
    bridge.settings.topic_prefix = "test"
    bridge.availability_topic = "test/status"
    bridge.adapter_health_provider.return_value = {}
    service = MqttService(Mock(), bridge, controller, outbox)

    outbox.loop.time.return_value = 0
    service.connected()
    state.updated_at = 30
    outbox.loop.time.return_value = 30
    service.publish_state("shade", state)
    assert bridge.publish_observed_state.call_args.kwargs["diagnostics"] is None
    # No further advertisement needed to flush the latest pending contact.
    outbox.loop.time.return_value = 60
    service._heartbeat()
    diagnostics = bridge.publish_observed_state.call_args.kwargs["diagnostics"]
    assert diagnostics["last_successful_communication"] == "1970-01-01T00:00:30+00:00"
    assert diagnostics["battery"] == 91
    outbox.replay.assert_called_with("test/status")

    outbox.loop.time.return_value = 300
    service._heartbeat()
    outbox.replay.assert_called_with("test/")
    service.close()
