"""TOML configuration loading and validation.

Non-secret topology and secret motor credentials deliberately live in separate
files.  Access codes are represented as strings so leading zeroes are never
lost and are excluded from object representations.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 3
    connect_timeout_s: float = 15.0
    gatt_timeout_s: float = 4.0
    feedback_timeout_s: float = 90.0
    backoff_s: float = 0.35
    jitter_s: float = 0.25


@dataclass(frozen=True)
class AdapterConfig:
    id: str
    controller_address: str
    usb_path: str | None = None
    max_connections: int = 5
    max_cold_connections: int = 1


@dataclass(frozen=True)
class BlindConfig:
    id: str
    name: str
    address: str
    dual_rail: bool
    access_code: str = field(repr=False)
    preferred_adapter: str | None = None
    fallback_adapters: tuple[str, ...] = ()
    battery_interval_s: float = 3600.0


@dataclass(frozen=True)
class GroupConfig:
    id: str
    name: str
    members: tuple[str, ...]
    mode: str = "best_effort"
    prepare_timeout_s: float = 12.0
    partial_failure: str = "release_ready"


@dataclass(frozen=True)
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    topic_prefix: str = "graber_rpi_test"
    discovery_prefix: str = "homeassistant"
    client_id: str = "graber-rpi-controller"
    device_id: str = "graber_rpi_controller"
    device_name: str = "Graber Raspberry Pi Controller"
    entity_name_suffix: str = ""
    tls: bool = False
    ca_certs: str | None = None
    certfile: str | None = None
    keyfile: str | None = field(default=None, repr=False)
    tls_insecure: bool = False


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    allow_live_commands: bool = False
    allowed_networks: tuple[str, ...] = ()
    control_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ControllerConfig:
    adapters: tuple[AdapterConfig, ...]
    blinds: tuple[BlindConfig, ...]
    groups: tuple[GroupConfig, ...]
    retry: RetryConfig = RetryConfig()
    warm_connection_s: float = 30.0
    metrics_path: str | None = None
    metrics_max_bytes: int = 2_000_000
    mqtt: MqttConfig = MqttConfig()
    web: WebConfig = WebConfig()


def _table_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ConfigError(f"{key} must be an array of TOML tables")
    return value


def _check_secret_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"secret file {path} must not be accessible by group/other (use chmod 600)")


def load_config(config_path: str | Path, secrets_path: str | Path) -> ControllerConfig:
    config_path, secrets_path = Path(config_path), Path(secrets_path)
    _check_secret_permissions(secrets_path)
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    with secrets_path.open("rb") as stream:
        secrets = tomllib.load(stream)

    adapters = tuple(
        AdapterConfig(
            id=str(item["id"]),
            controller_address=str(item["controller_address"]).upper(),
            usb_path=item.get("usb_path"),
            max_connections=int(item.get("max_connections", 5)),
            max_cold_connections=int(item.get("max_cold_connections", 1)),
        )
        for item in _table_list(raw, "adapters")
    )
    credentials = secrets.get("blinds", {})
    if not isinstance(credentials, dict):
        raise ConfigError("secrets [blinds] table is required")
    blinds: list[BlindConfig] = []
    for item in _table_list(raw, "blinds"):
        blind_id = str(item["id"])
        secret = credentials.get(blind_id, {})
        if not isinstance(secret, dict):
            raise ConfigError(f"missing secrets for blind {blind_id}")
        code = str(secret.get("access_code", ""))
        address = str(secret.get("address", ""))
        if len(code) != 7 or not code.isdigit():
            raise ConfigError(f"blind {blind_id} access_code must be exactly seven digits")
        if not address:
            raise ConfigError(f"blind {blind_id} address is required in secrets")
        blinds.append(BlindConfig(
            id=blind_id,
            name=str(item["name"]),
            address=address.upper(),
            dual_rail=bool(item.get("dual_rail", False)),
            access_code=code,
            preferred_adapter=item.get("preferred_adapter"),
            fallback_adapters=tuple(item.get("fallback_adapters", ())),
            battery_interval_s=float(item.get("battery_interval_s", 3600)),
        ))
    groups = tuple(
        GroupConfig(
            id=str(item["id"]), name=str(item.get("name", item["id"])),
            members=tuple(item["members"]), mode=str(item.get("mode", "best_effort")),
            prepare_timeout_s=float(item.get("prepare_timeout_s", 12)),
            partial_failure=str(item.get("partial_failure", "release_ready")),
        )
        for item in _table_list(raw, "groups")
    )
    retry_raw = raw.get("retry", {})
    retry = RetryConfig(**{k: retry_raw[k] for k in RetryConfig.__dataclass_fields__ if k in retry_raw})
    mqtt_raw = {**raw.get("mqtt", {}), **secrets.get("mqtt", {})}
    mqtt = MqttConfig(**{k: mqtt_raw[k] for k in MqttConfig.__dataclass_fields__ if k in mqtt_raw})
    web_raw = {**raw.get("web", {}), **secrets.get("web", {})}
    web = WebConfig(**{k: web_raw[k] for k in WebConfig.__dataclass_fields__ if k in web_raw})
    controller_raw = raw.get("controller", {})
    result = ControllerConfig(
        adapters=adapters, blinds=tuple(blinds), groups=groups, retry=retry,
        warm_connection_s=float(controller_raw.get("warm_connection_s", 30)),
        metrics_path=controller_raw.get("metrics_path"),
        metrics_max_bytes=int(controller_raw.get("metrics_max_bytes", 2_000_000)),
        mqtt=mqtt, web=web,
    )
    _validate(result)
    return result


def _validate(config: ControllerConfig) -> None:
    def unique(values: list[str], what: str) -> None:
        if len(values) != len(set(values)):
            raise ConfigError(f"duplicate {what} id")
    unique([x.id for x in config.adapters], "adapter")
    unique([x.id for x in config.blinds], "blind")
    unique([x.id for x in config.groups], "group")
    adapter_ids, blind_ids = {x.id for x in config.adapters}, {x.id for x in config.blinds}
    if not adapter_ids:
        raise ConfigError("at least one explicitly identified adapter is required")
    if config.metrics_max_bytes < 100_000:
        raise ConfigError("controller.metrics_max_bytes must be at least 100000")
    for adapter in config.adapters:
        if adapter.max_connections < 1 or adapter.max_cold_connections < 1:
            raise ConfigError(f"adapter {adapter.id} limits must be positive")
    for blind in config.blinds:
        if blind.battery_interval_s <= 0:
            raise ConfigError(f"blind {blind.id} battery_interval_s must be positive")
        choices = ({blind.preferred_adapter} if blind.preferred_adapter else set()) | set(blind.fallback_adapters)
        if not choices <= adapter_ids:
            raise ConfigError(f"blind {blind.id} references unknown adapter(s): {sorted(choices - adapter_ids)}")
    for group in config.groups:
        missing = set(group.members) - blind_ids
        if missing:
            raise ConfigError(f"group {group.id} references unknown blinds: {sorted(missing)}")
        if group.mode not in {"best_effort", "synchronized"}:
            raise ConfigError(f"group {group.id} has invalid mode")
        if group.partial_failure not in {"release_ready", "abort"}:
            raise ConfigError(f"group {group.id} has invalid partial_failure")
    if config.web.enabled and config.web.host not in {"127.0.0.1", "localhost", "::1"} and not config.web.allowed_networks:
        raise ConfigError("web.allowed_networks is required when binding beyond loopback")
    if config.web.allow_live_commands and (
        not config.web.control_token or config.web.control_token.startswith("REPLACE")
    ):
        raise ConfigError("web live commands require a non-placeholder secrets [web].control_token")
    if config.mqtt.enabled:
        if not config.mqtt.host.strip():
            raise ConfigError("mqtt.host is required when MQTT is enabled")
        if not 1 <= config.mqtt.port <= 65535:
            raise ConfigError("mqtt.port must be from 1 through 65535")
        if not config.mqtt.topic_prefix.strip() or not config.mqtt.discovery_prefix.strip():
            raise ConfigError("MQTT topic prefixes must not be empty")
        if not config.mqtt.device_id.strip() or not config.mqtt.device_name.strip():
            raise ConfigError("MQTT device identity must not be empty")
        if bool(config.mqtt.certfile) != bool(config.mqtt.keyfile):
            raise ConfigError("mqtt.certfile and mqtt.keyfile must be configured together")


def config_paths_from_env() -> tuple[str, str]:
    return (
        os.environ.get("GRABER_CONFIG", "/etc/graber-controller/config.toml"),
        os.environ.get("GRABER_SECRETS", "/etc/graber-controller/secrets.toml"),
    )
