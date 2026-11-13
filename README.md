# Graber BLE blinds for ESPHome

Local ESPHome control for tested Graber RRBZ motorized shades, including
single-rail and top-down/bottom-up (TDBU) covers, position notifications,
battery diagnostics, favorite/full-open/full-close commands, and coordinated
bulk actions. TDBU devices also expose fixed five-point Stack Up and Stack Down
controls that move both rails together while preserving their spacing.

This is reverse-engineered community software, not an official Graber product.
It has been tested primarily with P3 hardware and firmware `V01.10_250705`.
Preserve existing remotes: discovery and normal BLE control do not require
resetting, excluding, or re-pairing a shade.

## Architecture

Each ESP32 owns at most three nearby motors. All controllers run the same
dispatcher and package templates; a private inventory selects the motors for a
particular controller. Cold connections are staged serially for reliability.
Successful links remain warm for 30 seconds for responsive follow-up and Stop
commands, then disconnect to protect shade battery life.
If the BLE stack repeatedly fails to open, discover, or complete a GATT
transaction, the dispatcher waits for cleanup and automatically reboots that
ESP32. A diagnostic `Restart Controller` button is also exposed for manual
recovery; it is not required for normal watchdog recovery.

The project has three reusable layers:

- `components/graber_dispatcher/` contains the C++ BLE transaction engine.
- `packages/` contains reusable ESPHome YAML for common and single/TDBU blinds.
- A generated controller YAML supplies only device identity, blind inventory,
  secret references, and optional entity names.

This separation is deployment-independent. The files can be used from an
ESPHome dashboard, a local CLI, CI, or another build system.

## 1. Identify motors

Flash [`graber-identify.yaml`](graber-identify.yaml) to an
ESP32-WROOM-32. Its logs list `RRBZ_*` names, real BLE MAC addresses, RSSI, and
advertised rail values. Move one shade at a time and correlate the changing
values. Do not reset any shade or remote.

Candidate access codes can be read without writing from a Mac or Linux system
with a Bluetooth adapter:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-discovery.txt
python tools/graber_ble_inventory.py --scan-seconds 20
python tools/graber_ble_inventory.py --name RRBZ_EXAMPLE --read-candidate-codes
```

macOS normally reports a CoreBluetooth UUID instead of the BLE MAC; use the
ESP32 identifier for the address. A readable seven-digit value is only a
candidate until a safe movement test succeeds. Some already-used motors return
a one-byte command/status value instead.

## 2. Store secrets

Copy `examples/secrets.yaml.example` entries into the secrets mechanism used by
your ESPHome installation. Use distinct secret names for every motor. Never
put resolved codes or MAC addresses in the inventory JSON or generated YAML.
The generator emits `!secret` references only. See
[security guidance](docs/SECURITY.md).

## 3. Define controllers

Copy `examples/controllers.example.json` to an ignored path such as
`private/controllers.json`. For each controller, list one to three nearby
motors, their friendly names, secret names, and whether each is TDBU. A group
may contain motors owned by that same controller. Add the group's optional
`tdbu_blinds` list to generate synchronized Stack Up/Down buttons for its TDBU
members; single-rail blinds are excluded.

The default `package_dir` and `component_path` assume generated controller YAML
is written in this repository root. If your ESPHome configuration lives
elsewhere, set those paths per controller, for example:

```json
{
  "package_dir": "../graber-controller/packages",
  "component_path": "../graber-controller/components"
}
```

Optional `cover_name` and `position_name` fields preserve existing Home
Assistant entity names during migration. Preserve the existing `name` of an
already-flashed ESPHome controller so OTA addressing and its HA device identity
do not change.

Generate the minimal device YAML files:

```sh
python3 tools/generate_controllers.py private/controllers.json --output-dir .
```

The generated wrappers all use `packages/graber-common.yaml` and
`packages/graber-blind.yaml`; behavior changes belong in those shared files or
the dispatcher, not in individual controller YAML.

## 4. Validate and install

```sh
esphome config generated-controller.yaml
esphome compile generated-controller.yaml
esphome upload generated-controller.yaml --device CONTROLLER_IP
```

Begin with one nearby blind. Confirm passive positions, then make a small move,
test Stop, and confirm the physical remote and official app still work. Add
other motors only after that succeeds.

## Behavior and caveats

- ESP32 Wi-Fi and BLE share radio resources. One controller across multiple
  floors will be slower and less reliable than distributed controllers.
- Bulk operations use an all-or-nothing readiness barrier: if one blind cannot
  connect, already-ready blinds are not dispatched as a partial group.
- A cold command can take seconds because the motor advertises at a low duty
  cycle. Warm follow-ups are much faster.
- TDBU top-rail cover values are normalized for Home Assistant; raw motor values
  run in the opposite direction.
- TDBU Stack Up/Down controls reproduce the app's confirmed shared relative
  five-point step. They are not arbitrary paired positioning; each command
  moves both rails together, subject to each rail's travel limit. The independent
  rail sliders remain subject to motor behavior, and paired absolute targets
  are not considered verified.
- Protocol details and known uncertainties are documented in
  [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Development

The custom component lives in `components/graber_dispatcher`. Host-side tests
exercise connection staging, retries, command writes, notifications, battery
reads, bulk barriers, and warm-link behavior. Validate both tests and at least
one single/TDBU example before releasing changes.
