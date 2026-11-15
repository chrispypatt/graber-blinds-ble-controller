#!/bin/sh
# Repeatable Raspberry Pi OS installer. Dry-run is the default.
set -eu

usage() {
  echo "usage: sudo deploy/install.sh [--apply] [--enable]" >&2
  echo "  --apply   install packages, source, venv, user, and systemd unit" >&2
  echo "  --enable  also enable/start the service (requires an existing private config)" >&2
  echo "  install targets may be overridden with GRABER_INSTALL_DIR," >&2
  echo "  GRABER_CONFIG_DIR, GRABER_STATE_DIR, and GRABER_SERVICE_NAME" >&2
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

echo "Plan:"
echo "  install BlueZ, Python venv support, and Bluetooth inspection utilities"
echo "  install controller source at $install_dir"
echo "  preserve any existing $config_dir/config.toml and secrets.toml"
echo "  create private runtime state at $state_dir"
echo "  install $service_file"
if [ "$enable" = true ]; then
  echo "  validate and enable/start graber-controller.service"
else
  echo "  leave graber-controller.service disabled and stopped"
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
install -d -o root -g graber -m 0750 "$config_dir"
install -d -o graber -g graber -m 0750 "$state_dir" "$state_dir/setup"
install -d -o root -g root -m 0755 "$install_dir/graber"
install -d -o root -g root -m 0755 "$install_dir/bin"
rsync -a --delete "$repo_dir/linux_controller/graber/" "$install_dir/graber/"
find "$install_dir/graber" -type d -exec chmod 0755 {} \;
find "$install_dir/graber" -type f -exec chmod 0644 {} \;
install -o root -g root -m 0755 "$script_dir/run-setup-wizard.sh" \
  "$install_dir/bin/run-setup-wizard"

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
  if [ ! -f "$config_dir/config.toml" ]; then
    echo "Refusing to enable: create $config_dir/config.toml from the example first" >&2
    exit 1
  fi
  if [ ! -f "$config_dir/secrets.toml" ]; then
    echo "Refusing to enable: create $config_dir/secrets.toml from the example first" >&2
    exit 1
  fi
  chown root:graber "$config_dir/config.toml"
  chmod 0640 "$config_dir/config.toml"
  chown graber:graber "$config_dir/secrets.toml"
  chmod 0600 "$config_dir/secrets.toml"
  sudo -u graber env PYTHONPATH="$install_dir" "$install_dir/.venv/bin/python" -m graber.main \
    --config "$config_dir/config.toml" --secrets "$config_dir/secrets.toml" --check-config
  systemctl enable --now "$service_name.service"
fi
