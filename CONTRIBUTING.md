# Contributing

Keep protocol observations separate from conclusions, and state the motor model
and firmware for every new trace. Do not upload access codes, BLE addresses,
Home Assistant backups, Android bug reports, or HCI captures containing private
data.

Before proposing a change:

```sh
cp examples/secrets.yaml.example secrets.yaml
make test
make compile-example
python -m py_compile tools/*.py
```

Changes to connection scheduling, retries, writes, Stop, or bulk behavior need a
host-side regression test. Hardware validation should begin with one blind and
a small movement. Never require users to reset native remotes for BLE testing.

The repository is licensed under the MIT License. Contributions are accepted
through the private project repository and should continue to exclude all
device-specific secrets and identifiers.
