#!/bin/sh
# Repeatable Raspberry Pi OS installer. Dry-run is the default.
set -eu

usage() {
  echo "usage: sudo deploy/install.sh [--apply] [--enable]" >&2
  echo "  --apply   install packages, source, venv, user, and systemd unit" >&2
  echo "  --enable  also enable/start the setup/control web service" >&2
  echo "  install targets may be overridden with GRABER_INSTALL_DIR," >&2
  echo "  GRABER_CONFIG_DIR, GRABER_STATE_DIR, and GRABER_SERVICE_NAME" >&2
  echo "  web bind may be overridden with GRABER_PORTAL_HOST," >&2
  echo "  GRABER_PORTAL_PORT, and GRABER_ALLOWED_NETWORKS" >&2
}

apply=false
enable=false
for argument in "$@"; do
  case "$argument" in
    --apply) apply=true ;;
    --enable) enable=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if [ "$enable" = true ] && [ "$apply" != true ]; then
  echo "--enable requires --apply" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
install_dir=${GRABER_INSTALL_DIR:-/opt/graber-controller}
config_dir=${GRABER_CONFIG_DIR:-/etc/graber-controller}
state_dir=${GRABER_STATE_DIR:-/var/lib/graber-controller}
service_name=${GRABER_SERVICE_NAME:-graber-controller}
service_file=/etc/systemd/system/$service_name.service
portal_host=${GRABER_PORTAL_HOST:-0.0.0.0}
portal_port=${GRABER_PORTAL_PORT:-8080}
portal_allowed_networks=${GRABER_ALLOWED_NETWORKS:-127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7}

for target in "$install_dir" "$config_dir" "$state_dir"; do
  case "$target" in
    /*) ;;
    *) echo "install targets must be absolute paths: $target" >&2; exit 2 ;;
  esac
  case "$target" in
    *'|'*|*'%'*|*'&'*|*[[:space:]]*)
      echo "install targets may not contain whitespace or shell/template delimiters: $target" >&2
      exit 2
      ;;
  esac
done
case "$service_name" in
  *[!A-Za-z0-9_.@-]*|'') echo "invalid GRABER_SERVICE_NAME" >&2; exit 2 ;;
esac
case "$portal_host" in
  *[!A-Za-z0-9_.:-]*|'') echo "invalid GRABER_PORTAL_HOST" >&2; exit 2 ;;
esac
case "$portal_port" in
  *[!0-9]*|'') echo "invalid GRABER_PORTAL_PORT" >&2; exit 2 ;;
esac
if [ "$portal_port" -lt 1 ] || [ "$portal_port" -gt 65535 ]; then
  echo "GRABER_PORTAL_PORT must be from 1 through 65535" >&2
  exit 2
fi
case "$portal_allowed_networks" in
  *[!A-Fa-f0-9:.,/-]*|'') echo "invalid GRABER_ALLOWED_NETWORKS" >&2; exit 2 ;;
esac

echo "Plan:"
echo "  install BlueZ, Python venv support, and Bluetooth inspection utilities"
echo "  install controller source at $install_dir"
echo "  preserve any existing $config_dir/config.toml and secrets.toml"
echo "  create or preserve private setup settings and key"
echo "  install $service_file"
if [ "$enable" = true ]; then
  echo "  enable/start $service_name in setup or control mode"
else
  echo "  leave $service_name.service disabled and stopped"
fi

if [ "$apply" != true ]; then
  echo "Dry run only. Re-run with --apply after reviewing this plan."
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "--apply must run as root (use sudo); the service itself runs as user graber" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bluez bluetooth rfkill rsync usbutils python3 python3-venv python3-pip

if ! getent group bluetooth >/dev/null 2>&1; then
  groupadd --system bluetooth
fi
if ! getent group graber >/dev/null 2>&1; then
  groupadd --system graber
fi
if ! id graber >/dev/null 2>&1; then
  useradd --system --home-dir "$state_dir" --shell /usr/sbin/nologin \
    --gid graber --groups bluetooth graber
else
  usermod -a -G bluetooth graber
fi

install -d -o root -g root -m 0755 "$install_dir"
install -d -o root -g graber -m 0770 "$config_dir"
install -d -o graber -g graber -m 0700 "$state_dir" "$state_dir/portal"
install -d -o root -g root -m 0755 "$install_dir/graber"
install -d -o root -g root -m 0755 "$install_dir/bin"
rsync -a --delete "$repo_dir/linux_controller/graber/" "$install_dir/graber/"
find "$install_dir/graber" -type d -exec chmod 0755 {} \;
find "$install_dir/graber" -type f -exec chmod 0644 {} \;
install -o root -g root -m 0755 "$script_dir/run-setup-wizard.sh" \
  "$install_dir/bin/run-setup-wizard"
install -o root -g root -m 0755 "$script_dir/run-portal.sh" \
  "$install_dir/bin/run-portal"

if [ ! -d "$install_dir/.venv" ]; then
  python3 -m venv "$install_dir/.venv"
fi
"$install_dir/.venv/bin/python" -m pip install -r "$repo_dir/linux_controller/requirements.txt"

if [ -f "$repo_dir/linux_controller/config.example.toml" ]; then
  install -o root -g graber -m 0640 "$repo_dir/linux_controller/config.example.toml" \
    "$config_dir/config.toml.example"
fi
if [ -f "$repo_dir/linux_controller/secrets.example.toml" ]; then
  install -o root -g graber -m 0640 "$repo_dir/linux_controller/secrets.example.toml" \
    "$config_dir/secrets.toml.example"
fi
install -o root -g graber -m 0640 "$script_dir/setup-wizard.example.env" \
  "$config_dir/setup-wizard.example.env"

token_file=$state_dir/setup-token
setup_settings=$config_dir/setup.env
controller_environment=$config_dir/controller.env
if [ ! -f "$token_file" ]; then
  token_tmp=$(mktemp)
  "$install_dir/.venv/bin/python" -c \
    'import secrets; print(secrets.token_hex(24))' >"$token_tmp"
  install -o graber -g graber -m 0600 "$token_tmp" "$token_file"
  rm -f "$token_tmp"
fi
chown graber:graber "$token_file"
chmod 0600 "$token_file"

if [ ! -f "$setup_settings" ]; then
  settings_tmp=$(mktemp)
  {
    echo "GRABER_SETUP_SOURCE_DIR=$install_dir"
    echo "GRABER_SETUP_PYTHONPATH=$install_dir"
    echo "GRABER_SETUP_TOKEN_FILE=$token_file"
    echo "GRABER_SETUP_CONTROLLER_ADDRESSES=auto"
    echo "GRABER_SETUP_ALLOWED_NETWORKS=127.0.0.0/8,::1/128"
    echo "GRABER_SETUP_EXPORT_DIR=$config_dir"
    echo "GRABER_SETUP_HOST=127.0.0.1"
    echo "GRABER_SETUP_PORT=18080"
    echo "GRABER_SETUP_ALLOW_CODE_READ=true"
    echo "GRABER_SETUP_ALLOW_JOG=true"
  } >"$settings_tmp"
  install -o graber -g graber -m 0600 "$settings_tmp" "$setup_settings"
  rm -f "$settings_tmp"
fi
chown graber:graber "$setup_settings"
chmod 0600 "$setup_settings"

if [ ! -f "$controller_environment" ]; then
  environment_tmp=$(mktemp)
  {
    echo "GRABER_CONTROLLER_SOURCE_DIR=$install_dir"
    echo "GRABER_CONTROLLER_PYTHONPATH=$install_dir"
    echo "GRABER_CONFIG=$config_dir/config.toml"
    echo "GRABER_SECRETS=$config_dir/secrets.toml"
    echo "GRABER_SETUP_RUNNER=$install_dir/bin/run-setup-wizard"
    echo "GRABER_SETUP_SETTINGS=$setup_settings"
    echo "GRABER_CONTROL_TOKEN_FILE=$token_file"
    echo "GRABER_PORTAL_STATE_DIR=$state_dir/portal"
    echo "GRABER_PORTAL_ALLOWED_NETWORKS=$portal_allowed_networks"
    echo "GRABER_PORTAL_HOST=$portal_host"
    echo "GRABER_PORTAL_PORT=$portal_port"
  } >"$environment_tmp"
  install -o root -g graber -m 0640 "$environment_tmp" "$controller_environment"
  rm -f "$environment_tmp"
fi
chown root:graber "$controller_environment"
chmod 0640 "$controller_environment"
if [ -f "$config_dir/config.toml" ] && [ -f "$config_dir/secrets.toml" ]; then
  chown graber:graber "$config_dir/config.toml" "$config_dir/secrets.toml"
  chmod 0600 "$config_dir/config.toml" "$config_dir/secrets.toml"
fi
unit_tmp=$(mktemp)
trap 'rm -f "$unit_tmp"' EXIT HUP INT TERM
sed \
  -e "s|@INSTALL_DIR@|$install_dir|g" \
  -e "s|@CONFIG_DIR@|$config_dir|g" \
  -e "s|@STATE_DIR@|$state_dir|g" \
  "$script_dir/graber-controller.service" >"$unit_tmp"
install -o root -g root -m 0644 "$unit_tmp" "$service_file"
systemctl daemon-reload

if [ "$enable" = true ]; then
  if [ -f "$config_dir/config.toml" ] && [ -f "$config_dir/secrets.toml" ]; then
    sudo -u graber env PYTHONPATH="$install_dir" "$install_dir/.venv/bin/python" -m graber.main \
      --config "$config_dir/config.toml" --secrets "$config_dir/secrets.toml" --check-config
  elif [ -f "$config_dir/config.toml" ] || [ -f "$config_dir/secrets.toml" ]; then
    echo "Refusing to enable: config.toml and secrets.toml must both exist or both be absent" >&2
    exit 1
  fi
  systemctl enable --now "$service_name.service"
fi
