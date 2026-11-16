"""Bleak transport boundary and fake-friendly interfaces."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


Notification = Callable[[bytes], None]


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
    def __init__(self, client: object):
        self._client = client

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
            await self._client.disconnect()  # type: ignore[attr-defined]

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
            await scanner.start()
            await asyncio.sleep(duration_s)
        finally:
            self._monitored_adapters.discard(adapter)
            try:
                await scanner.stop()
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
        client = self._client_type(device.device, adapter=adapter, timeout=timeout_s)
        await asyncio.wait_for(client.connect(), timeout_s)
        return BleakConnection(client)
