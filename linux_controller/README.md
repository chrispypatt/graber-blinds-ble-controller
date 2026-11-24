# Linux controller

This package is the independently deployable Raspberry Pi/BlueZ implementation.
The service starts in setup mode until the private shade inventory is saved.

For deployment, use `deploy/install.sh` and complete the browser setup wizard
described in `../docs/RPI5_SETUP.md`. Manual configuration and command-line
identification are diagnostic alternatives, not required setup steps.

Run offline tests from the repository root:

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install --require-hashes --only-binary=:all: -r linux_controller/requirements-dev.lock
pytest linux_controller/tests
```

The service reads `GRABER_CONFIG` and `GRABER_SECRETS`, defaulting to files in
`/etc/graber-controller`. The secrets file must have mode `0600`.

The public portal owns browser authentication and proxies to loopback-only setup
or control children. Password records contain salted hashes for a recovery
administrator and a separate everyday owner account. For HTTPS, place a trusted
reverse proxy in front of the loopback-bound portal; see `../docs/RPI5_SETUP.md`.

Configured Bluetooth controllers keep their logical IDs when BlueZ renumbers
them or when a unique configured USB adapter moves to another port. The saved
USB path disambiguates matching adapters. The controller refreshes discovery
periodically and logs when it uses a different path. Set
`auto_recover_power = true` on a dedicated adapter to let it clear that
adapter's software radio block and request power through BlueZ. The service
account needs permission for both operations. Recovery leaves hardware blocks
in effect.

If a different action, rail, or position arrives while a blind is executing a
command, the dispatcher cancels the active command and sends STOP. After STOP
is confirmed, it executes the latest pending movement request. Matching
in-flight targets are suppressed, and resubmitting a command ID remains
idempotent. An explicit STOP clears pending movement. A synchronized group
can stop busy members immediately and releases replacement movements only
after readiness and confirmed stops allow them under its group policy.

MQTT Discovery publishes native Home Assistant shade covers, including a
command-only full-shade Open/Close/Stop cover for each TDBU motor alongside its
independent rail covers. Expose these covers directly through Home Assistant
Cloud for Alexa instead of duplicating movement commands as scripts. See the
[Home Assistant setup instructions](../docs/RPI5_SETUP.md#home-assistant)
for the exposure settings.
