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

Create an ignored private JSON file from `examples/deploy-config.example.json`.
It may contain deployment paths, target aliases, and upload addresses; none of
those belong in this repository. Pass it with `--deploy-config` or set
`GRABER_DEPLOY_CONFIG`.

```sh
export GRABER_DEPLOY_CONFIG=/path/to/private/deploy-config.json
```

From the repository checkout:

```sh
# Inspect the plan for an alias or inventory controller name; changes nothing.
python3 tools/deploy_controllers.py <target> --deploy-config "$GRABER_DEPLOY_CONFIG"

# Sync and regenerate Living, but do not build or flash.
python3 tools/deploy_controllers.py <target> --deploy-config "$GRABER_DEPLOY_CONFIG" --apply

# Sync, validate, and compile Living.
python3 tools/deploy_controllers.py <target> --deploy-config "$GRABER_DEPLOY_CONFIG" --apply --config --compile

# Compile and OTA-upload one controller. Use its current IP or mDNS name.
python3 tools/deploy_controllers.py <target> --deploy-config "$GRABER_DEPLOY_CONFIG" --apply --config --flash

# Build all four controllers without uploading.
python3 tools/deploy_controllers.py all --deploy-config "$GRABER_DEPLOY_CONFIG" --apply --config --compile

# Upload multiple controllers with explicit addresses.
python3 tools/deploy_controllers.py all --deploy-config "$GRABER_DEPLOY_CONFIG" --apply --config --flash
```

Target aliases are defined by the private config; `all` is always available.
With no configured device address, upload defaults to `<controller-name>.local`.
`--device NAME=ADDRESS` can override a configured address. `--skip-backup`
exists for recovery workflows only and is intentionally not recommended.

Recommended rollout is one controller at a time: apply/config/compile Living,
flash Living, verify its Home Assistant entities and movement, then proceed to
Upper, Main, and Basement. The script never changes remote pairings or writes
resolved MAC addresses/tokens into the repository.
