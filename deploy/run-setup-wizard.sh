#!/bin/sh
# Start the temporary identification UI from a private host-specific settings file.
set -eu

settings=${1:-}
if [ -z "$settings" ] || [ ! -f "$settings" ]; then
  echo "usage: deploy/run-setup-wizard.sh /path/to/private-settings.env" >&2
  exit 2
fi

mode=$(stat -c %a "$settings")
case "$mode" in
  400|600) ;;
  *) echo "private settings must have mode 0400 or 0600" >&2; exit 2 ;;
esac

# This file is locally controlled and must contain only NAME=value assignments.
# shellcheck disable=SC1090
. "$settings"

: "${GRABER_SETUP_SOURCE_DIR:?missing GRABER_SETUP_SOURCE_DIR}"
: "${GRABER_SETUP_TOKEN_FILE:?missing GRABER_SETUP_TOKEN_FILE}"
: "${GRABER_SETUP_CONTROLLER_ADDRESSES:?missing GRABER_SETUP_CONTROLLER_ADDRESSES}"
: "${GRABER_SETUP_ALLOWED_NETWORKS:?missing GRABER_SETUP_ALLOWED_NETWORKS}"
: "${GRABER_SETUP_EXPORT_DIR:?missing GRABER_SETUP_EXPORT_DIR}"

token_mode=$(stat -c %a "$GRABER_SETUP_TOKEN_FILE")
case "$token_mode" in
  400|600) ;;
  *) echo "setup token must have mode 0400 or 0600" >&2; exit 2 ;;
esac
GRABER_SETUP_TOKEN=$(cat "$GRABER_SETUP_TOKEN_FILE")
export GRABER_SETUP_TOKEN

python=${GRABER_SETUP_PYTHON:-$GRABER_SETUP_SOURCE_DIR/.venv/bin/python}
host=${GRABER_SETUP_HOST:-127.0.0.1}
port=${GRABER_SETUP_PORT:-8090}
export PYTHONPATH=${GRABER_SETUP_PYTHONPATH:-$GRABER_SETUP_SOURCE_DIR/linux_controller}

set -- "$python" -m graber.identify_web \
  --host "$host" --port "$port" --export-dir "$GRABER_SETUP_EXPORT_DIR"

old_ifs=$IFS
IFS=,
for network in $GRABER_SETUP_ALLOWED_NETWORKS; do
  set -- "$@" --allowed-network "$network"
done
for address in $GRABER_SETUP_CONTROLLER_ADDRESSES; do
  set -- "$@" --controller-address "$address"
done
IFS=$old_ifs

if [ "${GRABER_SETUP_ALLOW_CODE_READ:-false}" = true ]; then
  set -- "$@" --allow-code-read
fi
if [ "${GRABER_SETUP_ALLOW_JOG:-false}" = true ]; then
  set -- "$@" --allow-jog
fi

exec "$@"
