# Security and privacy

Treat motor access codes, motor/controller Bluetooth addresses, USB serials,
room assignments, MQTT credentials, web tokens, and Home Assistant mappings as
private household inventory.

Store topology in `/etc/graber-controller/config.toml` and credentials in
`secrets.toml`. The secrets file must have no group/other permissions; the
installer uses mode `0600` owned by the unprivileged `graber` user. Local config
and `private/` paths are ignored by Git.

The web listener is CIDR-restricted. Live controller or identification-jog
actions additionally require a non-placeholder private token and explicit live
gate. Do not port-forward either UI. They use plain HTTP on the trusted LAN; add
a separately managed authenticated TLS reverse proxy if encryption is needed.

MQTT movement commands are never retained. Duplicate QoS deliveries reuse an
in-memory ID, and movement queues do not survive restart. Discovery uses
separate controller IDs during migration.

Identification output is redacted by default. Full captures require an explicit
path and are mode `0600`; candidate access-code reads require another explicit
flag. Ordinary identification never writes GATT or changes motor associations.

HCI captures, mobile diagnostics, journals, metrics, HA backups, and screenshots
may contain private data. Inspect every staged file before publishing:

```sh
git status --short
git diff --cached
git grep -nE '[0-9]{7}|([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}'
```

Matches include placeholders/tests, so inspect each result.
