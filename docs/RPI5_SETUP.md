# Raspberry Pi 5 setup

## Prerequisites

- Raspberry Pi OS Lite 64-bit on the local network.
- External Bluetooth USB adapters in HCI mode (visible to Linux as Bluetooth
  controllers), connected before setup. The Pi 5 test system used USB adapters
  reporting CSR8510 A10, Bluetooth 4.0, USB ID `0a12:0001`. This identifier
  is shared by multiple products.
- The reviewed Raspberry Pi branch of this repository.

Prefer 5 GHz Wi-Fi and separate the Bluetooth adapters with USB extension
cables. Confirm each adapter appears in both commands:

```sh
lsusb
bluetoothctl list
```

The onboard Bluetooth controller may also appear. Setup discovers powered
external USB controllers. In a paired cold-start test across six shades, the
assigned USB adapters completed 48 of 48 status reads. The Pi 5 onboard radio
completed 45 of 48. Its median time to connect was longer on every shade, and
it had three timeouts. These measurements cover connection and status reads.
Group movement was timed separately with USB adapters.

If an adapter appears only in `lsusb`, it may be in HID mode. Check the
manufacturer's HCI-mode instructions for your exact model and firmware before
attempting a mode switch.

## Install and start

Review the plan, then install and enable the service:

```sh
./deploy/install.sh
sudo ./deploy/install.sh --apply --enable
```

The defaults are:

- App: `/opt/graber-controller`
- Private configuration: `/etc/graber-controller`
- Runtime state: `/var/lib/graber-controller`
- Web app: `127.0.0.1:8080` only, for access through a trusted HTTPS proxy

Set `GRABER_INSTALL_DIR`, `GRABER_CONFIG_DIR`, `GRABER_STATE_DIR`,
`GRABER_SERVICE_NAME`, `GRABER_PORTAL_HOST`, `GRABER_PORTAL_PORT`, or
`GRABER_ALLOWED_NETWORKS` before both installer commands to override them.
The default portal uses Secure cookies and does not listen on the LAN. Configure
the HTTPS proxy below before opening setup from another device. Direct LAN HTTP
requires a non-loopback `GRABER_PORTAL_HOST`,
`GRABER_PORTAL_ALLOW_LAN_HTTP=true`, and
`GRABER_PORTAL_SECURE_COOKIE=false` so login works over HTTP. It exposes login
credentials on the LAN and is intended only for a temporary supervised setup.
Restore the Secure cookie setting before returning to HTTPS.

The installer creates the unprivileged `graber` service account, a hashed
recovery administrator, host settings, and the systemd service. Existing private
files are preserved. When no saved controller configuration exists, the service
starts in setup mode.

## Open setup

The existing private setup key is the permanent `admin` recovery password:

```sh
sudo cat /var/lib/graber-controller/setup-token
```

Open the configured HTTPS proxy URL from the trusted local network, sign in as `admin`,
and enter that recovery password. The first login requires a separate everyday
account with a username and password you choose. Sign in with that account for
normal setup and control. Reserve `admin` for account recovery. Do not
port-forward this page.

In the wizard:

1. Scan for shades.
2. Identify and name each shade.
3. Confirm single-rail or TDBU type.
4. Choose its preferred adapter. Spread shades you often operate together
   across USB adapters. For a six-shade group, start with three shades per
   adapter on two radios, or two per adapter on three radios. The controller
   can start work on each radio independently. Three cold group Opens tested
   on the three-per-radio split completed on all six shades. Start with one
   new connection at a time on each radio (`max_cold_connections = 1`), then
   use the performance report to tune for your installation.
5. Add each shade to the groups you want to operate with one command. For
   example, a dining shade can belong to both “Dining Room” and “Main Floor”.
   Opening Main Floor from the control page sends Open to each member on its
   assigned adapter. With MQTT enabled, Home Assistant also discovers a cover
   for groups with at least two shades. Individual controls remain available.
6. Review the table and save.

Saving writes the private configuration and switches the same web address to
the control page. Use **Edit setup** on the control page to return to the wizard;
the controller stops before setup starts, so both modes never access Bluetooth
at the same time.

Scanning, credential lookup, and saving do not move a shade. **Identify with
jog** moves the selected shade briefly and only after you click it. Stay in the
room and stop every other automated controller for that shade first. Never
reset or re-pair a motor.

## Verify and operate

```sh
systemctl status graber-controller --no-pager
journalctl -u graber-controller -n 100 --no-pager
sudo systemctl restart graber-controller
sudo systemctl stop graber-controller
```

The control page reports observed rail positions, battery values, command
progress, and adapter health. A movement command progresses through bounded
states such as queued, running, awaiting feedback, and a terminal result. A
write acknowledged by the motor is not treated as proof that it moved.

### Continuous performance metrics

When `metrics_path` is configured, the controller continuously records a
bounded JSONL trace of command queuing, adapter admission, discovery, connect,
GATT setup, authentication, writes, feedback, retries, group spread, and
disconnect cleanup. It excludes access codes, protocol payloads, and raw
controller addresses. The active file rotates to one `.1` backup at
`metrics_max_bytes`, limiting microSD use.

Review all retained data at any time:

```sh
/opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli report \
  /var/lib/graber-controller/metrics.jsonl
```

The report highlights per-stage outliers and compares blinds and adapters.
Optional `--recent-days`, `--since`, and `--until` filters narrow the report
without pausing or resetting collection.

### Adapter recovery check

Adapters are resolved from their configured controller address and USB path,
not from the transient `hciN` number. If USB or BlueZ removes a controller, the
service marks it unavailable, bounds affected work, discards stale BlueZ device
objects, and automatically returns it to service when the same stable identity
reappears. It does not fall back to onboard Bluetooth or require a Pi reboot.

To validate recovery without moving a shade:

1. Pause shade automations and confirm no command is active in the control page.
2. Record the external adapters with `bluetoothctl list`. Run the adapter
   diagnostic with your installed count, for example
   `/opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli adapters --expected 2`
   for two USB adapters.
3. Disconnect one USB adapter, leaving the other attached. Confirm its health
   becomes `unavailable` while unrelated shades and the service remain available.
4. Reconnect it to the same USB path. Confirm health returns to `online` without
   restarting the service, then repeat for each remaining adapter.

Health includes connection attempts/successes, failures, disconnects,
recoveries, and the last unavailable/recovered times. This validation performs
no shade command; do not use Identify or a control button during it.

### Accounts and recovery

The everyday account can change its own password. If it is inaccessible, sign
in as `admin` with the recovery password and reset the everyday account. The
recovery password always remains valid unless it is explicitly rotated with the
offline tool.

Stop the service before an offline recovery-password rotation. The tool prompts
interactively so the new password never appears in shell history:

```sh
sudo systemctl stop graber-controller
sudo -u graber env PYTHONPATH=/opt/graber-controller \
  /opt/graber-controller/.venv/bin/python -m graber.auth set-recovery-password \
  --file /var/lib/graber-controller/portal/auth.json
sudo systemctl start graber-controller
```

To remove an inaccessible everyday account from the console instead, use
the offline reset command, then sign in as `admin` to create its replacement:

```sh
sudo systemctl stop graber-controller
sudo -u graber env PYTHONPATH=/opt/graber-controller \
  /opt/graber-controller/.venv/bin/python -m graber.auth reset-owner \
  --file /var/lib/graber-controller/portal/auth.json
sudo systemctl start graber-controller
```

### HTTPS with Caddy

Portal passwords and session cookies are not confidential over plain HTTP. For
long-term use, bind the controller to loopback and place an HTTPS reverse proxy
in front of it. `deploy/Caddyfile.example` provides an opt-in Caddy example;
the installer does not install Caddy, change DNS, or modify network settings.
See Caddy's [local HTTPS documentation](https://caddyserver.com/docs/automatic-https#local-https)
for its internal certificate authority and client trust model.

The installer defaults to a loopback portal with Secure cookies. When Caddy
runs on the Pi, trust the loopback proxy and allow the addresses of devices
that should reach the portal. For a new install, substitute your client LAN's
subnet for the example below.

```sh
export GRABER_PORTAL_TRUSTED_PROXIES=127.0.0.0/8,::1/128
export GRABER_ALLOWED_NETWORKS=127.0.0.0/8,::1/128,192.168.1.0/24
sudo -E ./deploy/install.sh --apply
```

For an existing install, edit the private `/etc/graber-controller/controller.env`.
Set `GRABER_PORTAL_HOST=127.0.0.1`, `GRABER_PORTAL_SECURE_COOKIE=true`, and
`GRABER_PORTAL_TRUSTED_PROXIES=127.0.0.0/8,::1/128`. Include the client
subnet in `GRABER_PORTAL_ALLOWED_NETWORKS`. The portal then checks the client
address forwarded by the loopback proxy. Restart the service after the HTTPS
proxy is ready.

Choose a private hostname, configure it in local DNS, install Caddy separately,
and adapt the example Caddyfile. It rejects clients outside private address
ranges. Caddy's internal CA is not automatically trusted
by other devices; install its root certificate on every client that uses the
portal. Keep the Pi portal on loopback and do not expose Caddy to the internet.
Back up Caddy's data directory with the other encrypted controller backups. It
contains the private CA key; never publish or copy that key to client devices.
Clients need only the public `root.crt` certificate.

## Home Assistant

Home Assistant integration uses an existing MQTT broker and MQTT Discovery.
The wizard does not install a broker. Before enabling MQTT, create a dedicated
non-administrator broker account, choose a stable controller device ID and
topic prefix, and put the credentials only in private `secrets.toml`.

Enable MQTT in `config.toml`, restart the controller, and verify the discovered
cover, battery, diagnostic, availability, and adapter-health entities before
changing dashboards or automations. Movement command topics are never retained.
Use broker TLS when it is available; see [Security and privacy](SECURITY.md).

If replacing an existing controller, disable its shade automations before
issuing the first movement through the Pi. Verify a single supervised shade
before enabling group automations.

### Alexa through Home Assistant Cloud

MQTT Discovery classifies the native cover entities as shades. Expose those
covers directly to Alexa for Open, Close, Stop, and supported position commands;
do not create separate Open/Close/Stop scripts. TDBU shades also receive a
full-shade cover for verified full Open, full Close, and Stop. Their bottom and
top rail covers remain available for explicit rail positioning, while the
full-shade cover intentionally has no arbitrary position control.

In Home Assistant's voice-assistant exposure settings, expose only the native
shade and group covers you want Alexa to discover. Hide legacy Alexa movement
scripts and old controller entities, then run Alexa device discovery. Retain a
narrowly named Favorite script only when that voice action is wanted, because
Favorite is not a native cover capability.

## Update or roll back

To update, check out the reviewed revision and rerun:

```sh
sudo ./deploy/install.sh --apply
sudo systemctl restart graber-controller
```

The installer preserves private configuration and portal authentication. It
installs application dependencies from the pinned, hash-verified
`linux_controller/requirements-rpi5.lock`.

To roll back, stop and disable the service, confirm no BLE sessions remain,
then re-enable the previous automation path:

```sh
sudo systemctl disable --now graber-controller
```

Rollback does not require re-pairing shades. Keep an encrypted backup of
`config.toml`, `secrets.toml`, `portal/auth.json`, and the private `setup-token`
outside the microSD card. The setup token is the recovery password: anyone with
it has administrative portal access. The session cookie is memory-only and is
not part of a backup.
