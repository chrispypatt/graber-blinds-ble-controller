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
in this repository. The installer stores only a salted password hash for portal
login; the internal controller token is never sent to the browser. Automated
tests never require Bluetooth hardware or move a shade.

## Start here

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install -r linux_controller/requirements-dev.txt
make test
```

Then read the [architecture audit](docs/ARCHITECTURE_AUDIT.md),
[Pi setup guide](docs/RPI5_SETUP.md), and
[supervised migration/rollback guide](docs/RPI5_MIGRATION.md).

## Identification and setup

Install the service, retrieve the one-time initial password, and open the web
wizard. It scans, identifies, names, groups, and saves shades, then switches the
same address to the control page. See the [Pi setup guide](docs/RPI5_SETUP.md).

Command-line adapter, inventory, and performance tools remain available for
diagnostics. Their normal output redacts motor identities; private captures are
written only to an explicit mode-`0600` path.

## Layout

```text
linux_controller/graber/       controller, protocol, MQTT, web, setup tools
linux_controller/tests/        hardware-free Phase A tests
deploy/                         repeatable installer and systemd service
docs/PROTOCOL.md               observed protocol and uncertainty boundaries
docs/RPI5_SETUP.md             Pi/adapters/install/identification
docs/RPI5_MIGRATION.md         pilot, acceptance, cutover, rollback
```

This is reverse-engineered community software, not an official Graber product.
