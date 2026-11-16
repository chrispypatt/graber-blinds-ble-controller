from pathlib import Path

import pytest

from graber.config import ConfigError, load_config


def write_config(tmp_path: Path, extra: str = "") -> tuple[Path, Path]:
    config = tmp_path / "config.toml"
    secrets = tmp_path / "secrets.toml"
    config.write_text("""
[controller]
warm_connection_s = 30
[[adapters]]
id = "external_a"
controller_address = "AA:BB:CC:DD:EE:01"
max_connections = 2
max_cold_connections = 1
[[blinds]]
id = "living_left"
name = "Living Left"
dual_rail = true
preferred_adapter = "external_a"
[[groups]]
id = "living_room"
members = ["living_left"]
[web]
host = "0.0.0.0"
allowed_networks = ["192.168.1.0/24"]
""" + extra)
    secrets.write_text('''[blinds.living_left]\naddress = "11:22:33:44:55:66"\naccess_code = "0123456"\n''')
    secrets.chmod(0o600)
    return config, secrets


def test_loads_separate_secrets_without_leaking_repr(tmp_path: Path):
    config, secrets = write_config(tmp_path)
    result = load_config(config, secrets)
    assert result.blinds[0].access_code == "0123456"
    assert "0123456" not in repr(result)
    assert result.groups[0].members == ("living_left",)
    assert result.blinds[0].battery_interval_s == 3600


def test_rejects_nonpositive_battery_interval(tmp_path: Path):
    config, secrets = write_config(tmp_path)
    config.write_text(config.read_text().replace(
        'preferred_adapter = "external_a"',
        'preferred_adapter = "external_a"\nbattery_interval_s = 0',
    ))
    with pytest.raises(ConfigError, match="battery_interval_s must be positive"):
        load_config(config, secrets)


def test_rejects_permissive_secret_file(tmp_path: Path):
    config, secrets = write_config(tmp_path)
    secrets.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(config, secrets)


def test_rejects_unknown_adapter(tmp_path: Path):
    config, secrets = write_config(tmp_path)
    content = config.read_text().replace('preferred_adapter = "external_a"', 'preferred_adapter = "onboard"')
    config.write_text(content)
    with pytest.raises(ConfigError, match="unknown adapter"):
        load_config(config, secrets)


def test_live_web_commands_require_private_token(tmp_path: Path):
    config, secrets = write_config(tmp_path)
    config.write_text(config.read_text().replace("allowed_networks", "allow_live_commands = true\nallowed_networks"))
    with pytest.raises(ConfigError, match="control_token"):
        load_config(config, secrets)
    with secrets.open("a") as stream:
        stream.write('\n[web]\ncontrol_token = "long-private-token"\n')
    assert load_config(config, secrets).web.allow_live_commands
