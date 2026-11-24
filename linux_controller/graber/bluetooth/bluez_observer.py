"""Optional BlueZ D-Bus telemetry collected during GATT connections."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator


BLUEZ_PROPERTY_INTERFACE = "org.freedesktop.DBus.Properties"
BLUEZ_DEVICE_INTERFACE = "org.bluez.Device1"
BLUEZ_OBSERVER_TIMEOUT_S = 0.2
# Limit repeated D-Bus setup overhead when the diagnostic bus is unavailable.
BLUEZ_OBSERVER_RETRY_S = 60.0


@dataclass
class BluezConnectPhases:
    started_at: float
    first_link_at: float | None = None
    last_link_at: float | None = None
    services_resolved_at: float | None = None
    link_up_count: int = 0
    link_drop_count: int = 0
    connected: bool | None = None

    def update(self, properties: dict[str, Any], now: float) -> None:
        connected = properties.get("Connected")
        if connected is not None:
            self.connected = connected.value
            if self.connected:
                self.link_up_count += 1
                if self.first_link_at is None:
                    self.first_link_at = now
                self.last_link_at = now
            else:
                self.link_drop_count += 1
                self.services_resolved_at = None
        resolved = properties.get("ServicesResolved")
        # Ignore stale service discovery signals until a dropped link comes back.
        if resolved is not None and self.connected is not False:
            self.services_resolved_at = now if resolved.value else None

    def metrics(self) -> dict[str, float | int]:
        result: dict[str, float | int] = {
            "bluez_link_up_count": self.link_up_count,
            "bluez_link_drop_count": self.link_drop_count,
        }
        for key, when in (
            ("bluez_first_link_ms", self.first_link_at),
            ("bluez_last_link_ms", self.last_link_at),
            ("bluez_services_resolved_ms", self.services_resolved_at),
        ):
            if when is not None:
                result[key] = (when - self.started_at) * 1000
        if (
            self.last_link_at is not None
            and self.services_resolved_at is not None
            and self.services_resolved_at >= self.last_link_at
        ):
            result["bluez_service_resolution_ms"] = (
                self.services_resolved_at - self.last_link_at
            ) * 1000
        return result


class BluezPhaseObserver:
    """Receive read-only property signals and retain data only for tracked paths."""

    def __init__(self) -> None:
        self._bus: Any = None
        self._lock = asyncio.Lock()
        self._retry_at = 0.0
        self.pending: dict[str, BluezConnectPhases] = {}

    def _on_message(self, message: Any) -> None:
        phases = self.pending.get(message.path)
        if (
            phases is not None
            and message.message_type.name == "SIGNAL"
            and message.interface == BLUEZ_PROPERTY_INTERFACE
            and message.member == "PropertiesChanged"
            and message.signature == "sa{sv}as"
            and message.body[0] == BLUEZ_DEVICE_INTERFACE
        ):
            phases.update(message.body[1], time.monotonic())

    async def ensure_subscribed(self) -> bool:
        async with self._lock:
            if self._bus is not None and self._bus.connected:
                return True
            if time.monotonic() < self._retry_at:
                return False
            bus = None
            subscribed = False
            try:
                # D-Bus bindings are only needed for the optional Linux timing observer.
                from dbus_fast import BusType, Message, MessageType
                from dbus_fast.aio import MessageBus

                async with asyncio.timeout(BLUEZ_OBSERVER_TIMEOUT_S):
                    bus = MessageBus(bus_type=BusType.SYSTEM)
                    await bus.connect()
                    bus.add_message_handler(self._on_message)
                    rule = (
                        "type='signal',sender='org.bluez',"
                        f"interface='{BLUEZ_PROPERTY_INTERFACE}',member='PropertiesChanged',"
                        f"arg0='{BLUEZ_DEVICE_INTERFACE}'"
                    )
                    reply = await bus.call(Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=[rule],
                    ))
                    subscribed = reply is not None and reply.message_type == MessageType.METHOD_RETURN
            except Exception:
                # The diagnostic subscription must not prevent a motor connection.
                pass
            finally:
                if bus is not None and not subscribed:
                    try:
                        bus.disconnect()
                    except Exception:
                        pass
            if subscribed:
                self._bus = bus
            else:
                self._retry_at = time.monotonic() + BLUEZ_OBSERVER_RETRY_S
            return subscribed

    @asynccontextmanager
    async def track(self, path: str | None) -> AsyncIterator[BluezConnectPhases | None]:
        if path is None or not await self.ensure_subscribed() or path in self.pending:
            yield None
            return

        phases = BluezConnectPhases(time.monotonic())
        self.pending[path] = phases
        try:
            yield phases
        except (Exception, asyncio.CancelledError) as exc:
            exc.connection_phase_metrics = phases.metrics()
            raise
        finally:
            del self.pending[path]
