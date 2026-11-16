# Raspberry Pi 5 pilot and migration

The Linux controller is an independent pilot alongside the existing legacy
controller. It does not deploy itself or move a blind during automated
tests. Every physical movement phase requires an explicit live-test gate and a
person supervising the room.

## Safety invariants

- Never reset, exclude, pair, or alter the motor's app/remote associations.
- Never run two active control paths against one blind. Legacy entities may remain
  visible, but disable their relevant automations before the Pi issues commands.
- Never retry a command after its final write was acknowledged unless feedback
  proves that retry is safe. Delivery and physical completion are distinct.
- Never restore queued movement commands after a process restart or MQTT outage.
- Keep permanent connections disabled. Start with a configurable 30-second warm
  hold and no increase to the existing battery-read frequency.

## Phase A: offline development

Run the Python suite with mock scanning/transport only. It covers protocol
bytes/timing, authentication, rail normalization, queue bounds, idempotency,
retry cleanup, adapter resolution/failover, group policies, timeouts, shutdown,
MQTT discovery, web authorization, and performance summaries. No test should
need BlueZ or a physical shade.

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install -r linux_controller/requirements-dev.txt
pytest linux_controller/tests
```

## Phase B: Pi and advertisement-only Bluetooth validation

Follow `RPI5_SETUP.md`. Validate adapters individually, together, after reboot,
and after a temporary USB disconnect. Resolve them from controller address plus
USB identity/path every time; `hciN` is only the current runtime handle.

Perform adapter-specific scans that do not connect or write and collect
advertisement latency and RSSI for every motor selected for the pilot. Do not
read access codes during routine diagnostics, log them, or publish raw inventory
output. Verify motor model, single/TDBU type, firmware, address, and any existing
HA entity mapping against the private deployed configuration.

The setup wizard may be used here without `--allow-jog` to name devices and
export draft configuration. Enable its jog gate only in Phase C after entering
a verified/candidate access code, with the room supervised and legacy
automations disabled for that blind.

## Phase C: one supervised blind

Keep live movement disabled while verifying advertisements and, if needed, an
authenticated position/battery session. Then explicitly enable live testing for
one motor and one operator session:

1. Make one small position adjustment—not a full-travel cycle.
2. Issue Stop and verify feedback distinguishes write ACK, observed motion, and
   stable position.
3. Let the warm timeout expire and verify disconnect.
4. Confirm the physical remote and official app still work.
5. Disable the live-test gate when finished.

## Phase D: multi-blind pilot

Assign pilot blinds across the configured adapters as an experiment, not a
capacity claim. Start with one blind per adapter, then increase concurrent
sessions only when the earlier test is healthy. Best-effort group mode is the
default: an unavailable blind reports its own failure while the others proceed.
Synchronized mode uses a bounded preparation deadline and the configured policy
for partial preparation; no prepared connection waits indefinitely.

Inject an unavailable mock/configured test target before causing a real outage.
Confirm each blind has an independent queue/state machine and that no blind can
be leased by two adapters at once.

## Phase E: reliability comparison

Use read-only authenticated sessions and small supervised movements to avoid
unnecessary battery drain/full travel. Target at least 50 samples per blind when
practical. Each operation record should contain a command ID and timestamps for:

- receipt, advertisement discovery, connect start/finish;
- service setup, authentication, command submission and ATT acknowledgement;
- first changed position/movement feedback and completion/timeout;
- disconnect/cleanup, attempt count, adapter stable ID, and error category.

Do not include access codes or raw private addresses. Generate an offline report:

```sh
/opt/graber-controller/.venv/bin/python -m graber.diagnostic_cli report \
  /var/lib/graber-controller/metrics.jsonl
```

Compare first-attempt connection success, eventual command success, median/p95
connection and movement-start latency, retries, timeouts, adapter recovery, and
group outcome. A GATT-write success rate is reported separately and cannot stand
in for movement success. Establish a comparable official-app observation as the
deployment-local reference. The stated 3-second/95%/99% goals remain targets,
not claims.

Keep the metrics file bounded/rotated and use aggregate reports for long-term
history to protect microSD storage. The example caps the active JSONL file at
2 MB and keeps one previous file.

## Home Assistant cutover

Before MQTT is enabled, confirm the broker and its authentication/TLS policy.
The Pi initially publishes retained Discovery documents under separate pilot
unique IDs. Command messages are never retained; state and availability are
non-optimistic. Verify top/bottom covers, battery, last communication, command
status, retry/failure counters, and adapter health without changing the current
legacy entities.

TDBU rail positions intentionally use Home Assistant cover semantics rather
than exposing both Graber bytes directly. The bottom rail is unchanged
(`HA = raw`): 0 is closed/bottom and 100 is open/top. The top rail is inverted
(`HA = 100 - raw`): Home Assistant Open/100 sends raw 0 and lowers the top rail;
Close/0 sends raw 100 and raises it. This matches the verified ESP controller.
Full TDBU open therefore reports bottom 100/top 0, and full close reports
bottom 0/top 0. The two cover entities describe rail locations; they do not
promise that two arbitrary absolute targets persist together.

Discovery includes individual rail covers and command-only group covers. Group
entities accept Open, Close, and Stop but do not publish an invented aggregate
position. Blind and group command topics are distinct even when their IDs are
the same. On every broker reconnect the controller republishes its currently
observed positions, diagnostics, and per-blind availability. Retained movement
commands are always ignored.

When pilot acceptance is complete:

1. Export or record affected dashboards, entity names, and automation targets.
2. Stop the Pi service.
3. Disable legacy automations that issue commands to the selected pilot blinds
   while leaving devices/remotes/app associations intact.
4. Start the Pi service, update automations to pilot entities, and supervise the
   first run.
5. Preserve old entities disabled/available for rollback until the observation
   period passes. Do not delete them during the pilot.

## Rollback

Rollback never requires motor re-pairing:

```sh
sudo systemctl disable --now graber-controller
```

Confirm the service is stopped and no BLE sessions remain, then re-enable the
previous controller automations. If rolling back only software, check out the last
known-good Git revision, run `sudo ./deploy/install.sh --apply`, validate the
unchanged private config, and restart. The installer does not overwrite
`/etc/graber-controller/config.toml` or `secrets.toml`.

If an external adapter is unhealthy, stop the service before unplugging it. Do
not silently fall back to onboard Bluetooth or reboot the Pi as routine
recovery. Preserve the metrics/journal excerpt needed to diagnose the failure,
redacting private inventory before sharing.
