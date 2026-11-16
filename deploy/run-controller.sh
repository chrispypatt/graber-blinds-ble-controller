#!/bin/sh
# Start the controller from host-specific paths supplied by a private environment file.
set -eu

: "${GRABER_CONTROLLER_SOURCE_DIR:?missing GRABER_CONTROLLER_SOURCE_DIR}"
: "${GRABER_CONFIG:?missing GRABER_CONFIG}"
: "${GRABER_SECRETS:?missing GRABER_SECRETS}"

python=${GRABER_CONTROLLER_PYTHON:-$GRABER_CONTROLLER_SOURCE_DIR/.venv/bin/python}
export PYTHONPATH=${GRABER_CONTROLLER_PYTHONPATH:-$GRABER_CONTROLLER_SOURCE_DIR/linux_controller}

exec "$python" -m graber.main --config "$GRABER_CONFIG" --secrets "$GRABER_SECRETS"
