# Graber BLE controllers

Community-maintained, local control for Graber RRBZ motorized shades. This
repository has two independent implementations. Choose the branch that matches
the controller hardware you want to run:

| Implementation | Best fit | Highlights | Important limits |
| --- | --- | --- | --- |
| [Raspberry Pi / Linux](../../tree/rpi5-linux-controller) | A central Raspberry Pi running Raspberry Pi OS | BlueZ and Bleak; one or more explicitly identified USB Bluetooth adapters; browser setup and control; MQTT Discovery for Home Assistant; per-shade queues, bounded retries, adapter failover, metrics, and best-effort or synchronized groups | Requires a maintained Linux service, compatible HCI adapters, and an existing MQTT broker for Home Assistant. Adapter capacity, RF range, motor firmware behavior, and cold-command latency must be measured on the installed hardware. |
| [ESP32 / ESPHome](../../tree/esp-controller) | Small distributed ESPHome controllers placed near the shades | Native ESPHome/Home Assistant workflow; reusable packages; single-rail and TDBU control; notifications, battery reporting, Favorite, presets, and confirmed fixed-step Stack Up/Down | Limited to three configured BLE clients per controller. ESP32 Wi-Fi/Bluetooth coexistence and cold connections can add latency; bulk commands use an all-or-nothing readiness barrier, and recovery may restart the individual controller. |

## Shared protocol scope

Both branches preserve the same observed Graber authentication and command
sequences. The protocol is reverse-engineered rather than an official Graber
specification, so behavior can vary with motor hardware and firmware.

Both implementations support the tested single-rail and top-down/bottom-up
(TDBU) operations while preserving existing remotes and app associations.
Arbitrary simultaneous absolute positions for both TDBU rails are not claimed:
independent rail targets and the confirmed shared relative Stack Up/Down
operation are distinct behaviors.

Neither branch should contain real motor addresses, access codes, MQTT
credentials, or household-specific configuration. Start with one supervised
shade, keep physical Stop controls available, and do not reset or re-pair a
motor during discovery.

Detailed setup, security guidance, protocol evidence, tests, and rollback
instructions live with each implementation on its branch.

This is reverse-engineered community software, not an official Graber product.
