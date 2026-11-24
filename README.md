# Graber BLE controller for Raspberry Pi 5

Local, battery-conscious control of Graber RRBZ motorized shades from Raspberry
Pi OS Lite using Linux BlueZ, Python asyncio, Bleak, one or more independently
managed USB Bluetooth adapters, MQTT Discovery, and a LAN-restricted web
interface.
Adapter count, shade inventory, rooms, and groups are deployment configuration;
the controller does not assume a particular household layout.

This branch is the Raspberry Pi implementation. The former ESP32/ESPHome
implementation is intentionally absent; it remains preserved on the
`esp-controller` branch. The observed wire protocol and relevant research are
retained in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

Features include single-rail and TDBU control, independent per-blind queues,
bounded retries, acknowledged-write no-replay safety, configurable warm links,
stable external-adapter identity/failover, group control, MQTT Discovery entities,
a password-protected LAN UI, and read-only identification/reliability tooling.

No real motor address, access code, password, or Home Assistant identity belongs
in this repository. The installer stores salted password hashes for a permanent
recovery administrator and a separate everyday account; the internal controller
token is never sent to the browser. Automated tests never require Bluetooth
hardware or move a shade.

## Start here

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install --require-hashes --only-binary=:all: -r linux_controller/requirements-dev.lock
make check
```

For installation and operation, see the [Pi setup guide](docs/RPI5_SETUP.md).
To follow a command from the web or Home Assistant through Bluetooth and back,
see the [controller architecture](docs/ARCHITECTURE.md).

## Identification and setup

Install the service, retrieve the private recovery key, and open the web wizard.
The first administrator login creates a separate everyday account. The wizard
then scans, identifies, names, groups, and saves shades before switching the same
address to the control page. See the [Pi setup guide](docs/RPI5_SETUP.md).

Command-line adapter, inventory, and performance tools remain available for
diagnostics. Their normal output redacts motor identities; private captures are
written only to an explicit mode-`0600` path.

## Layout

| Path | What lives here |
| --- | --- |
| [graber/main.py](linux_controller/graber/main.py), [graber/config.py](linux_controller/graber/config.py) | Service startup and configuration |
| [graber/setup/](linux_controller/graber/setup/) | Device identification and the setup wizard |
| [graber/webapp/](linux_controller/graber/webapp/) | Portal, control API, browser scripts and page templates |
| [graber/security/](linux_controller/graber/security/) | Portal credentials and sessions |
| [graber/control/](linux_controller/graber/control/) | Per-blind commands, queues, and group coordination |
| [graber/bluetooth/](linux_controller/graber/bluetooth/) | Radio selection, BlueZ connections, and motor protocol |
| [graber/integrations/](linux_controller/graber/integrations/) | MQTT commands and Home Assistant discovery |
| [graber/diagnostics/](linux_controller/graber/diagnostics/) | Adapter inspection, command metrics, and reports |
| [graber/util/](linux_controller/graber/util/) | Private-file and encoding helpers |
| [linux_controller/tests/](linux_controller/tests/) | Hardware-free controller tests |
| [deploy/](deploy/) | Installer, launch scripts, systemd units, and example HTTPS proxy |

For characteristic
UUIDs, command sequences, and the motor models tested, see the
[observed protocol](docs/PROTOCOL.md).

This is reverse-engineered community software, not an official Graber product.
