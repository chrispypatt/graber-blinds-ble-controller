"""Bleak transport boundary and fake-friendly interfaces."""

from __future__ import annotations

import asyncio
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

    async def discover(self, address: str, adapter: str, timeout_s: float) -> DiscoveredDevice:
        wanted = address.upper()
        found: DiscoveredDevice | None = None
        event = asyncio.Event()

        def detected(device: object, advertisement: object) -> None:
            nonlocal found
            if str(device.address).upper() != wanted:  # type: ignore[attr-defined]
                return
            found = DiscoveredDevice(
                device=device, address=str(device.address),  # type: ignore[attr-defined]
                name=getattr(advertisement, "local_name", None) or getattr(device, "name", None),
                rssi=getattr(advertisement, "rssi", None),
                manufacturer_data=dict(getattr(advertisement, "manufacturer_data", {})),
            )
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
