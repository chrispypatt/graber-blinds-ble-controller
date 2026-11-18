"""Service entry point.  No live operation occurs until explicit config loads."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from contextlib import suppress
from functools import partial
from datetime import datetime, timezone

from .adapter_manager import AdapterManager
from .ble_transport import BleakTransport
from .blind import BlindController
from .config import config_paths_from_env, load_config
from .dispatcher import BlindDispatcher
from .group_controller import Controller


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
    if blind.state.battery is not None:
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
    from .diagnostics import JsonlMetricsWriter, resolve_configured_adapters

    config = load_config(config_path, secrets_path)
    metrics = JsonlMetricsWriter(config.metrics_path, config.metrics_max_bytes) if config.metrics_path else None
    controller_ref: dict[str, Controller] = {}
    def service_telemetry(event: str, fields: dict[str, object]) -> None:
        telemetry(event, fields)
        if metrics:
            metrics.record(event, fields)
        controller = controller_ref.get("controller")
        if controller is not None:
            publish_command_lifecycle(controller, event, fields)
    from .adapter_recovery import recover_adapter_power

    manager = AdapterManager(config.adapters, resolve_configured_adapters)
    recovery_resolver = partial(resolve_configured_adapters, recover_power=recover_adapter_power)
    await manager.refresh()
    transport = BleakTransport()
    dispatchers: dict[str, BlindDispatcher] = {}
    state_sinks = []
    def state_changed(blind_id, state) -> None:
        for sink in tuple(state_sinks):
            sink(blind_id, state)
    for blind_config in config.blinds:
        blind = BlindController(blind_config, manager, transport, config.retry,
                                config.warm_connection_s, telemetry=service_telemetry,
                                state_listener=state_changed)
        dispatchers[blind_config.id] = BlindDispatcher(
            blind, telemetry=service_telemetry
        )
    controller = Controller(
        dispatchers, config.groups, manager.health, telemetry=service_telemetry
    )
    controller_ref["controller"] = controller
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    cleanup = []
    bridge = None
    if config.mqtt.enabled:
        import paho.mqtt.client as mqtt
        from .mqtt_bridge import (
            BlindEntity,
            GroupEntity,
            MqttBridge,
            MqttSettings,
            attach_paho_callbacks,
        )
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.mqtt.client_id)
        if config.mqtt.username:
            mqtt_client.username_pw_set(config.mqtt.username, config.mqtt.password)
        if config.mqtt.tls:
            mqtt_client.tls_set(
                ca_certs=config.mqtt.ca_certs,
                certfile=config.mqtt.certfile,
                keyfile=config.mqtt.keyfile,
            )
            mqtt_client.tls_insecure_set(config.mqtt.tls_insecure)
        bridge = MqttBridge(mqtt_client, controller, {
            item.id: BlindEntity(item.id, item.name, item.dual_rail) for item in config.blinds
        }, MqttSettings(
            topic_prefix=config.mqtt.topic_prefix, discovery_prefix=config.mqtt.discovery_prefix,
            device_id=config.mqtt.device_id, device_name=config.mqtt.device_name,
            entity_name_suffix=config.mqtt.entity_name_suffix,
        ), adapters=tuple(item.id for item in config.adapters),
            adapter_health_provider=manager.health,
            groups={
                item.id: GroupEntity(item.id, item.name, item.members)
                for item in config.groups
            })
        from .protocol import Rail, raw_to_ha_position
        def publish_state(blind_id, state) -> None:
            top_position = (
                raw_to_ha_position(Rail.TOP, state.top)
                if state.top is not None else None
            )
            bridge.publish_observed_state(
                blind_id,
                bottom_position=state.bottom,
                top_position=top_position,
                movement=(None if state.bottom is None else
                          "closed" if state.bottom == 0 else "open"),
                top_movement=(None if top_position is None else
                              "closed" if top_position == 0 else "open"),
                diagnostics={
                    "battery": state.battery,
                    "last_successful_communication": (
                        datetime.fromtimestamp(state.updated_at, timezone.utc).isoformat()
                        if state.updated_at else None
                    ),
                    **controller.diagnostics_for(blind_id),
                },
            )
            bridge.publish_blind_availability(blind_id, state.updated_at is not None)
        state_sinks.append(publish_state)
        def publish_result(blind_id, diagnostics) -> None:
            state = dispatchers[blind_id].blind.state
            publish_state(blind_id, state)
            for adapter_id, health in manager.health().items():
                bridge.publish_adapter_health(adapter_id, health)
        controller.set_result_listener(publish_result)
        def republish_current_state() -> None:
            for blind_id, dispatcher in dispatchers.items():
                publish_state(blind_id, dispatcher.blind.state)
        attach_paho_callbacks(
            bridge,
            mqtt_client,
            asyncio.get_running_loop(),
            error_handler=lambda exc: service_telemetry(
                "mqtt_callback_failed", {"error": f"{type(exc).__name__}: {exc}"}
            ),
            connected_handler=republish_current_state,
        )
        mqtt_client.will_set(bridge.availability_topic, "offline", qos=1, retain=True)
        mqtt_client.connect_async(config.mqtt.host, config.mqtt.port)
        mqtt_client.loop_start()
        async def close_mqtt() -> None:
            bridge.publish_availability(False)
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
        cleanup.append(close_mqtt)
    if config.web.enabled or web_override is not None:
        from .web import WebSettings, start_web
        if web_override is None:
            allowed_networks = config.web.allowed_networks or WebSettings().allowed_networks
            web_override = WebSettings(
                host=config.web.host, port=config.web.port,
                live_controls=config.web.allow_live_commands,
                allowed_networks=allowed_networks, control_token=config.web.control_token,
            )
        web_runner = await start_web(controller, web_override)
        cleanup.append(web_runner.cleanup)
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
            if bridge:
                for adapter_id, health in manager.health().items():
                    bridge.publish_adapter_health(adapter_id, health)
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
        for close in reversed(cleanup):
            await close()
        await controller.close()


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
        from pathlib import Path
        from .web import WebSettings
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
