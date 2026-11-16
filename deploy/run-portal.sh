#!/bin/sh
# Start the single-port setup/control supervisor from private host settings.
set -eu

: "${GRABER_CONTROLLER_SOURCE_DIR:?missing GRABER_CONTROLLER_SOURCE_DIR}"
: "${GRABER_CONFIG:?missing GRABER_CONFIG}"
: "${GRABER_SECRETS:?missing GRABER_SECRETS}"
: "${GRABER_SETUP_SETTINGS:?missing GRABER_SETUP_SETTINGS}"
: "${GRABER_CONTROL_TOKEN_FILE:?missing GRABER_CONTROL_TOKEN_FILE}"
: "${GRABER_PORTAL_AUTH_FILE:?missing GRABER_PORTAL_AUTH_FILE}"
: "${GRABER_PORTAL_STATE_DIR:?missing GRABER_PORTAL_STATE_DIR}"
: "${GRABER_PORTAL_ALLOWED_NETWORKS:?missing GRABER_PORTAL_ALLOWED_NETWORKS}"

python=${GRABER_CONTROLLER_PYTHON:-$GRABER_CONTROLLER_SOURCE_DIR/.venv/bin/python}
export PYTHONPATH=${GRABER_CONTROLLER_PYTHONPATH:-$GRABER_CONTROLLER_SOURCE_DIR}
setup_runner=${GRABER_SETUP_RUNNER:-$GRABER_CONTROLLER_SOURCE_DIR/bin/run-setup-wizard}
host=${GRABER_PORTAL_HOST:-127.0.0.1}
port=${GRABER_PORTAL_PORT:-8080}
internal_port=${GRABER_PORTAL_INTERNAL_PORT:-18080}

set -- "$python" -m graber.portal \
  --host "$host" --port "$port" --internal-port "$internal_port" \
  --setup-runner "$setup_runner" \
  --setup-settings "$GRABER_SETUP_SETTINGS" \
  --config "$GRABER_CONFIG" --secrets "$GRABER_SECRETS" \
  --token-file "$GRABER_CONTROL_TOKEN_FILE" \
  --auth-file "$GRABER_PORTAL_AUTH_FILE" \
  --mode-file "$GRABER_PORTAL_STATE_DIR/mode"

case "${GRABER_PORTAL_SECURE_COOKIE:-false}" in
  true) set -- "$@" --secure-cookie ;;
  false) set -- "$@" --no-secure-cookie ;;
  *) echo "GRABER_PORTAL_SECURE_COOKIE must be true or false" >&2; exit 2 ;;
esac

if [ -n "${GRABER_PORTAL_TRUSTED_PROXIES:-}" ]; then
  old_ifs=$IFS
  IFS=,
  for network in $GRABER_PORTAL_TRUSTED_PROXIES; do
    set -- "$@" --trusted-proxy "$network"
  done
  IFS=$old_ifs
fi

old_ifs=$IFS
IFS=,
for network in $GRABER_PORTAL_ALLOWED_NETWORKS; do
  set -- "$@" --allowed-network "$network"
done
IFS=$old_ifs

exec "$@"
