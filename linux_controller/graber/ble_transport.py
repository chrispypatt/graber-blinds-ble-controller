"""Bleak transport boundary and fake-friendly interfaces."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


Notification = Callable[[bytes], None]
BLUEZ_CLEANUP_TIMEOUT_S = 5.0
BLUEZ_PROPERTY_INTERFACE = "org.freedesktop.DBus.Properties"
BLUEZ_DEVICE_INTERFACE = "org.bluez.Device1"
BLUEZ_OBSERVER_TIMEOUT_S = 0.2


@dataclass
class _BluezConnectPhases:
    started_at: float
    first_link_at: float | None = None
    last_link_at: float | None = None
    services_resolved_at: float | None = None
    link_up_count: int = 0
    link_drop_count: int = 0

    def update(self, properties: dict[str, Any], now: float) -> None:
        connected = properties.get("Connected")
        if connected is not None:
            if connected.value:
                self.link_up_count += 1
                if self.first_link_at is None:
                    self.first_link_at = now
                self.last_link_at = now
            else:
                self.link_drop_count += 1
                self.services_resolved_at = None
        resolved = properties.get("ServicesResolved")
        if resolved is not None:
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


class _BluezPhaseObserver:
    """Receive read-only property signals and retain data only for tracked paths."""

    def __init__(self) -> None:
        self._bus: Any = None
        self._lock = asyncio.Lock()
        self._retry_at = 0.0
        self.pending: dict[str, _BluezConnectPhases] = {}

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
                self._retry_at = time.monotonic() + 60
            return subscribed


@dataclass(frozen=True)
class DiscoveredDevice:
    device: object
    address: str
    name: str | None
    rssi: int | None
    manufacturer_data: dict[int, bytes]


class BleConnection(Protocol):
    @property
    def connected(self) -> bool: ...
    async def start_notify(self, characteristic: str, callback: Notification) -> None: ...
    async def write(self, characteristic: str, value: bytes) -> None: ...
    async def read(self, characteristic: str) -> bytes: ...
    async def disconnect(self) -> None: ...
    def has_characteristic(self, characteristic: str) -> bool: ...


class BleTransport(Protocol):
    async def discover(self, address: str, adapter: str, timeout_s: float) -> DiscoveredDevice: ...
    async def connect(self, device: DiscoveredDevice, adapter: str, timeout_s: float) -> BleConnection: ...


class BleakConnection:
    def __init__(self, client: object, connection_phase_metrics: dict[str, float | int] | None = None):
        self._client = client
        self.connection_phase_metrics = connection_phase_metrics or {}

    @property
    def connected(self) -> bool:
        return bool(self._client.is_connected)  # type: ignore[attr-defined]

    async def start_notify(self, characteristic: str, callback: Notification) -> None:
        def adapt(_sender: object, data: bytearray) -> None:
            callback(bytes(data))
        await self._client.start_notify(characteristic, adapt)  # type: ignore[attr-defined]

    async def write(self, characteristic: str, value: bytes) -> None:
        # response=True is essential: acknowledgement defines the no-replay boundary.
        await self._client.write_gatt_char(characteristic, value, response=True)  # type: ignore[attr-defined]

    async def read(self, characteristic: str) -> bytes:
        return bytes(await self._client.read_gatt_char(characteristic))  # type: ignore[attr-defined]

    async def disconnect(self) -> None:
        if self.connected:
            await asyncio.wait_for(
                self._client.disconnect(), BLUEZ_CLEANUP_TIMEOUT_S  # type: ignore[attr-defined]
            )

    def has_characteristic(self, characteristic: str) -> bool:
        services = getattr(self._client, "services", None)
        return bool(services and services.get_characteristic(characteristic))


class BleakTransport:
    """Linux Bleak implementation; imports Bleak only when instantiated."""

    def __init__(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("Bleak is required for live Bluetooth operation") from exc
        self._client_type, self._scanner_type = BleakClient, BleakScanner
        self._advertisements: dict[tuple[str, str], tuple[float, DiscoveredDevice]] = {}
        self._advertisement_events: dict[tuple[str, str], asyncio.Event] = {}
        self._monitored_adapters: set[str] = set()
        self._phase_observer = _BluezPhaseObserver()

    @staticmethod
    def _discovered(device: object, advertisement: object) -> DiscoveredDevice:
        return DiscoveredDevice(
            device=device,
            address=str(device.address),  # type: ignore[attr-defined]
            name=getattr(advertisement, "local_name", None) or getattr(device, "name", None),
            rssi=getattr(advertisement, "rssi", None),
            manufacturer_data=dict(getattr(advertisement, "manufacturer_data", {})),
        )

    def _remember(self, adapter: str, discovered: DiscoveredDevice) -> None:
        key = (adapter, discovered.address.upper())
        self._advertisements[key] = (time.monotonic(), discovered)
        event = self._advertisement_events.get(key)
        if event is not None:
            event.set()

    def invalidate_adapter(self, adapter: str) -> None:
        """Drop BLEDevice objects tied to a vanished BlueZ controller."""
        stale = [key for key in self._advertisements if key[0] == adapter]
        for key in stale:
            self._advertisements.pop(key, None)

    async def monitor_advertisements(
        self, adapter: str, callback: Callable[[DiscoveredDevice], None], duration_s: float = 30.0,
        addresses: set[str] | None = None,
    ) -> None:
        """Observe advertisements on one configured adapter for a bounded window.

        Bounded windows are restarted by the service so a lost/reappearing USB
        controller is recovered without restarting the Raspberry Pi. No GATT
        connection is opened by this path.
        """
        wanted = {address.upper() for address in addresses} if addresses is not None else None

        def detected(device: object, advertisement: object) -> None:
            discovered = self._discovered(device, advertisement)
            if wanted is not None and discovered.address.upper() not in wanted:
                return
            self._remember(adapter, discovered)
            callback(discovered)

        scanner = self._scanner_type(detection_callback=detected, adapter=adapter)
        self._monitored_adapters.add(adapter)
        try:
            await asyncio.wait_for(scanner.start(), BLUEZ_CLEANUP_TIMEOUT_S)
            await asyncio.sleep(duration_s)
        except Exception:
            self.invalidate_adapter(adapter)
            raise
        finally:
            self._monitored_adapters.discard(adapter)
            try:
                await asyncio.wait_for(scanner.stop(), BLUEZ_CLEANUP_TIMEOUT_S)
            except Exception:
                pass

    async def discover(self, address: str, adapter: str, timeout_s: float) -> DiscoveredDevice:
        wanted = address.upper()
        key = (adapter, wanted)
        cached = self._advertisements.get(key)
        if cached is not None and time.monotonic() - cached[0] <= max(15.0, timeout_s):
            return cached[1]
        if adapter in self._monitored_adapters:
            event = self._advertisement_events.setdefault(key, asyncio.Event())
            event.clear()
            cached = self._advertisements.get(key)
            if cached is not None and time.monotonic() - cached[0] <= max(15.0, timeout_s):
                return cached[1]
            await asyncio.wait_for(event.wait(), timeout_s)
            return self._advertisements[key][1]
        found: DiscoveredDevice | None = None
        event = asyncio.Event()

        def detected(device: object, advertisement: object) -> None:
            nonlocal found
            if str(device.address).upper() != wanted:  # type: ignore[attr-defined]
                return
            found = self._discovered(device, advertisement)
            self._remember(adapter, found)
            event.set()

        scanner = self._scanner_type(detection_callback=detected, adapter=adapter)
        async with scanner:
            await asyncio.wait_for(event.wait(), timeout_s)
        assert found is not None
        return found

    async def connect(self, device: DiscoveredDevice, adapter: str, timeout_s: float) -> BleConnection:
        # Passing the scanned BLEDevice avoids Bleak's implicit discovery path.
        details = getattr(device.device, "details", None)
        path = details.get("path") if isinstance(details, dict) else None
        observer = self._phase_observer if isinstance(path, str) else None
        phases = None
        if observer is not None and await observer.ensure_subscribed() and path not in observer.pending:
            phases = _BluezConnectPhases(time.monotonic())
            observer.pending[path] = phases
        try:
            client = self._client_type(device.device, adapter=adapter, timeout=timeout_s)
            await asyncio.wait_for(client.connect(), timeout_s)
        except (Exception, asyncio.CancelledError) as exc:
            if phases is not None:
                exc.connection_phase_metrics = phases.metrics()
            # A BLEDevice contains a BlueZ object path scoped to the adapter.
            # It must be rediscovered after USB/BlueZ tears that object down.
            if not isinstance(exc, asyncio.CancelledError):
                self.invalidate_adapter(adapter)
            raise
        finally:
            if phases is not None:
                observer.pending.pop(path, None)
        return BleakConnection(client, phases.metrics() if phases is not None else None)
