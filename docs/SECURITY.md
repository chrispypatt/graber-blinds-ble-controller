# Security and privacy

Treat each motor's seven-digit access code as a secret. BLE MAC addresses and
room assignments are also private inventory data. Never commit any of them to a
public repository, issue, CI log, or support bundle.

Keep these values in ESPHome's ignored `secrets.yaml`. The example inventory
contains only secret *names*. Generated production YAML may reference those
names but must never contain the resolved values.

Raw Android bug reports, HCI snoop captures, GATT dumps, and Home Assistant
backups can contain account data and identifiers. The repository ignores common
capture and archive formats, but review every staged file before publishing:

```sh
git status --short
git diff --cached
git grep -nE '[0-9]{7}|([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}'
```

The last command also finds harmless examples; inspect every match rather than
assuming a clean exit is sufficient.
