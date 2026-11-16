import asyncio
import json

from graber.mqtt_bridge import (
    BlindEntity,
    GroupEntity,
    MqttBridge,
    MqttSettings,
    attach_paho_callbacks,
)


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
        groups={
            "living": GroupEntity("living", "Living Room", ("left", "right")),
        },
    )
    return bridge, client, controller


def test_discovery_is_retained_separate_and_non_optimistic():
    bridge, client, _ = make_bridge()
    bridge.publish_discovery()
    cover_messages = [item for item in client.published if "/cover/" in item[0]]
    assert len(cover_messages) == 4
    assert all(item[3] is True for item in cover_messages)
    payloads = [json.loads(item[1]) for item in cover_messages]
    assert all(payload["optimistic"] is False for payload in payloads)
    blind_payloads = [payload for payload in payloads if "group_" not in payload["unique_id"]]
    group_payloads = [payload for payload in payloads if "group_" in payload["unique_id"]]
    assert all("state_topic" in payload for payload in blind_payloads)
    assert all("position_topic" in payload for payload in blind_payloads)
    assert all(len(payload["availability"]) == 2 for payload in blind_payloads)
    assert all("state_topic" not in payload and "position_topic" not in payload
               for payload in group_payloads)
    assert all("Pi Pilot" in payload["name"] for payload in payloads)
    assert len({payload["unique_id"] for payload in payloads}) == 4
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
    assert kwargs["target_type"] == "blind"


def test_tdbu_top_open_and_close_dispatch_explicit_blind_target():
    bridge, _, controller = make_bridge()
    assert asyncio.run(
        bridge.handle_message("test-pilot/blind/left/top/command", "OPEN")
    )
    assert asyncio.run(
        bridge.handle_message("test-pilot/blind/left/top/command", "CLOSE")
    )
    assert [(action, kwargs["target_type"]) for _, action, kwargs in controller.commands] == [
        ("open_top", "blind"),
        ("close_top", "blind"),
    ]


def test_group_commands_are_discovered_subscribed_and_explicitly_typed():
    bridge, client, controller = make_bridge()
    bridge.subscribe_commands()
    assert ("test-pilot/group/living/command", 1) in client.subscribed
    assert asyncio.run(
        bridge.handle_message("test-pilot/group/living/command", "CLOSE")
    )
    target_id, action, kwargs = controller.commands[-1]
    assert (target_id, action, kwargs["target_type"]) == ("living", "close", "group")
    assert not asyncio.run(
        bridge.handle_message("test-pilot/group/living/set_position", "50")
    )


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


def test_blind_availability_is_retained_and_scoped():
    bridge, client, _ = make_bridge()
    bridge.publish_availability(True)
    bridge.publish_blind_availability("left", True)
    assert client.published[-2] == ("test-pilot/status", "online", 1, True)
    assert client.published[-1] == ("test-pilot/blind/left/status", "online", 1, True)


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


def test_connect_callback_can_republish_current_state():
    bridge, _, _ = make_bridge()
    client = type("PahoClient", (), {})()
    connected = []
    loop = asyncio.new_event_loop()
    try:
        attach_paho_callbacks(
            bridge, client, loop, connected_handler=lambda: connected.append(True)
        )
        client.on_connect(client, None, None, 0)
    finally:
        loop.close()
    assert connected == [True]
