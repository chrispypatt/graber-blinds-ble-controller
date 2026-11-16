# Observed Graber BLE protocol

This project is based on observations from RRBZ/P3 motors running firmware
`V01.10_250705`. It is not an official Graber specification. Other hardware or
firmware may differ.

The historical C++ dispatcher referenced below is preserved on the
`esp-controller` branch; it is not part of this Raspberry Pi-only branch. Its
tested byte ordering and timing remain protocol research inputs, not Linux
architecture constraints.

## Known characteristics

| Purpose | Service | Characteristic |
| --- | --- | --- |
| Access code / direct command | `DE8A5AAC-A99B-C315-0C80-60B63A869124` | `5B026510-4088-C297-46D8-BE6CB63A8691` |
| Bottom target | same | `3EAD2B49-ABBC-4602-AC80-54D4B5687BAC` |
| Bottom notification | same | `EBC1ED9C-E9E1-4DB3-A8F5-FF249944B442` |
| Preset index | same | `E3FC1EBF-E615-44F2-B1DD-B6B95266A453` |
| Top target | `375B4079-D234-4631-9592-65C87174410C` | `A506FBB1-9777-4E43-AA13-67D1BAB6C0A1` |
| Execute / top command | same | `BB27521D-2FD4-4187-AF9F-5D0780238D7E` |
| Top notification | same | `900EEAEB-0F27-4808-809C-4026A8FD7D5A` |
| Battery | standard Battery Service | `2A19` |

The historical dispatcher authenticated, discovered and cached handles,
subscribed to rail notifications, sent one command at a time, and kept a
successful connection warm briefly so Stop or a follow-up command did not pay
another cold-connect penalty. Its ESP32 cold-link serialization was a platform
workaround, not a Linux architecture requirement.

## Commands observed in the official app

- Bottom position: write target byte `0..100`, then `0x14` to the access/direct
  command characteristic.
- Top position: write target byte `0..100`, then `0x14` to Execute.
- Stop: `0x10` to both access/direct command and Execute.
- Favorite: `0x16` to the access/direct command characteristic. The preserved
  reference dispatcher used this sequence for both single-rail and TDBU shades.
- Fully open: `0x12` to access/direct command on the tested TDBU firmware.
- Fully closed: `0x12` to Execute, then `0x13` to access/direct command on the
  tested TDBU firmware (about 45–55 ms apart).
- Presets 5 and custom indices: write the preset index, then `0x15` to the
  access/direct command characteristic.

### TDBU shared stack movement

Repeated, action-correlated captures from a TDBU motor running firmware
`V01.10_250705` show shared relative movement that preserves the rails'
separation. This is distinct from setting two independent absolute positions:

- Stack Up: write `0x05` to the bottom target, write `0x05` to the top target,
  then write `0x18` to Execute.
- Stack Down: write `0x05` to both targets, then write `0x19` to Execute.
- Captured target-write spacing was about 50–90 ms; Execute followed the second
  target by about 60–100 ms. The dispatcher uses 70 ms between writes.
- In the unequal-position capture, Up moved bottom 26→30 and top 71→75; Down
  moved bottom 30→25 and top 75→70. At a travel limit, the rail with remaining
  travel can continue while its partner stays at the limit.
- `0x05` is the only confirmed shared-step size. Other target values are not
  exposed as adjustable step sizes.
- An earlier private Android HCI capture also contains paired target writes
  `0x37` before Execute `0x18` and `0x0f` before
  Execute `0x19`, followed by rail movement. The action labels and exact intent
  are not sufficiently correlated in that older capture to treat these as
  validated variable step sizes. They remain a lead for future capture work.

The dispatcher exposes these as Stack Up / Stack Down buttons on TDBU devices.
They use the normal authenticated connection, serialized command queue, and
fresh samples from both rail feedback streams. Completion requires the
positions to remain stable for at least 600 ms; missing feedback ends with an
explicit timeout. Stop continues to send `0x10` to auth and Execute. The Linux
controller exposes group stack actions only when every configured member is
known to be TDBU; its best-effort and synchronized group policies are
independent of the protocol bytes described here.

This does not establish that two arbitrary absolute rail targets can coexist.
The dispatcher rejects mode 8 (sequential paired absolute targets): the tested
sequence reset the partner rail to an endpoint. A different sequence would need
to demonstrate that both positions persist before paired absolute positioning
can be reconsidered. The independent `GraberBlinds_ESP32` research also reports
partner-rail defaulting for individual `0x14` operations. Our captures show
individual app slider actions and shared `0x18`/`0x19` movement, but do not
establish persistent coexistence of two arbitrary positions.

Favorite (`0x16`) is validated by official-app captures and the preserved
reference dispatcher, which used the same sequence for single-rail and TDBU
shades.

Rail direction in Home Assistant is normalized by the package. The raw TDBU
top value is inverted when exposed as a cover position.

## Limits

- The historical ESP32 build was intentionally limited to three BLE clients;
  this is not a Linux or protocol limit. Linux adapter concurrency remains a
  deployment measurement, not a guaranteed capacity.
- Advertised position bytes are useful but are validated against GATT
  notifications during active commands.
- Battery readings can be wrong on the motor itself; a value also reported as
  zero by the official app is not corrected by this project.
- Range and cold connection latency are physical constraints. Adapter
  assignment and connection concurrency must be validated on installed
  hardware.
