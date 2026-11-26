"""Service entry point.  No live operation occurs until explicit config loads."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from contextlib import AsyncExitStack, suppress
from functools import partial
from pathlib import Path

from graber.adapter_manager import AdapterManager
from graber.adapter_recovery import recover_adapter_power
from graber.ble_transport import BleakTransport
from graber.battery import BatteryHistory
from graber.blind import BlindController
from graber.config import config_paths_from_env, load_config
from graber.dispatcher import BlindDispatcher
from graber.group_controller import Controller
from graber.mqtt_service import start_mqtt


LOG = logging.getLogger("graber")

# Even if a local config is accidentally set too low, background battery work
# must remain low-frequency in production. Tests can inject a smaller floor.
MINIMUM_BATTERY_REFRESH_S = 60.0


def publish_command_lifecycle(
    controller: Controller, event: str, fields: dict[str, object]
) -> None:
    """Translate BLE write telemetry into user-visible command progress."""
    if event != "gatt_write_acknowledged" or fields.get("internal") is True:
        return
    blind_id = fields.get("blind_id")
    command_id = fields.get("command_id")
    if not isinstance(blind_id, str) or not isinstance(command_id, str):
        return
    controller.record_lifecycle(blind_id, "write_acknowledged", command_id)
    if fields.get("step") == fields.get("steps"):
        controller.record_lifecycle(blind_id, "awaiting_feedback", command_id)


def telemetry(event: str, fields: dict[str, object]) -> None:
    LOG.info("%s %s", event, json.dumps(fields, sort_keys=True, default=str))


async def refresh_battery_periodically(
    blind: BlindController,
    report=telemetry,
    *,
    minimum_interval_s: float = MINIMUM_BATTERY_REFRESH_S,
) -> None:
    """Refresh one blind's status without movement writes or rapid retries."""
    interval_s = max(minimum_interval_s, blind.config.battery_interval_s)
    if blind.battery_read_since_start:
        await asyncio.sleep(interval_s)
    while True:
        try:
            await blind.refresh_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report("battery_refresh_failed", {
                "blind_id": blind.config.id,
                "error": f"{type(exc).__name__}: {exc}",
            })
        await asyncio.sleep(interval_s)


async def run_service(config_path: str, secrets_path: str, *, web_override=None) -> None:
    # Import deployment-only modules lazily so offline protocol tests stay light.
    from graber.diagnostics import JsonlMetricsWriter, resolve_configured_adapters

    config = load_config(config_path, secrets_path)
    metrics = JsonlMetricsWriter(config.metrics_path, config.metrics_max_bytes) if config.metrics_path else None
    controller: Controller | None = None

    def service_telemetry(event: str, fields: dict[str, object]) -> None:
        telemetry(event, fields)
        if metrics:
            try:
                metrics.record(event, fields)
            except OSError as exc:
                LOG.warning("Could not persist controller metrics: %s", type(exc).__name__)
        if controller is not None:
            publish_command_lifecycle(controller, event, fields)

    manager = AdapterManager(config.adapters, resolve_configured_adapters)
    recovery_resolver = partial(resolve_configured_adapters, recover_power=recover_adapter_power)
    await manager.refresh()
    transport = BleakTransport()
    dispatchers: dict[str, BlindDispatcher] = {}
    mqtt_service = None

    def state_changed(blind_id, state) -> None:
        if mqtt_service is not None:
            mqtt_service.publish_state(blind_id, state)

    user_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    state_dir = Path(os.environ.get("GRABER_PORTAL_STATE_DIR") or user_state / "graber-controller")
    battery_history = BatteryHistory(state_dir / "battery-history.json")
    for blind_config in config.blinds:
        blind = BlindController(blind_config, manager, transport, config.retry,
                                config.warm_connection_s, telemetry=service_telemetry,
                                state_listener=state_changed, battery_history=battery_history)
        dispatchers[blind_config.id] = BlindDispatcher(
            blind, telemetry=service_telemetry
        )
    controller = Controller(
        dispatchers, config.groups, manager.health, telemetry=service_telemetry
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    blinds_by_address = {
        dispatcher.blind.config.address.upper(): dispatcher.blind
        for dispatcher in dispatchers.values()
    }

    def advertisement_observed(device) -> None:
        blind = blinds_by_address.get(device.address.upper())
        if blind is not None:
            blind.observe_advertisement(device)

    async def monitor_advertisements() -> None:
        last_resolution_error: str | None = None
        while True:
            adapters: dict[str, str] = {}
            resolution_error: Exception | None = None
            for blind_config in config.blinds:
                try:
                    for runtime in await manager.candidates(blind_config):
                        if runtime.resolved is not None:
                            adapters[runtime.config.id] = runtime.resolved.device
                except Exception as exc:
                    resolution_error = exc
            if not adapters:
                if resolution_error is not None:
                    error = f"{type(resolution_error).__name__}: {resolution_error}"
                    if error != last_resolution_error:
                        service_telemetry("advertisement_adapters_unavailable", {"error": error})
                    last_resolution_error = error
                await asyncio.sleep(5)
                continue
            last_resolution_error = None
            results = await asyncio.gather(*(
                transport.monitor_advertisements(
                    device, advertisement_observed, 30, set(blinds_by_address)
                )
                for device in adapters.values()
            ), return_exceptions=True)
            for adapter_id, result in zip(adapters, results):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    service_telemetry("advertisement_monitor_failed", {
                        "adapter_id": adapter_id,
                        "error": f"{type(result).__name__}: {result}",
                    })
            await asyncio.sleep(0.25)

    async def refresh_adapter_health() -> None:
        while True:
            await asyncio.sleep(30)
            await manager.refresh(resolver=recovery_resolver)
            if mqtt_service is not None:
                mqtt_service.publish_adapter_health()

    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(controller.close)
        if config.mqtt.enabled:
            mqtt_service = start_mqtt(config, controller, manager.health, service_telemetry)
            cleanup.callback(mqtt_service.close)
        if config.web.enabled or web_override is not None:
            from graber.web import WebSettings, start_web
            if web_override is None:
                allowed_networks = config.web.allowed_networks or WebSettings().allowed_networks
                web_override = WebSettings(
                    host=config.web.host, port=config.web.port,
                    live_controls=config.web.allow_live_commands,
                    allowed_networks=allowed_networks, control_token=config.web.control_token,
                )
            web_runner = await start_web(controller, web_override)
            cleanup.push_async_callback(web_runner.cleanup)
        health_task = asyncio.create_task(refresh_adapter_health(), name="adapter-health")
        advertisement_task = asyncio.create_task(
            monitor_advertisements(), name="advertisement-monitor"
        )
        battery_tasks = tuple(
            asyncio.create_task(
                refresh_battery_periodically(dispatcher.blind, service_telemetry),
                name=f"battery-refresh-{blind_id}",
            )
            for blind_id, dispatcher in dispatchers.items()
        )
        LOG.info("controller_ready blinds=%d adapters=%d", len(config.blinds), len(config.adapters))
        try:
            await stop.wait()
        finally:
            background_tasks = (advertisement_task, health_task, *battery_tasks)
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                with suppress(asyncio.CancelledError):
                    await task


def main() -> None:
    default_config, default_secrets = config_paths_from_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--secrets", default=default_secrets)
    parser.add_argument("--check-config", action="store_true",
                        help="validate local files only; do not inspect Bluetooth or start services")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--portal-host")
    parser.add_argument("--portal-port", type=int)
    parser.add_argument("--portal-allowed-network", action="append", default=[])
    parser.add_argument("--portal-control-token-file")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.check_config:
        config = load_config(args.config, args.secrets)
        print(f"configuration valid: {len(config.blinds)} blind(s), {len(config.adapters)} adapter(s)")
        return
    web_override = None
    if args.portal_host or args.portal_port or args.portal_control_token_file:
        if not (args.portal_host and args.portal_port and args.portal_control_token_file):
            parser.error("portal web override requires host, port, and control token file")
        from graber.web import WebSettings
        token = Path(args.portal_control_token_file).read_text(encoding="utf-8").strip()
        if not token:
            parser.error("portal control token file is empty")
        web_override = WebSettings(
            host=args.portal_host,
            port=args.portal_port,
            live_controls=True,
            allowed_networks=tuple(args.portal_allowed_network or ("127.0.0.0/8", "::1/128")),
            control_token=token,
        )
    asyncio.run(run_service(args.config, args.secrets, web_override=web_override))


if __name__ == "__main__":
    main()
