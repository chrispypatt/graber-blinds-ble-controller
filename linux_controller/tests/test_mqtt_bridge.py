import asyncio
import json

from graber.mqtt_bridge import BlindEntity, MqttBridge, MqttSettings


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


class FakeController:
    def __init__(self):
        self.commands = []

    async def submit_command(self, blind_id, action, **kwargs):
        self.commands.append((blind_id, action, kwargs))


def make_bridge():
    client = FakeClient()
    controller = FakeController()
    bridge = MqttBridge(
        client,
        controller,
        {
            "left": BlindEntity("left", "Living Room Left", tdbu=True),
            "right": BlindEntity("right", "Living Room Right"),
        },
        MqttSettings(topic_prefix="test-pilot", discovery_prefix="ha"),
    )
    return bridge, client, controller


def test_discovery_is_retained_separate_and_non_optimistic():
    bridge, client, _ = make_bridge()
    bridge.publish_discovery()
    cover_messages = [item for item in client.published if "/cover/" in item[0]]
    assert len(cover_messages) == 3
    assert all(item[3] is True for item in cover_messages)
    payloads = [json.loads(item[1]) for item in cover_messages]
    assert all(payload["optimistic"] is False for payload in payloads)
    assert all("state_topic" not in payload for payload in payloads)
    assert all("Pi Pilot" in payload["name"] for payload in payloads)
    assert len({payload["unique_id"] for payload in payloads}) == 3
    battery = [json.loads(item[1]) for item in client.published if "_battery/config" in item[0]]
    assert len(battery) == 2
    assert all(item["device_class"] == "battery" for item in battery)


def test_retained_command_is_never_replayed():
    bridge, _, controller = make_bridge()
    accepted = asyncio.run(
        bridge.handle_message("test-pilot/blind/right/bottom/command", "OPEN", retained=True)
    )
    assert accepted is False
    assert controller.commands == []


def test_valid_position_command_is_dispatched_with_generated_id():
    bridge, _, controller = make_bridge()
    accepted = asyncio.run(
        bridge.handle_message("test-pilot/blind/left/top/set_position", b"42")
    )
    assert accepted is True
    blind_id, action, kwargs = controller.commands[0]
    assert (blind_id, action, kwargs["position"], kwargs["source"]) == (
        "left",
        "set_top_position",
        42,
        "mqtt",
    )
    assert kwargs["command_id"]


def test_invalid_or_unknown_commands_are_rejected():
    bridge, _, controller = make_bridge()
    assert not asyncio.run(
        bridge.handle_message("test-pilot/blind/right/top/command", "OPEN")
    )
    assert not asyncio.run(
        bridge.handle_message("test-pilot/blind/right/bottom/set_position", "101")
    )
    assert not asyncio.run(
        bridge.handle_message("test-pilot/blind/right/bottom/command", "FAVORITE")
    )
    assert controller.commands == []


def test_observed_state_is_not_published_until_called():
    bridge, client, _ = make_bridge()
    assert client.published == []
    bridge.publish_observed_state(
        "right", bottom_position=73, movement="open", diagnostics={"retry_count": 1}
    )
    assert [item[1] for item in client.published] == ["73", "open", '{"retry_count":1}']
    assert all(item[3] is False for item in client.published)


def test_mqtt_duplicate_reuses_id_but_later_mid_reuse_does_not():
    bridge, _, _ = make_bridge()
    first = bridge.command_id_for_delivery("topic", 7, False)
    assert bridge.command_id_for_delivery("topic", 7, True) == first
    assert bridge.command_id_for_delivery("topic", 7, False) != first


def test_adapter_health_has_discovery_and_retained_current_state():
    client = FakeClient()
    bridge = MqttBridge(
        client, FakeController(), {}, MqttSettings(topic_prefix="test", discovery_prefix="ha"),
        adapters=("external_a",),
        adapter_health_provider=lambda: {"external_a": {"status": "online", "failures": 0}},
    )
    bridge.publish_discovery()
    assert any("external_a_health/config" in topic for topic, *_ in client.published)
    health = [item for item in client.published if item[0] == "test/adapter/external_a/diagnostics"]
    assert health and health[-1][3] is True
    assert json.loads(health[-1][1])["status"] == "online"
