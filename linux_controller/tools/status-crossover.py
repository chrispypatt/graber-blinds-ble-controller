"""Compare single cold status reads across the two configured USB adapters."""

import argparse
import asyncio
import json
import os
import subprocess
from dataclasses import replace

from graber.adapter_manager import AdapterManager
from graber.ble_transport import BleakTransport
from graber.blind import BlindController
from graber.config import load_config
from graber.diagnostics import resolve_configured_adapters

UNIT = "graber-controller.service"
PROCESS_PATTERN = r"(^|[[:space:]])graber\.(main|portal)([[:space:]]|$)"
EVENTS = {
    "connection_ready", "connection_phase_failed", "status_refreshed",
    "status_refresh_failed", "disconnect_cleanup_failed", "disconnected",
}
NUMERIC_FIELDS = {
    "active_queue_ms", "cold_queue_ms", "discovery_ms", "connect_ms",
    "advertisement_rssi", "bluez_first_link_ms", "bluez_last_link_ms",
    "bluez_services_resolved_ms", "bluez_service_resolution_ms",
    "bluez_link_up_count", "bluez_link_drop_count", "notify_setup_ms", "auth_ms",
    "gatt_setup_ms", "rail_read_ms", "battery_read_ms", "total_latency_ms",
    "phase_elapsed_ms",
}
PHASE_FIELDS = {
    "active_slot", "cold_slot", "discovery", "connect", "notify_bottom",
    "notify_top", "auth",
}
ERROR_CATEGORIES = {
    "completion_unconfirmed", "timeout", "adapter_unavailable", "disconnected",
    "bluez", "gatt", "other", "cancelled",
}


def require_controller_stopped():
    state = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "--value"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    if state.stdout.strip() != "inactive":
        raise RuntimeError("controller user unit must be inactive")
    process = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-f", PROCESS_PATTERN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
    )
    if process.returncode != 1:
        raise RuntimeError("could not establish that the standalone controller is stopped")


def write_record(**fields):
    print(json.dumps(fields, sort_keys=True, allow_nan=False), flush=True)


async def wait_for_disconnect(device_path):
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        async with asyncio.timeout(10):
            while True:
                reply = await bus.call(Message(
                    destination="org.bluez", path="/",
                    interface="org.freedesktop.DBus.ObjectManager", member="GetManagedObjects",
                ))
                if reply.message_type != MessageType.METHOD_RETURN:
                    raise RuntimeError("unable to verify Bluetooth link state")
                device = reply.body[0].get(device_path, {}).get("org.bluez.Device1")
                if device is None or not device["Connected"].value:
                    return
                await asyncio.sleep(0.25)
    finally:
        bus.disconnect()


async def run_trial(manager, transport, config, shade, adapter_id, round_number):
    require_controller_stopped()
    trial = {"round": round_number, "blind_id": shade.id, "adapter_id": adapter_id}
    cleanup_failed = False

    def telemetry(event, fields):
        nonlocal cleanup_failed
        if event not in EVENTS:
            return
        if event == "disconnect_cleanup_failed":
            cleanup_failed = True
        safe = {key: fields[key] for key in NUMERIC_FIELDS
                if isinstance(fields.get(key), (int, float)) and not isinstance(fields[key], bool)}
        if fields.get("phase") in PHASE_FIELDS:
            safe["phase"] = fields["phase"]
        if fields.get("error_category") in ERROR_CATEGORIES:
            safe["error_category"] = fields["error_category"]
        write_record(event=event, **trial, **safe)

    forced = replace(shade, preferred_adapter=adapter_id, fallback_adapters=())
    (runtime,) = await manager.candidates(forced)
    path = f"/org/bluez/{runtime.resolved.device}/dev_{shade.address.upper().replace(':', '_')}"
    await wait_for_disconnect(path)
    blind = BlindController(forced, manager, transport, config.retry, telemetry=telemetry)
    positions = {}
    outcome = "failed"
    try:
        state = await blind.refresh_status()
        positions["bottom"] = state.bottom
        if forced.dual_rail:
            positions["top"] = state.top
        outcome = "completed"
    except Exception:
        # The allowlisted telemetry above records a safe error category.
        pass
    finally:
        await blind.close()
    write_record(event="crossover_result", **trial, outcome=outcome, **positions)
    if cleanup_failed:
        await wait_for_disconnect(path)
    require_controller_stopped()


async def run(args):
    require_controller_stopped()
    config = load_config(args.config, args.secrets)
    shades = {shade.id: shade for shade in config.blinds}
    first, second = args.blinds
    left, right = args.adapters
    if first == second or left == right:
        raise ValueError("choose two distinct blinds and adapters")
    adapters = {adapter.id: adapter for adapter in config.adapters}
    manager = AdapterManager((adapters[left], adapters[right]), resolve_configured_adapters)
    await manager.refresh()
    if any(item["status"] != "online" for item in manager.health().values()):
        raise RuntimeError("both test adapters must resolve and be powered")
    transport = BleakTransport()
    pairs = [(first, left), (second, right), (first, right), (second, left)]
    for round_number in range(1, args.rounds + 1):
        order = pairs if round_number % 2 else reversed(pairs)
        for blind_id, adapter_id in order:
            await run_trial(manager, transport, config, shades[blind_id], adapter_id, round_number)
            await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--secrets", required=True)
    parser.add_argument("--rounds", type=int, choices=(2, 3), default=2)
    parser.add_argument("--blinds", nargs=2, default=("sitting_room", "living_room_middle"))
    parser.add_argument("--adapters", nargs=2, default=("usb_1_1", "usb_3_1"))
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        write_record(event="crossover_aborted", error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
