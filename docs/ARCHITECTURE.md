# Linux controller architecture

The portal runs one local service at a time. In setup mode, the wizard discovers
USB radios and shades and saves their configuration. In control mode, the web
API and the optional MQTT bridge share the same command dispatcher for each
blind. Switching modes stops the previous service before starting its replacement.

## Follow a group Open

Consider a group of six shades spread across three USB adapters.

1. The controller receives one group command from the web API or MQTT. In the
   default `best_effort` mode, it submits Open to all six member dispatchers.
   Each member gets a command ID derived from the group command ID. A slow
   member can finish after the others.
2. Each dispatcher serializes commands for its own blind. A blind can reuse
   its connected session. Otherwise, it tries its preferred adapter and the
   configured fallbacks. The adapter manager resolves logical adapter IDs to
   BlueZ's current device names using controller address and USB path.
3. Each adapter limits its own active sessions and concurrent cold connections.
   With one cold slot per adapter, three radios can be connecting at once.
   A cold slot covers discovery and connection. Once BlueZ connects, the next
   shade on that radio can start connecting while the previous shade
   subscribes to rail notifications and authenticates.
4. The blind sends the motor's GATT writes in protocol order. An acknowledgement
   confirms delivery. The controller uses fresh GATT feedback to assess the
   result. If feedback is inconclusive after an acknowledged movement write,
   the controller reports that outcome and suppresses replay.
5. The controller records each blind's result and summarizes the group.
   The control page can show individual failures. With MQTT enabled, Home
   Assistant receives the observed state and diagnostics.

For a group configured as `synchronized`, the controller first prepares the
members' sessions within `prepare_timeout_s`. When a member fails preparation,
`partial_failure` selects whether to release the ready members or abort the
prepared movements. Stop bypasses this barrier. A conflicting movement for a
busy blind triggers a Stop, and the dispatcher waits for confirmation before
releasing its replacement.

## Position, battery, and session lifetime

The advertisement monitor listens on the available radios. Supported motor
advertisements can update the last observed rail positions. During a command,
GATT rail notifications and explicit reads provide feedback from the connected
motor. For top-down/bottom-up shades, the MQTT and web interfaces translate
the top rail's raw position to the user-facing direction.

Battery comes from reading the GATT Battery Level characteristic. After a
successful movement, the controller can take a battery reading on the same
session. A periodic status refresh also reads rail positions and battery. A
connection opened solely for that refresh closes when the read finishes.
Movement sessions have a configurable idle hold so a follow-up command can
reuse the connection.

## MQTT state delivery

The MQTT publisher sends changed state and keeps the newest pending value for
each topic. Cover positions, battery, and other replaceable state use QoS 0 so
they can reach the broker while QoS 1 discovery packets await acknowledgement.
Home Assistant keeps the same battery entity ID and subscribes to a dedicated
retained battery topic. MQTT commands keep their existing delivery and duplicate
handling.

A broker reconnect or Home Assistant's online announcement triggers a fresh
state snapshot. A five-minute snapshot also lets a subscriber recover a missed
update. The controller sends an online heartbeat every 30 seconds and
coalesces contact-only diagnostics to about once per minute per blind.

## Where to look

| Question | Source under `linux_controller/graber/` |
| --- | --- |
| How do setup, login, and the control API fit together? | `setup/`, `security/`, `webapp/` |
| Where are blind commands admitted, interrupted, and completed? | `control/dispatcher.py`, `control/blind.py` |
| Which rail values confirm that a command finished? | `control/feedback.py` |
| Who decides when a group can move? | `control/group_controller.py` |
| Which radio and bytes are used for a motor command? | `bluetooth/adapter_manager.py`, `bluetooth/ble_transport.py`, `bluetooth/protocol.py` |
| How do covers and commands map to MQTT topics? | `integrations/mqtt_bridge.py` |
| How can I tell whether a delay is in the queue, connection, or feedback? | `diagnostics/` and the [metrics guide](RPI5_SETUP.md#continuous-performance-metrics) |

The [protocol reference](PROTOCOL.md) describes the observed motor commands.
The [setup guide](RPI5_SETUP.md) covers adapter assignment, groups, MQTT, and
operator recovery.
