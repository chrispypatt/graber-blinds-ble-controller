"""Load saved setup and render private controller configuration."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any

from graber.config import load_config
from graber.diagnostics.adapters import AdapterIdentity
from .devices import DeviceSetup


def load_known_credentials(path: str | Path) -> dict[str, str]:
    """Load address-to-code mappings without exposing them to browser state.

    The accepted format is the controller's normal private secrets TOML. Blind
    table names are informational; Bluetooth addresses are the match key.
    """

    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError("known-secrets path must be a readable regular file, not a symlink") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("known-secrets path must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError("known-secrets file must be owned by the wizard user")
        if stat.S_IMODE(metadata.st_mode) & 0o177:
            raise ValueError("known-secrets file must have mode 0600 or stricter")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = tomllib.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    blinds = raw.get("blinds", {})
    if not isinstance(blinds, dict):
        raise ValueError("known-secrets file requires a [blinds] table")
    credentials: dict[str, str] = {}
    for blind_id, item in blinds.items():
        if not isinstance(item, dict):
            raise ValueError(f"known secret for {blind_id} must be a table")
        address = str(item.get("address", "")).upper()
        code = str(item.get("access_code", ""))
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ValueError(f"known secret for {blind_id} has an invalid Bluetooth address")
        if re.fullmatch(r"[0-9]{7}", code) is None:
            raise ValueError(f"known secret for {blind_id} has an invalid access code")
        if address in credentials:
            raise ValueError("known-secrets file contains a duplicate Bluetooth address")
        credentials[address] = code
    return credentials


@dataclass(frozen=True)
class SavedDeviceSetup:
    id: str
    friendly_name: str
    dual_rail: bool
    preferred_adapter: str | None
    groups: tuple[str, ...]
    access_code: str = field(repr=False)
    config: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SavedSetup:
    devices: dict[str, SavedDeviceSetup]
    groups: dict[str, str]
    config_base: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets_base: dict[str, Any] = field(default_factory=dict, repr=False)
    adapters: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    group_configs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)


def load_saved_setup(config_path: str | Path, secrets_path: str | Path) -> SavedSetup:
    """Load a previously exported setup without exposing credentials to the UI."""

    config = load_config(config_path, secrets_path)
    with Path(config_path).open("rb") as stream:
        config_base = tomllib.load(stream)
    with Path(secrets_path).open("rb") as stream:
        secrets_base = tomllib.load(stream)
    adapters = {row["id"]: row for row in config_base.pop("adapters", [])}
    blind_configs = {row["id"]: row for row in config_base.pop("blinds", [])}
    group_configs = {row["id"]: row for row in config_base.pop("groups", [])}
    blind_secrets = secrets_base.pop("blinds", {})
    memberships: dict[str, list[str]] = {blind.id: [] for blind in config.blinds}
    groups = {group.id: group.name for group in config.groups}
    for group in config.groups:
        for blind_id in group.members:
            memberships[blind_id].append(group.id)
    devices = {
        blind.address.upper(): SavedDeviceSetup(
            id=blind.id,
            friendly_name=blind.name,
            dual_rail=blind.dual_rail,
            preferred_adapter=blind.preferred_adapter,
            groups=tuple(memberships[blind.id]),
            access_code=blind.access_code,
            config=blind_configs[blind.id],
            secrets=blind_secrets[blind.id],
        )
        for blind in config.blinds
    }
    return SavedSetup(devices=devices, groups=groups,
                      config_base=config_base, secrets_base=secrets_base, adapters=adapters,
                      group_configs=group_configs)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            raise ValueError("nested TOML table arrays are not supported in preserved settings")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported preserved TOML value type: {type(value).__name__}")


def _serialize_toml_tables(raw: dict[str, Any]) -> list[str]:
    """Serialize non-topology settings loaded by tomllib without moving secrets."""

    lines: list[str] = []

    def write_table(path: tuple[str, ...], table: dict[str, Any]) -> None:
        scalars = {key: value for key, value in table.items() if not isinstance(value, dict)}
        children = {key: value for key, value in table.items() if isinstance(value, dict)}
        if path:
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
        for key, value in scalars.items():
            lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        if path or scalars:
            lines.append("")
        for key, value in children.items():
            write_table((*path, str(key)), value)

    write_table((), raw)
    return lines


def _serialize_toml_row(table: str, row: dict[str, Any]) -> list[str]:
    return [f"[[{table}]]", *(
        f"{_toml_key(key)} = {_toml_value(value)}" for key, value in row.items()
    ), ""]


def _write_text_no_follow(path: Path, content: str, mode: int) -> None:
    """Replace a generated file without following an attacker-controlled symlink."""

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_configuration(
    directory: Path, adapters: dict[str, AdapterIdentity], selected: list[DeviceSetup],
    group_names: dict[str, str], saved_setup: SavedSetup,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    config_path, secrets_path = directory / "config.toml", directory / "secrets.toml"
    base_config = saved_setup.config_base
    lines = _serialize_toml_tables(base_config)
    if not base_config:
        lines = ["[controller]", "warm_connection_s = 30", "",
                 "[retry]", "attempts = 3", ""]
    for adapter_id, adapter in adapters.items():
        settings = {"max_connections": 5, "max_cold_connections": 1}
        settings.update(saved_setup.adapters.get(adapter_id, {}))
        settings.update(id=adapter_id, controller_address=adapter.address)
        if adapter.usb_path:
            settings["usb_path"] = adapter.usb_path
        lines.extend(_serialize_toml_row("adapters", settings))
    secret_rows: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[str]] = {}
    for item in selected:
        address = item.observation.address.upper()
        saved = saved_setup.devices.get(address)
        settings = dict(saved.config) if saved else {}
        if not saved or saved.preferred_adapter != item.preferred_adapter:
            settings["fallback_adapters"] = [
                value for value in adapters if value != item.preferred_adapter
            ]
        settings.update(id=item.id, name=item.friendly_name, dual_rail=item.dual_rail)
        if item.preferred_adapter is None:
            settings.pop("preferred_adapter", None)
        else:
            settings["preferred_adapter"] = item.preferred_adapter
        lines.extend(_serialize_toml_row("blinds", settings))
        credentials = dict(saved.secrets) if saved else {}
        credentials.update(address=address, access_code=item.access_code)
        secret_rows[item.id] = credentials
    for item in selected:
        for group in item.groups:
            groups.setdefault(group, []).append(item.id)
    for group, members in groups.items():
        settings = {"mode": "best_effort", **saved_setup.group_configs.get(group, {})}
        existing_order = settings.get("members", [])
        ordered = [member for member in existing_order if member in members]
        ordered.extend(member for member in members if member not in ordered)
        settings.update(id=group, name=group_names.get(group, group.replace("_", " ").title()),
                        members=ordered)
        lines.extend(_serialize_toml_row("groups", settings))
    if not base_config:
        lines.extend(["[mqtt]", "enabled = false", "", "[web]", "enabled = false", ""])
    secret_lines = _serialize_toml_tables({**saved_setup.secrets_base, "blinds": secret_rows})
    config_content = "\n".join(lines) + "\n"
    secrets_content = "\n".join(secret_lines)
    _write_text_no_follow(config_path, config_content, 0o600)
    _write_text_no_follow(secrets_path, secrets_content, 0o600)
    return config_path, secrets_path
