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
