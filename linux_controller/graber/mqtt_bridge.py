"""MQTT and Home Assistant Discovery boundary.

This module deliberately contains no BLE logic.  The bridge accepts commands and
hands them to a controller; state is published only when the controller reports
observed feedback.  This keeps Home Assistant entities non-optimistic.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol


LOG = logging.getLogger(__name__)


class MqttClient(Protocol):
    """Small subset shared by paho and the test client."""

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> Any: ...

    def subscribe(self, topic: str, qos: int = 0) -> Any: ...


class CommandController(Protocol):
    async def submit_command(
        self,
        target_id: str,
        action: str,
        *,
        position: int | None = None,
        command_id: str,
        source: str,
        target_type: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class MqttSettings:
    topic_prefix: str = "graber-rpi-pilot"
    discovery_prefix: str = "homeassistant"
    qos: int = 1
    device_id: str = "graber_rpi_pilot"
    device_name: str = "Graber Raspberry Pi Pilot"


@dataclass(frozen=True)
class BlindEntity:
    blind_id: str
    name: str
    tdbu: bool = False


@dataclass(frozen=True)
class GroupEntity:
    group_id: str
    name: str
    members: tuple[str, ...] = ()


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in value.lower())


class MqttBridge:
    """Translate MQTT messages to commands and observed state to MQTT."""

    def __init__(
        self,
        client: MqttClient,
        controller: CommandController,
        blinds: Mapping[str, BlindEntity],
        settings: MqttSettings | None = None,
        adapters: tuple[str, ...] = (),
        adapter_health_provider: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        groups: Mapping[str, GroupEntity] | None = None,
    ) -> None:
        self.client = client
        self.controller = controller
        self.blinds = dict(blinds)
        self.settings = settings or MqttSettings()
        self.adapters = adapters
        self.adapter_health_provider = adapter_health_provider or (lambda: {})
        self.groups = dict(groups or {})
        self._mqtt_ids: OrderedDict[tuple[str, int], str] = OrderedDict()

    def command_id_for_delivery(self, topic: str, mid: int, duplicate: bool) -> str:
        key = (topic, mid)
        if duplicate and key in self._mqtt_ids:
            return self._mqtt_ids[key]
        command_id = uuid.uuid4().hex
        self._mqtt_ids[key] = command_id
        self._mqtt_ids.move_to_end(key)
        while len(self._mqtt_ids) > 256:
            self._mqtt_ids.popitem(last=False)
        return command_id

    @property
    def availability_topic(self) -> str:
        return f"{self.settings.topic_prefix}/status"

    def blind_availability_topic(self, blind_id: str) -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/status"

    def group_command_topic(self, group_id: str) -> str:
        return f"{self.settings.topic_prefix}/group/{group_id}/command"

    def _blind_availability(self, blind_id: str) -> list[dict[str, str]]:
        return [
            {
                "topic": self.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": self.blind_availability_topic(blind_id),
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ]

    def command_topic(self, blind_id: str, rail: str = "bottom") -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/{rail}/command"

    def set_position_topic(self, blind_id: str, rail: str = "bottom") -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/{rail}/set_position"

    def state_topic(self, blind_id: str, rail: str = "bottom") -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/{rail}/state"

    def position_topic(self, blind_id: str, rail: str = "bottom") -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/{rail}/position"

    def diagnostics_topic(self, blind_id: str) -> str:
        return f"{self.settings.topic_prefix}/blind/{blind_id}/diagnostics"

    def publish_discovery(self) -> None:
        """Publish separate, retained, non-optimistic pilot entities."""
        device = {
            "identifiers": [self.settings.device_id],
            "name": self.settings.device_name,
            "manufacturer": "Community Graber BLE controller",
        }
        for blind in self.blinds.values():
            rails = ("bottom", "top") if blind.tdbu else ("bottom",)
            for rail in rails:
                suffix = f" {rail.title()}" if blind.tdbu else ""
                entity_id = _slug(f"{self.settings.device_id}_{blind.blind_id}_{rail}")
                payload = {
                    "name": f"{blind.name}{suffix} (Pi Pilot)",
                    "unique_id": entity_id,
                    "device": device,
                    "command_topic": self.command_topic(blind.blind_id, rail),
                    "set_position_topic": self.set_position_topic(blind.blind_id, rail),
                    "state_topic": self.state_topic(blind.blind_id, rail),
                    "position_topic": self.position_topic(blind.blind_id, rail),
                    "availability": self._blind_availability(blind.blind_id),
                    "availability_mode": "all",
                    "position_open": 100,
                    "position_closed": 0,
                    "optimistic": False,
                }
                topic = f"{self.settings.discovery_prefix}/cover/{entity_id}/config"
                self._publish(topic, payload, retain=True)

            diagnostic_id = _slug(f"{self.settings.device_id}_{blind.blind_id}_diagnostics")
            diagnostic = {
                "name": f"{blind.name} Pi Diagnostics",
                "unique_id": diagnostic_id,
                "device": device,
                "state_topic": self.diagnostics_topic(blind.blind_id),
                "value_template": "{{ value_json.last_command_status }}",
                "json_attributes_topic": self.diagnostics_topic(blind.blind_id),
                "entity_category": "diagnostic",
                "availability": self._blind_availability(blind.blind_id),
                "availability_mode": "all",
            }
            self._publish(
                f"{self.settings.discovery_prefix}/sensor/{diagnostic_id}/config",
                diagnostic,
                retain=True,
            )
            for key, label, unit, device_class in (
                ("battery", "Battery", "%", "battery"),
                ("last_successful_communication", "Last Communication", None, "timestamp"),
                ("connection_failures", "Connection Failures", None, None),
                ("retry_count", "Retry Count", None, None),
            ):
                sensor_id = _slug(f"{self.settings.device_id}_{blind.blind_id}_{key}")
                sensor = {
                    "name": f"{blind.name} Pi {label}",
                    "unique_id": sensor_id,
                    "device": device,
                    "state_topic": self.diagnostics_topic(blind.blind_id),
                    "value_template": "{{ value_json.%s }}" % key,
                    "entity_category": "diagnostic",
                    "availability": self._blind_availability(blind.blind_id),
                    "availability_mode": "all",
                }
                if unit:
                    sensor["unit_of_measurement"] = unit
                if device_class:
                    sensor["device_class"] = device_class
                self._publish(
                    f"{self.settings.discovery_prefix}/sensor/{sensor_id}/config",
                    sensor,
                    retain=True,
                )
        for group in self.groups.values():
            entity_id = _slug(f"{self.settings.device_id}_group_{group.group_id}")
            self._publish(
                f"{self.settings.discovery_prefix}/cover/{entity_id}/config",
                {
                    "name": f"{group.name} (Pi Pilot)",
                    "unique_id": entity_id,
                    "device": device,
                    "command_topic": self.group_command_topic(group.group_id),
                    "availability_topic": self.availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "optimistic": False,
                },
                retain=True,
            )
        for adapter_id in self.adapters:
            entity_id = _slug(f"{self.settings.device_id}_{adapter_id}_health")
            self._publish(
                f"{self.settings.discovery_prefix}/sensor/{entity_id}/config",
                {
                    "name": f"{adapter_id} Bluetooth Health",
                    "unique_id": entity_id,
                    "device": device,
                    "state_topic": f"{self.settings.topic_prefix}/adapter/{_slug(adapter_id)}/diagnostics",
                    "value_template": "{{ value_json.status }}",
                    "json_attributes_topic": f"{self.settings.topic_prefix}/adapter/{_slug(adapter_id)}/diagnostics",
                    "entity_category": "diagnostic",
                    "availability_topic": self.availability_topic,
                },
                retain=True,
            )
            health = self.adapter_health_provider().get(adapter_id, {"status": "unknown"})
            self.publish_adapter_health(adapter_id, health)

    def subscribe_commands(self) -> None:
        for blind in self.blinds.values():
            rails = ("bottom", "top") if blind.tdbu else ("bottom",)
            for rail in rails:
                self.client.subscribe(self.command_topic(blind.blind_id, rail), self.settings.qos)
                self.client.subscribe(
                    self.set_position_topic(blind.blind_id, rail), self.settings.qos
                )
        for group in self.groups.values():
            self.client.subscribe(self.group_command_topic(group.group_id), self.settings.qos)

    async def handle_message(self, topic: str, payload: bytes | str, *, retained: bool = False,
                             command_id: str | None = None) -> bool:
        """Validate and dispatch one message.

        Retained commands are always ignored so reconnecting cannot replay stale
        physical movement.
        """
        if retained:
            LOG.warning("ignoring retained movement command on %s", topic)
            return False
        parsed = self._parse_command_topic(topic)
        if parsed is None:
            return False
        target_id, target_type, rail, is_position = parsed
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        text = text.strip()
        position: int | None = None
        if is_position:
            try:
                position = int(text)
            except ValueError:
                return False
            if not 0 <= position <= 100:
                return False
            action = f"set_{rail}_position"
        else:
            command = text.upper()
            if command not in {"OPEN", "CLOSE", "STOP"}:
                return False
            if command == "STOP":
                action = "stop"
            elif rail == "top":
                action = f"{command.lower()}_top"
            elif target_type == "blind" and self.blinds[target_id].tdbu:
                action = f"{command.lower()}_bottom"
            else:
                action = command.lower()
        await self.controller.submit_command(
            target_id,
            action,
            position=position,
            command_id=command_id or uuid.uuid4().hex,
            source="mqtt",
            target_type=target_type,
        )
        return True

    def _parse_command_topic(self, topic: str) -> tuple[str, str, str, bool] | None:
        group_prefix = f"{self.settings.topic_prefix}/group/"
        if topic.startswith(group_prefix):
            parts = topic[len(group_prefix) :].split("/")
            if len(parts) != 2 or parts[0] not in self.groups or parts[1] != "command":
                return None
            return parts[0], "group", "bottom", False
        prefix = f"{self.settings.topic_prefix}/blind/"
        if not topic.startswith(prefix):
            return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3:
            return None
        blind_id, rail, operation = parts
        blind = self.blinds.get(blind_id)
        if blind is None or rail not in ({"bottom", "top"} if blind.tdbu else {"bottom"}):
            return None
        if operation not in {"command", "set_position"}:
            return None
        return blind_id, "blind", rail, operation == "set_position"

    def publish_observed_state(
        self,
        blind_id: str,
        *,
        bottom_position: int | None = None,
        top_position: int | None = None,
        movement: str | None = None,
        top_movement: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish only feedback supplied by the state machine/advertisement path."""
        if blind_id not in self.blinds:
            raise KeyError(blind_id)
        if bottom_position is not None:
            self._publish(self.position_topic(blind_id), str(bottom_position))
        if top_position is not None:
            self._publish(self.position_topic(blind_id, "top"), str(top_position))
        if movement is not None:
            self._publish(self.state_topic(blind_id), movement)
        if top_movement is not None:
            self._publish(self.state_topic(blind_id, "top"), top_movement)
        if diagnostics is not None:
            self._publish(self.diagnostics_topic(blind_id), diagnostics)

    def publish_availability(self, available: bool) -> None:
        self.client.publish(
            self.availability_topic,
            "online" if available else "offline",
            qos=self.settings.qos,
            retain=True,
        )

    def publish_blind_availability(self, blind_id: str, available: bool) -> None:
        if blind_id not in self.blinds:
            raise KeyError(blind_id)
        self.client.publish(
            self.blind_availability_topic(blind_id),
            "online" if available else "offline",
            qos=self.settings.qos,
            retain=True,
        )

    def publish_adapter_health(self, adapter_id: str, health: Mapping[str, Any]) -> None:
        """Expose adapter status without leaking controller addresses into topic names."""
        safe_id = _slug(adapter_id)
        self._publish(
            f"{self.settings.topic_prefix}/adapter/{safe_id}/diagnostics", health, retain=True
        )

    def _publish(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        serialized = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        self.client.publish(topic, serialized, qos=self.settings.qos, retain=retain)


def attach_paho_callbacks(
    bridge: MqttBridge,
    client: Any,
    loop: asyncio.AbstractEventLoop,
    error_handler: Callable[[BaseException], None] | None = None,
    connected_handler: Callable[[], Awaitable[None] | None] | None = None,
) -> None:
    """Attach paho callbacks without importing paho at module import time.

    Paho invokes callbacks on its network thread.  Commands are transferred to
    the asyncio loop; exceptions are consumed and logged instead of becoming
    unobserved task errors.
    """

    def report_error(error: BaseException) -> None:
        if error_handler:
            error_handler(error)
        else:
            LOG.error("MQTT callback failed", exc_info=error)

    def done(completed: Any) -> None:
        error = completed.exception()
        if error is not None:
            report_error(error)

    def on_connect(_client: Any, _userdata: Any, _flags: Any, reason_code: Any, *_: Any) -> None:
        if int(reason_code) == 0:
            bridge.subscribe_commands()
            bridge.publish_discovery()
            bridge.publish_availability(True)
            if connected_handler:
                try:
                    result = connected_handler()
                except BaseException as exc:  # callbacks must not escape Paho's thread
                    report_error(exc)
                else:
                    if inspect.isawaitable(result):
                        future = asyncio.run_coroutine_threadsafe(result, loop)
                        future.add_done_callback(done)

    def on_message(_client: Any, _userdata: Any, message: Any) -> None:
        future = asyncio.run_coroutine_threadsafe(
            bridge.handle_message(
                message.topic, message.payload, retained=bool(message.retain),
                command_id=bridge.command_id_for_delivery(
                    message.topic, message.mid, bool(message.dup)
                ),
            ),
            loop,
        )

        future.add_done_callback(done)

    client.on_connect = on_connect
    client.on_message = on_message
