import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import paho.mqtt.client as paho

from graber.config import AdapterConfig, BlindConfig, ControllerConfig, MqttConfig
from graber.control.commands import PositionState
from graber.control.group_controller import Controller
from graber.integrations.mqtt_service import start_mqtt


def test_mqtt_start_reconnect_result_and_shutdown(monkeypatch):
    client = Mock()
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
        messages = {entry.args[0]: entry.args[1] for entry in client.publish.call_args_list}
        assert messages["test/status"] == messages["test/blind/shade/status"] == "online"
        assert messages["test/blind/shade/bottom/state"] == "closed"
        assert messages["test/blind/shade/top/position"] == "80"
        assert json.loads(messages["test/blind/shade/diagnostics"])["battery"] == 80

        client.publish.reset_mock()
        controller.record_lifecycle("shade", "running", "move-1")
        messages = {entry.args[0]: entry.args[1] for entry in client.publish.call_args_list}
        assert json.loads(messages["test/blind/shade/diagnostics"])["active_command_id"] == "move-1"
        assert json.loads(messages["test/adapter/radio/diagnostics"]) == {"status": "online"}

        service.close()
        assert client.method_calls[-3:] == [
            call.publish("test/status", "offline", qos=1, retain=True),
            call.disconnect(),
            call.loop_stop(),
        ]

    asyncio.run(scenario())
