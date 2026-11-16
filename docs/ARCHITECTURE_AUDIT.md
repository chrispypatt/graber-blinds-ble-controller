# Raspberry Pi migration architecture audit

This audit separates confirmed Graber behavior from ESP32 implementation
choices. The Linux design reuses the former and is free to replace the latter.

## Directly reusable protocol behavior

`docs/PROTOCOL.md` and the historical C++ dispatcher preserved on the
`esp-controller` branch are the evidence-backed references. The Linux
implementation retains:

- The two proprietary services, their characteristic UUIDs, and the standard
  Battery Level characteristic.
- Notification subscription before authentication, followed by a seven-byte,
  zero-padded access-code write. An acknowledged write is the current evidence
  that setup succeeded; the access code must never enter logs.
- Bottom and top absolute target writes followed by the rail-specific `0x14`
  execute write. Top values are inverted only at the Home Assistant/UI boundary.
- Stop writes to both direct-command and Execute when both exist.
- The captured TDBU full-open, full-close, preset, and fixed five-point
  Stack Up/Down sequences, including their inter-write delays.
- Fresh GATT feedback for command completion. A successful ATT/GATT write is
  delivery evidence, not proof that a motor moved or reached its target.
- The explicit limitation that arbitrary paired absolute TDBU targets are not
  verified. Favorite uses the reference dispatcher's shared single-rail/TDBU
  sequence. Experimental
  captures must not be exposed as supported commands.
- Advertisement manufacturer bytes 2 and 3 as candidate bottom/top positions
  only when present and in `0..100`; active operations still require GATT
  feedback.

The 30-second warm connection, bounded queue, retry cleanup, six-hour battery
poll, and group barrier are valuable baselines, but they are policies rather
than wire-protocol requirements and remain configurable on Linux.

## ESP32-specific portions not carried over as architecture constraints

- ESPHome component registration, YAML packages, template covers/sensors, and
  generated per-controller firmware wrappers.
- The global single cold-connect staging lock and three-client ceiling. They
  reflect the tested ESP32/BlueDroid environment. Linux instead applies
  configurable limits per USB controller and starts conservatively until Pi
  measurements justify more parallel setup.
- GATT handle caching by numeric handle and the ESPHome connection-type modes.
  BlueZ owns its service cache; the Linux transport uses Bleak service objects
  and falls back cleanly after a BlueZ/GATT failure.
- Rebooting the entire controller after a wedged BLE stack. Linux isolates
  failures per blind/adapter, re-resolves USB adapters, and restarts only the
  service as a last bounded process-level recovery. It never uses Pi reboot as
  routine recovery.
- The existing all-or-nothing bulk barrier as the only group policy. Linux uses
  best effort by default and offers a bounded synchronized preparation mode.

## Raspberry Pi identification replacement

The old ESP identification firmware and inventory helper are not present on
this Pi-only branch. `graber.identify_cli` replaces them with adapter-specific
BlueZ scanning, redacted normal output, read-only firmware/battery inspection,
and explicit mode-`0600` private capture. `graber.identify_web` adds a local
setup wizard for naming, rail-type confirmation, adapter assignment, and split
config/secrets export. Jogging is a separate token-gated live mode and is never
enabled by ordinary identification.

## Deployment-specific information still required

Nothing below is fabricated or committed. The private Pi configuration needs:

- Each selected pilot motor's real BLE identity, seven-digit access code, rail
  type, tested firmware, and preferred/fallback adapter assignments.
- Each external controller address plus USB serial or physical port path,
  observed after individual and combined reboot tests.
- MQTT broker host/port, TLS/authentication requirements, and confirmation that
  a broker already exists. This project does not install a broker.
- Current legacy Home Assistant device and entity IDs, friendly names,
  dashboards, and automations that refer to them.
- The Pi's intended LAN address/hostname and the desired LAN-only web bind and
  authentication secret.

Private files belong outside the checkout: non-secret topology in
`/etc/graber-controller/config.toml` (`0640`, `root:graber`) and motor/MQTT
credentials in `secrets.toml` (`0600`, `graber:graber`). Motor addresses and
access codes must not be emitted in MQTT topics, metrics, or logs.

## Home Assistant pilot treatment

The Pi publishes retained MQTT Discovery documents under a separate migration
namespace and stable unique IDs. State is non-optimistic; command topics
are not retained. Existing legacy entities are not renamed, overwritten, or
deleted. During the pilot, automations must have only one active controller for
each blind. After acceptance testing, cut over automation targets deliberately,
preserve friendly names/dashboard placement where practical, and disable (do
not erase) the old ESP automation path. Physical remotes and the Graber app are
never re-paired or modified.

## BlueZ/Bleak constraints to validate on the Pi

- Bleak's Linux backend communicates with BlueZ over D-Bus. A specific adapter
  can be chosen, but its `hciN` name may change; the service resolves that name
  from the configured controller address plus USB identity at runtime. See the
  [Bleak BlueZ arguments](https://bleak.readthedocs.io/en/stable/api/args.html).
- BlueZ discovery is shared between clients and merges discovery filters. A
  process must still filter every received update, and stopping one discovery
  session does not necessarily stop another client's scan. See the
  [BlueZ Adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/).
- BlueZ represents a remote device under a particular adapter object. Preserve
  the scanned Bleak device object for the subsequent connection, and never open
  the same blind through two adapters concurrently.
- `ServicesResolved` and physical connection are separate stages; measure link,
  service setup, authentication, write acknowledgement, and movement feedback
  separately. See the [BlueZ Device API](https://bluez.readthedocs.io/en/latest/device-api/).
- Adapter power is not guaranteed to persist across unplug/restart. The manager
  must report a missing or unpowered preferred controller and use configured
  fallback policy rather than silently selecting onboard Bluetooth.
- RSSI, advertisement interval, BlueZ cache state, USB topology, Wi-Fi/Bluetooth
  coexistence, and motor firmware can all affect results. Capacity and parallel
  connection setup must be measured on the installed system.
