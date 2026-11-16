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
- Web app: `http://PI_ADDRESS:8080`

Set `GRABER_INSTALL_DIR`, `GRABER_CONFIG_DIR`, `GRABER_STATE_DIR`,
`GRABER_SERVICE_NAME`, `GRABER_PORTAL_HOST`, `GRABER_PORTAL_PORT`, or
`GRABER_ALLOWED_NETWORKS` before both installer commands to override them.

The installer creates the unprivileged `graber` service account, a private
setup key, host settings, and the systemd service. Existing private files are
preserved. When no saved controller configuration exists, the service starts in
setup mode.

## Open setup

Read the setup key on the Pi:

```sh
sudo cat /var/lib/graber-controller/setup-token
```

Open `http://PI_ADDRESS:8080` from the trusted local network and enter the key.
Do not port-forward this page.

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

The installer preserves the private configuration and setup key.

To roll back, stop and disable the service, confirm no BLE sessions remain,
then re-enable the previous automation path:

```sh
sudo systemctl disable --now graber-controller
```

Rollback does not require re-pairing shades. Keep an encrypted backup of
`config.toml` and `secrets.toml` outside the microSD card.
