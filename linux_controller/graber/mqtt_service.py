"""Connect the controller's state and lifecycle to a Paho MQTT client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from graber.protocol import Rail, raw_to_ha_position
from graber.config import ControllerConfig, MqttConfig
from graber.blind import PositionState
from graber.group_controller import Controller

from .mqtt_bridge import BlindEntity, GroupEntity, MqttBridge, MqttSettings, attach_paho_callbacks
from .mqtt_outbox import MAX_PENDING, StateOutbox


CONTACT_INTERVAL_S = 60.0
AVAILABILITY_INTERVAL_S = 30.0
SNAPSHOT_INTERVAL_S = 300.0

if TYPE_CHECKING:
    from paho.mqtt.client import Client


def _create_client(settings: MqttConfig) -> Client:
    # A controller with MQTT disabled does not require the Paho package.
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.client_id)
    # Leave room for the explicit offline message at shutdown.
    client.max_inflight_messages_set(MAX_PENDING + 1)
    client.max_queued_messages_set(MAX_PENDING + 1)
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
class DiagnosticSnapshot:
    fields: dict[str, object]
    has_contact: bool
    next_contact: float


@dataclass
class MqttService:
    client: Client
    bridge: MqttBridge
    controller: Controller
    outbox: StateOutbox
    _diagnostics: dict[str, DiagnosticSnapshot] = field(default_factory=dict)
    _timer: asyncio.TimerHandle | None = None
    _next_snapshot: float = 0.0
    _closed: bool = False

    def publish_state(self, blind_id: str, state: PositionState) -> None:
        if self._closed or not self.client.is_connected():
            return  # Reconnection takes a fresh snapshot from the controller.
        top = raw_to_ha_position(Rail.TOP, state.top) if state.top is not None else None
        battery = state.battery_fields()
        fields = {**battery, **self.controller.diagnostics_for(blind_id)}
        now = self.outbox.loop.time()
        last = self._diagnostics.get(blind_id)
        diagnostics = None
        has_contact = state.updated_at is not None
        if last is None or fields != last.fields or has_contact != last.has_contact or now >= last.next_contact:
            observed_at = datetime.fromtimestamp(state.updated_at, timezone.utc) if state.updated_at else None
            diagnostics = {
                **fields,
                "last_successful_communication": observed_at.isoformat() if observed_at else None,
            }
            self.bridge.publish_battery(blind_id, battery)
            self._diagnostics[blind_id] = DiagnosticSnapshot(fields, has_contact, now + CONTACT_INTERVAL_S)
        self.bridge.publish_observed_state(
            blind_id,
            bottom_position=state.bottom, top_position=top,
            movement=_cover_state(state.bottom), top_movement=_cover_state(top),
            diagnostics=diagnostics, complete_snapshot=True,
        )
        self.bridge.publish_blind_availability(blind_id, state.updated_at is not None)

    def publish_adapter_health(self) -> None:
        for adapter_id, health in self.bridge.adapter_health_provider().items():
            self.bridge.publish_adapter_health(adapter_id, health)

    def publish_result(self, blind_id: str, _diagnostics: dict[str, object]) -> None:
        self.publish_state(blind_id, self.controller.dispatchers[blind_id].blind.state)
        self.publish_adapter_health()

    def _observe_all(self) -> None:
        for blind_id, dispatcher in self.controller.dispatchers.items():
            self.publish_state(blind_id, dispatcher.blind.state)
        self.publish_adapter_health()

    def republish_current_state(self) -> None:
        if self._closed:
            return
        self._diagnostics.clear()
        self._observe_all()
        self.outbox.replay(self.bridge.settings.topic_prefix + "/")
        self._next_snapshot = self.outbox.loop.time() + SNAPSHOT_INTERVAL_S

    def connected(self) -> None:
        if self._closed:
            return
        self.republish_current_state()
        self.outbox.reconnected()
        if self._timer is None:
            self._timer = self.outbox.loop.call_later(AVAILABILITY_INTERVAL_S, self._heartbeat)

    def _heartbeat(self) -> None:
        if self.client.is_connected():
            self.outbox.replay(self.bridge.availability_topic)
            if self.outbox.loop.time() >= self._next_snapshot:
                self.republish_current_state()
            else:
                # Flush the final coalesced contact time even if advertisements stopped.
                self._observe_all()
        self._timer = self.outbox.loop.call_later(AVAILABILITY_INTERVAL_S, self._heartbeat)

    def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        self.outbox.close()
        try:
            if self.client.is_connected():
                # Send directly while the network thread still runs; the offline
                # availability and will are independent of pending state updates.
                self.client.publish(self.bridge.availability_topic, "offline", qos=1, retain=True)
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
    outbox = StateOutbox(client, asyncio.get_running_loop())
    bridge = MqttBridge(
        outbox, controller,
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
    service = MqttService(client, bridge, controller, outbox)
    controller.set_result_listener(service.publish_result)
    attach_paho_callbacks(
        bridge, client, asyncio.get_running_loop(),
        error_handler=lambda exc: telemetry("mqtt_callback_failed", {"error": f"{type(exc).__name__}: {exc}"}),
        connected_handler=service.connected, ha_online_handler=service.republish_current_state,
    )
    client.will_set(bridge.availability_topic, "offline", qos=1, retain=True)
    client.connect_async(settings.host, settings.port)
    client.loop_start()
    return service
