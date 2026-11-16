# Security and privacy

Treat motor access codes, motor/controller Bluetooth addresses, USB serials,
room assignments, MQTT credentials, portal authentication, and Home Assistant
mappings as private household inventory.

Store topology in `/etc/graber-controller/config.toml` and credentials in
`secrets.toml`. The secrets file must have no group/other permissions; the
installer uses mode `0600` owned by the unprivileged `graber` user. Local config
and `private/` paths are ignored by Git.

The public portal is CIDR-restricted and owns password authentication, expiring
browser sessions, request-forgery protection, and login throttling. Passwords
are stored as salted hashes. The private controller token is injected only on
the loopback hop and is never stored by browser JavaScript. Identification jog
also retains its explicit live gate.

The installer initially hashes the existing private setup key and marks that
credential for mandatory replacement. After the first password change, the
setup key remains an internal loopback credential and is not a portal password.

Plain HTTP still exposes login credentials, cookies, state, and commands to an
observer on the network. For long-term use, bind the portal to loopback and use
an HTTPS reverse proxy. The Caddy example uses an internal CA; every client must
trust that CA root before relying on it. Do not bypass certificate warnings,
port-forward the UI, or treat CIDR filtering as encryption.

Treat the Caddy data directory as private backup material because it contains
the local CA private key. Distribute only the public root certificate to client
devices and verify its fingerprint through a trusted channel.

Use a dedicated, non-administrator MQTT account with a randomly generated
password. Store it only in private `secrets.toml`. Prefer broker TLS with
hostname verification; plain MQTT exposes credentials and commands to anyone
able to observe that network segment. Do not expose the broker to the internet.
Never reuse the portal password or internal controller token for MQTT.

MQTT movement commands are never retained. Duplicate QoS deliveries reuse an
in-memory ID, and movement queues do not survive restart. Discovery can use
separate controller IDs during migration; keep the chosen production ID stable
after cutover to avoid duplicate entities.

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
