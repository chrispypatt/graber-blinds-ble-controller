import asyncio

import graber.ble_transport as ble_transport
from graber.ble_transport import BleakConnection, BleakTransport


class FakeDevice:
    def __init__(self, address="11:22:33:44:55:66", name="RRBZ_TEST"):
        self.address = address
        self.name = name


class FakeAdvertisement:
    local_name = "RRBZ_TEST"
    rssi = -52
    manufacturer_data = {1: b"\x00\x00\x2a\x64"}


class FakeScanner:
    instances = []

    def __init__(self, detection_callback, adapter):
        self.callback = detection_callback
        self.adapter = adapter
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def emit(self, device=None, advertisement=None):
        self.callback(device or FakeDevice(), advertisement or FakeAdvertisement())


def make_transport():
    transport = object.__new__(BleakTransport)
    transport._client_type = object
    transport._scanner_type = FakeScanner
    transport._advertisements = {}
    transport._advertisement_events = {}
    transport._monitored_adapters = set()
    FakeScanner.instances.clear()
    return transport


def test_monitor_updates_callback_and_discover_reuses_scanned_device():
    async def scenario():
        transport = make_transport()
        observed = []
        monitor = asyncio.create_task(
            transport.monitor_advertisements(
                "hci9", observed.append, 30, {"11:22:33:44:55:66"}
            )
        )
        await asyncio.sleep(0)
        scanner = FakeScanner.instances[0]
        scanner.emit(FakeDevice("AA:BB:CC:DD:EE:FF"))
        assert observed == []
        device = FakeDevice()
        scanner.emit(device)

        discovered = await transport.discover(device.address, "hci9", 0.1)
        assert discovered.device is device
        assert discovered.rssi == -52
        assert observed == [discovered]
        assert len(FakeScanner.instances) == 1

        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        assert scanner.stopped is True

    asyncio.run(scenario())


def test_discover_waits_for_active_monitor_instead_of_starting_another_scan():
    async def scenario():
        transport = make_transport()
        monitor = asyncio.create_task(
            transport.monitor_advertisements("hci9", lambda _device: None, 30)
        )
        await asyncio.sleep(0)
        discovery = asyncio.create_task(
            transport.discover("11:22:33:44:55:66", "hci9", 0.2)
        )
        await asyncio.sleep(0)
        FakeScanner.instances[0].emit()
        found = await discovery
        assert found.address == "11:22:33:44:55:66"
        assert len(FakeScanner.instances) == 1
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_connect_failure_discards_stale_device_for_reconnect_scan():
    class FailingClient:
        def __init__(self, _device, **_kwargs):
            pass

        async def connect(self):
            raise RuntimeError("org.freedesktop.DBus.Error.UnknownObject")

    async def scenario():
        transport = make_transport()
        transport._client_type = FailingClient
        discovered = transport._discovered(FakeDevice(), FakeAdvertisement())
        transport._remember("hci9", discovered)
        assert transport._advertisements
        try:
            await transport.connect(discovered, "hci9", 0.1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("connect should fail")
        assert transport._advertisements == {}

    asyncio.run(scenario())


def test_monitor_failure_discards_cached_devices():
    class FailingScanner(FakeScanner):
        async def start(self):
            raise RuntimeError("adapter is not available")

    async def scenario():
        transport = make_transport()
        transport._scanner_type = FailingScanner
        discovered = transport._discovered(FakeDevice(), FakeAdvertisement())
        transport._remember("hci9", discovered)
        try:
            await transport.monitor_advertisements("hci9", lambda _device: None, 30)
        except RuntimeError:
            pass
        else:
            raise AssertionError("monitor should fail")
        assert transport._advertisements == {}
        assert "hci9" not in transport._monitored_adapters

    asyncio.run(scenario())


def test_bluez_cleanup_operations_are_bounded(monkeypatch):
    monkeypatch.setattr(ble_transport, "BLUEZ_CLEANUP_TIMEOUT_S", 0.01)

    class HangingClient:
        is_connected = True

        async def disconnect(self):
            await asyncio.Event().wait()

    class HangingScanner(FakeScanner):
        async def start(self):
            await asyncio.Event().wait()

    async def scenario():
        connection = BleakConnection(HangingClient())
        try:
            await connection.disconnect()
        except TimeoutError:
            pass
        else:
            raise AssertionError("disconnect cleanup should time out")

        transport = make_transport()
        transport._scanner_type = HangingScanner
        try:
            await transport.monitor_advertisements("hci9", lambda _device: None, 30)
        except TimeoutError:
            pass
        else:
            raise AssertionError("scanner startup should time out")
        assert "hci9" not in transport._monitored_adapters

    asyncio.run(scenario())
