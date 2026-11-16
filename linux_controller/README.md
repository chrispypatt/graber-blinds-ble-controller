# Linux controller

This package is the independently deployable Raspberry Pi/BlueZ implementation.
Live Bluetooth and web movement controls remain disabled until private
configuration is supplied and the supervised pilot steps in
`../docs/RPI5_MIGRATION.md` are followed.

For deployment, use `deploy/install.sh` and complete the browser setup wizard
described in `../docs/RPI5_SETUP.md`. Manual configuration and command-line
identification are diagnostic alternatives, not required setup steps.

Run offline tests from the repository root:

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install -r linux_controller/requirements-dev.txt
pytest linux_controller/tests
```

The service reads `GRABER_CONFIG` and `GRABER_SECRETS`, defaulting to files in
`/etc/graber-controller`. The secrets file must have mode `0600`.

The public portal owns browser authentication and proxies to loopback-only setup
or control children. Password records contain salted hashes for a recovery
administrator and a separate everyday owner account. For HTTPS, place a trusted
reverse proxy in front of the loopback-bound portal; see `../docs/RPI5_SETUP.md`.

Configured Bluetooth controllers are tracked by stable address/USB identity,
not `hciN`. Lost adapters are marked unavailable and automatically rebound after
they return; queued connection work remains bounded and the onboard controller
is never selected implicitly.

MQTT Discovery publishes native Home Assistant shade covers, including a
command-only full-shade Open/Close/Stop cover for each TDBU motor alongside its
independent rail covers. Expose these covers directly through Home Assistant
Cloud for Alexa instead of duplicating movement commands as scripts. See the
setup and migration guides for the exposure and cleanup steps.
