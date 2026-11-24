"""Connect the controller's state and lifecycle to a Paho MQTT client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from graber.bluetooth.protocol import Rail, raw_to_ha_position
from graber.config import ControllerConfig, MqttConfig
from graber.control.commands import PositionState
from graber.control.group_controller import Controller

from .mqtt_bridge import BlindEntity, GroupEntity, MqttBridge, MqttSettings, attach_paho_callbacks

if TYPE_CHECKING:
    from paho.mqtt.client import Client


def _create_client(settings: MqttConfig) -> Client:
    # A controller with MQTT disabled does not require the Paho package.
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.client_id)
    if settings.username:
        client.username_pw_set(settings.username, settings.password)
    if settings.tls:
        client.tls_set(ca_certs=settings.ca_certs, certfile=settings.certfile, keyfile=settings.keyfile)
        client.tls_insecure_set(settings.tls_insecure)
    return client


def _cover_state(position: int | None) -> str | None:
    if position is None:
        return None
    return "closed" if position == 0 else "open"


@dataclass
class MqttService:
    client: Client
    bridge: MqttBridge
    controller: Controller

    def publish_state(self, blind_id: str, state: PositionState) -> None:
        top = raw_to_ha_position(Rail.TOP, state.top) if state.top is not None else None
        observed_at = datetime.fromtimestamp(state.updated_at, timezone.utc) if state.updated_at else None
        self.bridge.publish_observed_state(
            blind_id,
            bottom_position=state.bottom,
            top_position=top,
            movement=_cover_state(state.bottom),
            top_movement=_cover_state(top),
            diagnostics={
                "battery": state.battery,
                "last_successful_communication": observed_at.isoformat() if observed_at else None,
                **self.controller.diagnostics_for(blind_id),
            },
        )
        self.bridge.publish_blind_availability(blind_id, state.updated_at is not None)

    def publish_adapter_health(self) -> None:
        for adapter_id, health in self.bridge.adapter_health_provider().items():
            self.bridge.publish_adapter_health(adapter_id, health)

    def publish_result(self, blind_id: str, _diagnostics: dict[str, object]) -> None:
        self.publish_state(blind_id, self.controller.dispatchers[blind_id].blind.state)
        self.publish_adapter_health()

    def republish_current_state(self) -> None:
        for blind_id, dispatcher in self.controller.dispatchers.items():
            self.publish_state(blind_id, dispatcher.blind.state)

    def close(self) -> None:
        try:
            self.bridge.publish_availability(False)
        finally:
            try:
                self.client.disconnect()
            finally:
                self.client.loop_stop()


def start_mqtt(
    config: ControllerConfig,
    controller: Controller,
    adapter_health: Callable[[], Mapping[str, Mapping[str, object]]],
    telemetry: Callable[[str, dict[str, object]], None],
) -> MqttService:
    settings = config.mqtt
    client = _create_client(settings)
    bridge = MqttBridge(
        client, controller,
        blinds={blind.id: BlindEntity(blind.id, blind.name, blind.dual_rail) for blind in config.blinds},
        settings=MqttSettings(
            topic_prefix=settings.topic_prefix, discovery_prefix=settings.discovery_prefix,
            device_id=settings.device_id, device_name=settings.device_name,
            entity_name_suffix=settings.entity_name_suffix,
        ),
        adapters=tuple(adapter.id for adapter in config.adapters),
        adapter_health_provider=adapter_health,
        groups={group.id: GroupEntity(group.id, group.name, group.members) for group in config.groups},
    )
    service = MqttService(client, bridge, controller)
    controller.set_result_listener(service.publish_result)
    attach_paho_callbacks(
        bridge, client, asyncio.get_running_loop(),
        error_handler=lambda exc: telemetry("mqtt_callback_failed", {"error": f"{type(exc).__name__}: {exc}"}),
        connected_handler=service.republish_current_state,
    )
    client.will_set(bridge.availability_topic, "offline", qos=1, retain=True)
    client.connect_async(settings.host, settings.port)
    client.loop_start()
    return service
