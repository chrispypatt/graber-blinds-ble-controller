# Home Assistant UI integration

This project provides the ESPHome entities and BLE protocol components. A
Home Assistant dashboard is deployment-specific and should remain outside the
controller firmware repository's private inventory. Keep resolved MAC addresses,
access tokens, local entity mappings, and captured diagnostics in the
deployment's private files.

## Dual-cover card

For a tested top-down/bottom-up shade, use a dual-cover custom card with one
Home Assistant cover for each rail:

```yaml
type: custom:dual-cover-card
title: Example shade
top_entity: cover.example_top
bottom_entity: cover.example_bottom
stack_up_entity: button.example_stack_up
stack_down_entity: button.example_stack_down
invert_top: true
size: compact
compact_orientation: horizontal
show_position: false
show_buttons: false
open_popup: true
```

The exact entity IDs are generated from the private controller inventory. Do
not put real device identifiers or access codes in a published dashboard
example.

Set `invert_top: true` when the motor's raw top-rail percentage runs in the
opposite direction from Home Assistant's cover percentage. The card applies
the conversion while rendering and while writing an absolute top-rail target.
The bottom rail normally remains uninverted. Confirm the direction on the
specific motor before deploying this setting.

## Combined fabric dragging

Dragging the fabric area of the card is a relative stack operation. It does
not send two absolute rail targets, because the tested motor firmware couples
sequential absolute rail writes and may move the other rail to a default.

The card converts the drag distance into approximately 5-percent steps and
presses the configured `stack_up_entity` or `stack_down_entity` once per
step. Repeated steps are separated by a short delay so the controller can
process each BLE transaction. The controller's stack commands correspond to
the app-observed shared movement and preserve rail separation subject to the
motor's travel limits.

The individual top and bottom handles remain absolute controls. They call
Home Assistant's `cover.set_cover_position` for one rail. Independent
arbitrary final positions for both rails are not assumed to work unless the
motor firmware and a notification trace prove that behavior.

The separate Stack Up/Stack Down buttons can be omitted from the dashboard;
the entities still need to exist so the card can invoke them.

## Dashboard mapping

A deployment may keep a private mapping from rooms to generated entities and
choose its own card layout. A typical mapping contains:

- TDBU pairs: top cover, bottom cover, stack-up button, stack-down button.
- Single-rail shades: one ordinary cover.
- Optional room actions: open, close, favorite, and stop scripts.
- Optional diagnostics: battery and controller RSSI.

Keep this mapping in a private Home Assistant deployment directory or
secrets/configuration area. Do not commit resolved MAC addresses, seven-digit
motor access codes, private captures, or household room/device identifiers to
a public repository.

## Resource and browser cache handling

Use one stable Lovelace resource as a bootstrap and have it import the changing
implementation with a cache-busting query:

```js
import(`/local/graber-dashboard-impl.js?v=${Date.now()}`);
```

Edit the implementation file during routine dashboard work, then perform a
normal full-page browser reload. Do not create a new versioned Lovelace
resource for every edit and do not rely on editing Home Assistant's
`.storage/lovelace_resources` file directly: the running in-memory resource
registry may not change.

If the stable bootstrap filename or its registered URL changes, update the
live Lovelace resource through Home Assistant's WebSocket
`lovelace/resources` API, then query the registry to verify the result. Use
the Supervisor proxy only from an authorized deployment environment and never
print or persist `SUPERVISOR_TOKEN`.

## Operational limitations

- A Home Assistant service-call acknowledgement means the command was
  accepted, not that the shade has finished moving.
- Cold BLE connection setup can take seconds. Controller placement and
  distributing nearby motors matter more than making one ESP32 handle many
  distant shades.
- Bulk operations may be delayed by serialized BLE connection setup.
- Active GATT notifications are useful during movement; passive advertising
  can provide useful idle position data. Neither path should be treated as
  infallible when a motor is unavailable.
- TDBU raw top values may need inversion at the HA cover boundary.
- Only expose stack controls for motors configured as TDBU. Single-rail motors
  should use ordinary cover controls.

