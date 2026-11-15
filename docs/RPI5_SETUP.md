# Raspberry Pi 5 setup

This guide prepares Raspberry Pi OS Lite 64-bit for the Linux controller. It
does not change Wi-Fi, reboot the Pi, pair/reset a motor, install an MQTT broker,
or issue a movement command.

## 1. Prepare and inspect the host

Use 5 GHz Wi-Fi if it is already available. Do not change networking as part of
this controller install. Connect each FSC-BP119 through a separated USB
extension, initially one at a time, then together:

```sh
uname -a
cat /etc/os-release
lsusb
bluetoothctl list
rfkill list bluetooth
systemctl status bluetooth --no-pager
```

Each external adapter must appear both as a USB device and as a BlueZ
controller. The onboard controller may also appear; it is not an acceptable
silent substitute. Record the external controller addresses and USB physical
paths locally. Do not publish them.

FSC-BP119 units can be supplied in either HCI or HID mode. BlueZ needs USB HCI
mode. The [Feasycom FSC-BP119 datasheet](https://www.feasycom.com/datasheet/fsc-bp119.pdf)
states that HID-to-HCI switching is possible but is a vendor/device procedure.
If a unit appears in `lsusb` but not `bluetoothctl list`, stop and confirm its
mode, exact USB identity, firmware, and current Feasycom instructions. Do not
flash persistent firmware or run a generic CSR conversion command from this
project. Convert only with explicit approval and a recovery plan.

## 2. Install without starting the controller

From the reviewed repository branch:

```sh
./deploy/install.sh
sudo ./deploy/install.sh --apply
```

The first command is a dry run. `--apply` installs OS packages, creates the
unprivileged `graber` account, installs source under `/opt/graber-controller`,
creates its virtual environment from the pinned requirements, and installs the
systemd unit. It deliberately leaves the service stopped and disabled.

Those are generic defaults, not assumptions about a particular host. Override
them when needed by setting `GRABER_INSTALL_DIR`, `GRABER_CONFIG_DIR`,
`GRABER_STATE_DIR`, and/or `GRABER_SERVICE_NAME` consistently for both the dry
run and apply commands. The installer renders those values into the local unit;
no host-specific path or identity is committed to the repository.

The service uses BlueZ over system D-Bus and does not run as root. Verify the
installed user can inspect BlueZ before proceeding:

```sh
sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli host
sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli adapters --show-private
```

The private flag prints controller addresses and USB serials; keep that output
local. The normal command redacts them. If permissions fail, diagnose the D-Bus
or distribution policy specifically rather than granting unrestricted sudo.

## 3. Verify the two adapters

Perform these tests before adding blind credentials:

1. Run `adapters --show-private` with only Adapter A inserted.
2. Repeat with only Adapter B.
3. Insert both, repeat, and confirm two unique controller addresses and two USB
   paths/serials.
4. Reboot only during this planned host-validation session, then confirm the
   stable identities map correctly even if their `hciN` names changed.
5. Temporarily unplug one adapter, confirm a useful missing-adapter diagnostic,
   reconnect it, and confirm it is rediscovered without rebooting.

Advertisement-only motor scanning follows only after both adapters pass. Bleak
may use active scanning, but this diagnostic never connects or writes. Scans
must name an adapter resolved from its stable selector. The live controller
retains its scan objects for connection; the standalone CLI prints and discards
them. Test each adapter independently before dual scanning. Treat scan output
as private inventory. For example:

```sh
sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli \
  scan --controller-address CONTROLLER_ADDRESS --seconds 20
```

The default output replaces blind identity/address with an opaque label. Add
`--show-private` only for local inventory work.

### Identify and build private inventory

The replacement identification CLI filters `RRBZ_*`, keeps the scanned Bleak
device object for optional read-only inspection, and never writes GATT:

```sh
sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.identify_cli \
  --controller-address CONTROLLER_ADDRESS --seconds 20

sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.identify_cli \
  --controller-address CONTROLLER_ADDRESS --seconds 20 --inspect \
  --capture-private /var/lib/graber-controller/living-inventory.json
```

Candidate access-code reads require `--read-candidate-access-code` plus the
private capture path. A readable seven-digit value remains a candidate until a
later supervised movement proves it. The capture is mode `0600` and must not be
shared or committed.

For a browser workflow, copy `deploy/setup-wizard.example.env` to a private
host-only file, replace every example with the local paths, network, and two
external controller addresses, and generate a separate token file. Neither
private file belongs in Git. Scanning, naming, type confirmation, adapter
assignment, and export do not move a blind:

```sh
sudo -u graber sh -c 'umask 077; openssl rand -hex 24 > /var/lib/graber-controller/setup-token'
sudo chmod 0600 /var/lib/graber-controller/setup.env /var/lib/graber-controller/setup-token
sudo -u graber /opt/graber-controller/bin/run-setup-wizard \
  /var/lib/graber-controller/setup.env
```

Open `http://PI_ADDRESS:8090`, enter the token, scan, inspect, name, and assign
devices. The exported `config.toml` and mode-`0600` `secrets.toml` are a draft:
review them before installing under `/etc/graber-controller`.

Do not add `--allow-jog` during read-only identification. That flag is reserved
for Phase C with an operator supervising the room and every other automated
controller path for that blind stopped. A jog uses the saved access
code, moves the bottom rail five raw percentage points, verifies feedback, and
attempts to restore the original position. If delivery is ambiguous it sends
Stop and does not replay the movement.

## 4. Create private configuration

If the setup wizard exported a reviewed draft, install it with explicit owners
and modes:

```sh
sudo install -o root -g graber -m 0640 \
  /var/lib/graber-controller/setup/config.toml \
  /etc/graber-controller/config.toml
sudo install -o graber -g graber -m 0600 \
  /var/lib/graber-controller/setup/secrets.toml \
  /etc/graber-controller/secrets.toml
```

Otherwise copy the installed examples and edit them manually:

```sh
sudo install -o root -g graber -m 0640 \
  /etc/graber-controller/config.toml.example \
  /etc/graber-controller/config.toml
sudo install -o graber -g graber -m 0600 \
  /etc/graber-controller/secrets.toml.example \
  /etc/graber-controller/secrets.toml
sudoedit /etc/graber-controller/config.toml
sudoedit /etc/graber-controller/secrets.toml
```

Populate the topology and secrets files only with verified values from the
deployed private inventory:

- two external adapter selectors and per-adapter concurrency;
- three living-room rail types and preferred adapter assignments in
  `config.toml` (initially two on A and one on B), with real motor identities
  and access codes only in `secrets.toml`;
- the `living_room` group and best-effort default mode;
- existing MQTT broker details and a distinct Pi pilot discovery prefix;
- LAN-only web bind/port and its authentication/control safety settings;
- timeouts, retry limits, 30-second warm hold, battery policy, metrics path.

Never put secrets in command-line arguments or the systemd environment file.
Do not commit either private file. Back them up encrypted or to another trusted
system. A same-card copy is useful only for accidental edits, not microSD
failure:

```sh
sudo install -d -m 0700 /root/graber-controller-config-backup
sudo cp --preserve=mode,ownership \
  /etc/graber-controller/config.toml /etc/graber-controller/secrets.toml \
  /root/graber-controller-config-backup/
```

## 5. Validate, enable, and operate

Configuration validation must not scan, connect, or move a blind:

```sh
sudo -u graber /opt/graber-controller/.venv/bin/python -m graber.main \
  --config /etc/graber-controller/config.toml \
  --secrets /etc/graber-controller/secrets.toml --check-config
sudo ./deploy/install.sh --apply --enable
systemctl status graber-controller --no-pager
journalctl -u graber-controller -n 100 --no-pager
```

The web listener must bind only as configured for the trusted LAN and must not
be port-forwarded. This project does not alter firewall or router rules. MQTT
Discovery uses separate pilot IDs, so verify those entities before enabling any
automation.

Routine operations:

```sh
sudo systemctl restart graber-controller
sudo systemctl stop graber-controller
journalctl -u graber-controller --since today
sudo systemctl disable --now graber-controller
```

To update, check out the reviewed revision, run the installer without
`--enable`, validate configuration, then restart the existing service. Keep the
previous Git revision available for rollback. The service logs to journald;
avoid debug logging during normal operation. Review `journalctl --disk-usage`
and configure system-wide journal limits according to the Pi's other services
rather than having this installer overwrite global retention policy.
