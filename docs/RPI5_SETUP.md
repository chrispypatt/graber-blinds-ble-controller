# Raspberry Pi 5 setup

## Prerequisites

- Raspberry Pi OS Lite 64-bit on the local network.
- External Bluetooth USB adapters in HCI mode and connected before setup.
- The reviewed Raspberry Pi branch of this repository.

Prefer 5 GHz Wi-Fi and separate the Bluetooth adapters with USB extension
cables. Confirm each adapter appears in both commands:

```sh
lsusb
bluetoothctl list
```

The onboard Bluetooth controller may also appear. The installer selects powered
external USB controllers for setup.

If an adapter appears only in `lsusb`, it may be in HID mode. Confirm the exact
Feasycom procedure for that device and firmware. Do not flash firmware or run a
generic HID-to-HCI conversion command without a recovery plan.

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
- Web app: `http://PI_ADDRESS:8080` before optional HTTPS setup

Set `GRABER_INSTALL_DIR`, `GRABER_CONFIG_DIR`, `GRABER_STATE_DIR`,
`GRABER_SERVICE_NAME`, `GRABER_PORTAL_HOST`, `GRABER_PORTAL_PORT`, or
`GRABER_ALLOWED_NETWORKS` before both installer commands to override them.

The installer creates the unprivileged `graber` service account, a hashed portal
login, host settings, and the systemd service. Existing private files are
preserved. When no saved controller configuration exists, the service starts in
setup mode.

## Open setup

The existing private setup key is the one-time initial password:

```sh
sudo cat /var/lib/graber-controller/setup-token
```

Open `http://PI_ADDRESS:8080` from the trusted local network, sign in as `admin`,
and enter that initial password. The portal immediately requires a new password
and stores only its salted hash. Do not port-forward this page.

In the wizard:

1. Scan for shades.
2. Identify and name each shade.
3. Confirm single-rail or TDBU type.
4. Choose its preferred adapter.
5. Add room or whole-home groups.
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

### Change or reset the portal password

The portal provides an authenticated password-change screen. For an offline
reset, stop the service and run the password tool interactively so the password
never appears in shell history:

```sh
sudo systemctl stop graber-controller
sudo -u graber env PYTHONPATH=/opt/graber-controller \
  /opt/graber-controller/.venv/bin/python -m graber.auth set-password \
  --file /var/lib/graber-controller/portal/auth.json --username admin
sudo systemctl start graber-controller
```

### HTTPS with Caddy

Portal passwords and session cookies are not confidential over plain HTTP. For
long-term use, bind the controller to loopback and place an HTTPS reverse proxy
in front of it. `deploy/Caddyfile.example` provides an opt-in Caddy example;
the installer does not install Caddy, change DNS, or modify network settings.
See Caddy's [local HTTPS documentation](https://caddyserver.com/docs/automatic-https#local-https)
for its internal certificate authority and client trust model.

For a new install, set these variables before running the installer:

```sh
export GRABER_PORTAL_HOST=127.0.0.1
export GRABER_PORTAL_SECURE_COOKIE=true
export GRABER_PORTAL_TRUSTED_PROXIES=127.0.0.0/8,::1/128
sudo -E ./deploy/install.sh --apply
```

For an existing install, set `GRABER_PORTAL_HOST=127.0.0.1`,
`GRABER_PORTAL_SECURE_COOKIE=true`, and
`GRABER_PORTAL_TRUSTED_PROXIES=127.0.0.0/8,::1/128` in the private
`/etc/graber-controller/controller.env`. This trusts forwarded client addresses
only from the loopback proxy. Restart the service after the HTTPS proxy is ready.

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

Never leave two automated controller paths active for the same shade. Follow
the [cutover and rollback procedure](RPI5_MIGRATION.md#home-assistant-cutover)
when replacing another controller.

## Update or roll back

To update, check out the reviewed revision and rerun:

```sh
sudo ./deploy/install.sh --apply
sudo systemctl restart graber-controller
```

The installer preserves private configuration and portal authentication.

To roll back, stop and disable the service, confirm no BLE sessions remain,
then re-enable the previous automation path:

```sh
sudo systemctl disable --now graber-controller
```

Rollback does not require re-pairing shades. Keep an encrypted backup of
`config.toml`, `secrets.toml`, and `portal/auth.json` outside the microSD card.
The session cookie is memory-only and is not part of a backup.
