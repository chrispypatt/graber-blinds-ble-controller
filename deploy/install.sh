#!/bin/sh
# Repeatable Raspberry Pi OS installer. Dry-run is the default.
set -eu

usage() {
  echo "usage: sudo deploy/install.sh [--apply] [--enable]" >&2
  echo "  --apply   install packages, source, venv, user, and systemd unit" >&2
  echo "  --enable  also enable/start the service (requires an existing private config)" >&2
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
install_dir=/opt/graber-controller
config_dir=/etc/graber-controller
service_file=/etc/systemd/system/graber-controller.service

echo "Plan:"
echo "  install BlueZ, Python venv support, and Bluetooth inspection utilities"
echo "  install controller source at $install_dir"
echo "  preserve any existing $config_dir/config.toml and secrets.toml"
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
  useradd --system --home-dir /var/lib/graber-controller --shell /usr/sbin/nologin \
    --gid graber --groups bluetooth graber
else
  usermod -a -G bluetooth graber
fi

install -d -o root -g root -m 0755 "$install_dir"
install -d -o root -g graber -m 0750 "$config_dir"
install -d -o root -g root -m 0755 "$install_dir/graber"
rsync -a --delete "$repo_dir/linux_controller/graber/" "$install_dir/graber/"
find "$install_dir/graber" -type d -exec chmod 0755 {} \;
find "$install_dir/graber" -type f -exec chmod 0644 {} \;

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
install -o root -g root -m 0644 "$script_dir/graber-controller.service" "$service_file"
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
  sudo -u graber "$install_dir/.venv/bin/python" -m graber.main \
    --config "$config_dir/config.toml" --secrets "$config_dir/secrets.toml" --check-config
  systemctl enable --now graber-controller.service
fi
