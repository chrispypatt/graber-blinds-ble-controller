# Deployment workflow

`tools/deploy_controllers.py` is the guarded bridge between this controller
repository and a Home Assistant ESPHome deployment. It keeps the private
private inventory and the deployment's existing `secrets.yaml`; neither is
copied into Git. The deployment paths are supplied by private command-line
arguments or environment variables and are deliberately not defaults in this
repository.

The command is a dry run unless `--apply` is supplied. An apply first creates a
recoverable backup under `/data/graber-deploy-backup-<UTC timestamp>/`, then
synchronizes only the reviewed `packages/` and `components/` directories and
regenerates the selected wrapper YAML. It does not flash a device unless the
separate `--flash` flag is also supplied.

When `--config`, `--compile`, or `--flash` is requested, the selected wrappers,
reviewed packages/components, and the live ESPHome `secrets.yaml` are copied to
a temporary writable build tree. ESPHome runs from that tree, so its `.esphome`
cache and generated build files do not depend on the live Home Assistant mount.
The temporary tree is removed when the command exits; secrets are never staged
in the repository or retained by this tool.

Set private deployment paths in your shell (or pass the corresponding flags):

```sh
export GRABER_INVENTORY=/path/to/private/controllers.json
export GRABER_CONTROLLER_ROOT=/path/to/ha/graber-controller
export GRABER_ESPHOME_ROOT=/path/to/ha/esphome
export GRABER_BACKUP_ROOT=/path/to/private/deploy-backups
```

From the repository checkout:

```sh
# Inspect the plan; changes nothing.
python3 tools/deploy_controllers.py living

# Sync and regenerate Living, but do not build or flash.
python3 tools/deploy_controllers.py living --apply

# Sync, validate, and compile Living.
python3 tools/deploy_controllers.py living --apply --config --compile

# Compile and OTA-upload one controller. Use its current IP or mDNS name.
python3 tools/deploy_controllers.py living --apply --config --flash \
  --device living=<controller-address-or-hostname>

# Build all four controllers without uploading.
python3 tools/deploy_controllers.py all --apply --config --compile

# Upload multiple controllers with explicit addresses.
python3 tools/deploy_controllers.py all --apply --config --flash \
  --device living=<living-address-or-hostname> \
  --device main=<main-address-or-hostname> \
  --device upper=<upper-address-or-hostname> \
  --device basement=<basement-address-or-hostname>
```

Target aliases are `living`, `main`, `upper`, `basement`, and `all`. With no
`--device`, the upload target defaults to `<controller-name>.local`; pass an
explicit address when mDNS is unreliable. `--skip-backup` exists for recovery
workflows only and is intentionally not recommended.

Recommended rollout is one controller at a time: apply/config/compile Living,
flash Living, verify its Home Assistant entities and movement, then proceed to
Upper, Main, and Basement. The script never changes remote pairings or writes
resolved MAC addresses/tokens into the repository.
