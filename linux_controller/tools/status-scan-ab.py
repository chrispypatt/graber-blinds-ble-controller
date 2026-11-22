"""Compare cold, status-only reads with the target radio scanning or idle."""

import argparse
import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path

from bleak import BleakScanner
from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from graber.adapter_manager import AdapterManager
from graber.ble_transport import BleakTransport
from graber.config import load_config
from graber.diagnostics import resolve_configured_adapters

spec = importlib.util.spec_from_file_location(
    "crossover", Path(__file__).with_name("status-crossover.py")
)
crossover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crossover)


async def is_discovering(bus, adapter):
    reply = await bus.call(Message(
        destination="org.bluez", path=f"/org/bluez/{adapter}",
        interface="org.freedesktop.DBus.Properties", member="Get",
        signature="ss", body=["org.bluez.Adapter1", "Discovering"],
    ))
    if reply.message_type != MessageType.METHOD_RETURN:
        raise RuntimeError("unable to verify adapter scan state")
    return reply.body[0].value


class TrialTransport(BleakTransport):
    def __init__(self, bus, scanning):
        super().__init__()
        self.bus = bus
        self.scanning = scanning
        self.scan_checks = []

    async def connect(self, device, adapter, timeout_s):
        observed = await is_discovering(self.bus, adapter)
        self.scan_checks.append(observed == self.scanning)
        if observed != self.scanning:
            raise RuntimeError("adapter scan mode differs from requested treatment")
        return await super().connect(device, adapter, timeout_s)


async def run_trial(bus, manager, config, shade, adapter_id, round_number, scanning):
    crossover.require_controller_stopped()
    (runtime,) = await manager.candidates(replace(shade, fallback_adapters=()))
    adapter = runtime.resolved.device
    path = f"/org/bluez/{adapter}/dev_{shade.address.upper().replace(':', '_')}"
    await crossover.wait_for_disconnect(path)
    transport = TrialTransport(bus, scanning)
    fresh = asyncio.Event()

    def detected(device, advertisement):
        if device.address.upper() == shade.address.upper():
            transport._remember(adapter, transport._discovered(device, advertisement))
            fresh.set()

    scanner = BleakScanner(detection_callback=detected, adapter=adapter)
    trial = dict(round=round_number, blind_id=shade.id, adapter_id=adapter_id,
                 scanning=scanning)
    crossover.write_record(event="scan_trial_start", **trial)
    await asyncio.wait_for(scanner.start(), 8)
    stopped = False
    try:
        await asyncio.wait_for(fresh.wait(), 10)
        if not scanning:
            await asyncio.wait_for(scanner.stop(), 8)
            stopped = True
        await crossover.run_trial(manager, transport, config, shade, adapter_id, round_number)
        crossover.write_record(event="scan_trial_end", **trial,
                               scan_verified=bool(transport.scan_checks) and all(transport.scan_checks))
    finally:
        if not stopped:
            await asyncio.wait_for(scanner.stop(), 8)


async def run(args):
    crossover.require_controller_stopped()
    config = load_config(args.config, args.secrets)
    shades = {shade.id: shade for shade in config.blinds}
    manager = AdapterManager(config.adapters, resolve_configured_adapters)
    await manager.refresh()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        for round_number in range(1, args.rounds + 1):
            for blind_id in ("sitting_room", "living_room_middle"):
                shade = shades[blind_id]
                for scanning in ((True, False) if round_number % 2 else (False, True)):
                    await run_trial(bus, manager, config, shade, shade.preferred_adapter,
                                    round_number, scanning)
                    await asyncio.sleep(1)
    finally:
        bus.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--secrets", required=True)
    parser.add_argument("--rounds", type=int, choices=(2, 3, 4), default=4)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        crossover.write_record(event="scan_aborted", error_type=type(exc).__name__)
        raise SystemExit(1) from None
