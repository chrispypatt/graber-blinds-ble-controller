# Contributing

Keep verified protocol observations separate from inference. For every new
trace, record motor model/firmware privately and state what remains uncertain.
Never expose a command merely because its bytes appeared in an uncorrelated
capture.

Before proposing a change:

```sh
python3 -m venv .venv-linux
. .venv-linux/bin/activate
pip install -r linux_controller/requirements-dev.txt
make check
```

Changes to retries, writes, Stop, feedback completion, adapter assignment, or
group release need an offline regression test. Never automatically replay a
command after an effectful write acknowledgement.

Hardware validation starts only after offline tests and adapter diagnostics.
Begin with advertisements, then one supervised blind and a small movement.
Never reset/re-pair a motor or run two automated controllers against one blind.

Do not commit access codes, BLE/controller identities, room assignments,
private captures, MQTT credentials, HA backups, or household network details.
