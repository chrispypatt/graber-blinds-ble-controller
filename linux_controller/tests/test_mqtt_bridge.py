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
        MqttSettings(
            topic_prefix="test-pilot", discovery_prefix="ha",
            entity_name_suffix=" (Test)",
        ),
        groups={
            "living": GroupEntity("living", "Living Room", ("left", "right")),
        },
    )
    return bridge, client, controller


def test_discovery_is_retained_separate_and_non_optimistic():
    bridge, client, _ = make_bridge()
    bridge.publish_discovery()
    cover_messages = [
        item for item in client.published if "/cover/" in item[0] and item[1]
    ]
    assert len(cover_messages) == 5
    assert all(item[3] is True for item in cover_messages)
    payloads = [json.loads(item[1]) for item in cover_messages]
    assert all(payload["optimistic"] is False for payload in payloads)
    assert all(payload["device_class"] == "shade" for payload in payloads)
    blind_payloads = [payload for payload in payloads if "group_" not in payload["unique_id"]]
    group_payloads = [payload for payload in payloads if "group_" in payload["unique_id"]]
    rail_payloads = [
        payload for payload in blind_payloads if not payload["unique_id"].endswith("_full")
    ]
    full_payloads = [
        payload for payload in blind_payloads if payload["unique_id"].endswith("_full")
    ]
    assert all("state_topic" in payload for payload in blind_payloads)
    assert all("position_topic" in payload for payload in rail_payloads)
    assert len(full_payloads) == 1
    assert "position_topic" not in full_payloads[0]
    assert "set_position_topic" not in full_payloads[0]
    assert all(len(payload["availability"]) == 2 for payload in blind_payloads)
    assert all("state_topic" not in payload and "position_topic" not in payload
               for payload in group_payloads)
    assert all(payload["name"].endswith(" (Test)") for payload in payloads)
    assert len({payload["unique_id"] for payload in payloads}) == 5
    battery = [json.loads(item[1]) for item in client.published if "_battery/config" in item[0]]
    assert len(battery) == 2
    assert all(item["device_class"] == "battery" for item in battery)


def test_action_button_discovery_is_verified_and_non_optimistic():
    bridge, client, _ = make_bridge()
    bridge.publish_discovery()
    buttons = {
        json.loads(payload)["unique_id"]: json.loads(payload)
        for topic, payload, _, retained in client.published
        if "/button/" in topic and retained and payload
    }
    assert set(buttons) == {
        "graber_rpi_controller_blind_left_favorite",
        "graber_rpi_controller_blind_left_stack_up",
        "graber_rpi_controller_blind_left_stack_down",
        "graber_rpi_controller_blind_right_favorite",
        "graber_rpi_controller_group_living_favorite",
    }
    assert all(payload["payload_press"] == "PRESS" for payload in buttons.values())
    assert all(payload["qos"] == 1 for payload in buttons.values())
    assert all("state_topic" not in payload for payload in buttons.values())


def test_all_tdbu_group_gets_stack_buttons_but_unknown_members_do_not():
    client = FakeClient()
    bridge = MqttBridge(
        client,
        FakeController(),
        {
            "left": BlindEntity("left", "Left", tdbu=True),
            "right": BlindEntity("right", "Right", tdbu=True),
        },
        MqttSettings(topic_prefix="test", discovery_prefix="ha"),
        groups={
            "tdbu": GroupEntity("tdbu", "TDBU", ("left", "right")),
            "unknown": GroupEntity("unknown", "Unknown", ("left", "missing")),
        },
    )
    bridge.publish_discovery()
    topics = [
        topic for topic, payload, *_ in client.published
        if "/button/" in topic and payload
    ]
    assert any("group_tdbu_stack_up" in topic for topic in topics)
    assert any("group_tdbu_stack_down" in topic for topic in topics)
    assert not any("group_unknown_stack_" in topic for topic in topics)


def test_single_member_group_has_no_discovery_entities_but_remains_commandable():
    client = FakeClient()
    controller = FakeController()
    bridge = MqttBridge(
        client,
        controller,
        {"left": BlindEntity("left", "Left", tdbu=True)},
        MqttSettings(topic_prefix="test", discovery_prefix="ha"),
        groups={"solo": GroupEntity("solo", "Solo", ("left",))},
    )
    bridge.publish_discovery()
    solo_messages = [item for item in client.published if "group_solo" in item[0]]
    assert len(solo_messages) == 4
    assert all(payload == "" and qos == 1 and retained
               for _, payload, qos, retained in solo_messages)
    bridge.subscribe_commands()
    assert ("test/group/solo/command", 1) in client.subscribed
    assert not any("test/group/solo/action/" in topic for topic, _ in client.subscribed)
    assert asyncio.run(bridge.handle_message("test/group/solo/command", "OPEN"))
    assert not asyncio.run(bridge.handle_message(
        "test/group/solo/action/favorite", "PRESS"
    ))
    assert controller.commands[-1][2]["target_type"] == "group"


def test_republishing_discovery_clears_entities_removed_by_topology_change():
    bridge, client, _ = make_bridge()
    bridge.groups["living"] = GroupEntity("living", "Living", ("left",))
    bridge.publish_discovery()
    assert not any(
        topic.endswith("graber_rpi_controller_group_living/config") and payload
        for topic, payload, *_ in client.published
    )

    bridge.groups["living"] = GroupEntity("living", "Living", ("left", "right"))
    bridge.publish_discovery()
    first_publish_count = len(client.published)
    bridge.groups["living"] = GroupEntity("living", "Living", ("left",))
    bridge.publish_discovery()
    cleanup = client.published[first_publish_count:]
    stale_topics = {
        topic for topic, payload, qos, retained in cleanup
        if payload == "" and qos == 1 and retained
    }
    assert "ha/cover/graber_rpi_controller_group_living/config" in stale_topics
    assert "ha/button/graber_rpi_controller_group_living_favorite/config" in stale_topics


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


def test_tdbu_full_cover_routes_only_verified_full_open_close_and_stop():
    bridge, client, controller = make_bridge()
    bridge.subscribe_commands()
    assert ("test-pilot/blind/left/full/command", 1) in client.subscribed
    assert ("test-pilot/blind/left/full/set_position", 1) not in client.subscribed

    for command in ("OPEN", "CLOSE", "STOP"):
        assert asyncio.run(
            bridge.handle_message("test-pilot/blind/left/full/command", command)
        )
    assert not asyncio.run(
        bridge.handle_message("test-pilot/blind/left/full/set_position", "50")
    )
    assert not asyncio.run(
        bridge.handle_message("test-pilot/blind/right/full/command", "OPEN")
    )
    assert [
        (target, action, kwargs["position"], kwargs["target_type"])
        for target, action, kwargs in controller.commands
    ] == [
        ("left", "open", None, "blind"),
        ("left", "close", None, "blind"),
        ("left", "stop", None, "blind"),
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


def test_action_buttons_subscribe_with_qos_and_dispatch_explicit_target_types():
    bridge, client, controller = make_bridge()
    bridge.subscribe_commands()
    assert ("test-pilot/blind/left/action/stack_up", 1) in client.subscribed
    assert ("test-pilot/blind/right/action/favorite", 1) in client.subscribed
    assert ("test-pilot/group/living/action/favorite", 1) in client.subscribed
    assert ("test-pilot/group/living/action/stack_up", 1) not in client.subscribed

    assert asyncio.run(bridge.handle_message(
        "test-pilot/blind/left/action/stack_down", "PRESS"
    ))
    assert asyncio.run(bridge.handle_message(
        "test-pilot/group/living/action/favorite", "PRESS"
    ))
    assert asyncio.run(bridge.handle_message(
        "test-pilot/blind/right/action/favorite", "PRESS"
    ))
    assert [
        (target, action, kwargs["target_type"])
        for target, action, kwargs in controller.commands
    ] == [
        ("left", "stack_down", "blind"),
        ("living", "favorite", "group"),
        ("right", "favorite", "blind"),
    ]


def test_action_buttons_reject_retained_wrong_payload_and_unsupported_stack():
    bridge, _, controller = make_bridge()
    assert not asyncio.run(bridge.handle_message(
        "test-pilot/blind/left/action/favorite", "PRESS", retained=True
    ))
    assert not asyncio.run(bridge.handle_message(
        "test-pilot/blind/left/action/favorite", "favorite"
    ))
    assert not asyncio.run(bridge.handle_message(
        "test-pilot/blind/right/action/stack_up", "PRESS"
    ))
    assert not asyncio.run(bridge.handle_message(
        "test-pilot/group/living/action/stack_down", "PRESS"
    ))
    assert controller.commands == []


def test_single_rail_and_mixed_group_clear_only_stale_stack_buttons():
    bridge, client, _ = make_bridge()
    bridge.publish_discovery()
    expected = {
        *(f"ha/button/graber_rpi_controller_blind_right_{action}/config"
          for action in ("stack_up", "stack_down")),
        *(f"ha/button/graber_rpi_controller_group_living_{action}/config"
          for action in ("stack_up", "stack_down")),
    }
    cleared = {
        topic for topic, payload, qos, retained in client.published
        if payload == "" and qos == 1 and retained
    }
    assert expected <= cleared
    assert "ha/button/graber_rpi_controller_blind_right_favorite/config" not in cleared
    assert "ha/button/graber_rpi_controller_group_living_favorite/config" not in cleared


def test_action_topics_disambiguate_same_blind_and_group_id():
    client = FakeClient()
    controller = FakeController()
    bridge = MqttBridge(
        client,
        controller,
        {
            "shared": BlindEntity("shared", "Shared Blind", tdbu=True),
            "other": BlindEntity("other", "Other Blind", tdbu=True),
        },
        MqttSettings(topic_prefix="test", discovery_prefix="ha"),
        groups={"shared": GroupEntity("shared", "Shared Group", ("shared", "other"))},
    )
    assert asyncio.run(bridge.handle_message(
        "test/blind/shared/action/favorite", "PRESS"
    ))
    assert asyncio.run(bridge.handle_message(
        "test/group/shared/action/stack_up", "PRESS"
    ))
    assert [
        (target, action, kwargs["target_type"])
        for target, action, kwargs in controller.commands
    ] == [
        ("shared", "favorite", "blind"),
        ("shared", "stack_up", "group"),
    ]


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


def test_tdbu_full_cover_state_is_derived_only_from_observed_rail_feedback():
    bridge, client, _ = make_bridge()
    bridge.publish_observed_state("left", bottom_position=0, top_position=0)
    assert client.published[-1][:2] == (
        "test-pilot/blind/left/full/state", "closed"
    )

    bridge.publish_observed_state("left", bottom_position=0, top_position=31)
    assert client.published[-1][:2] == (
        "test-pilot/blind/left/full/state", "open"
    )

    bridge.publish_observed_state("left", bottom_position=100, top_position=0)
    assert client.published[-1][:2] == (
        "test-pilot/blind/left/full/state", "open"
    )


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


def test_connect_callback_accepts_paho_reason_code_objects():
    class ReasonCode:
        def __eq__(self, other):
            return other == 0

        def __int__(self):
            raise TypeError("Paho v2 reason codes are not integers")

    bridge, client, _ = make_bridge()
    paho = type("PahoClient", (), {})()
    loop = asyncio.new_event_loop()
    try:
        attach_paho_callbacks(bridge, paho, loop)
        paho.on_connect(paho, None, None, ReasonCode())
    finally:
        loop.close()
    assert client.subscribed
    assert any(topic.startswith("ha/cover/") for topic, *_ in client.published)
