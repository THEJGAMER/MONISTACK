#!/usr/bin/env bash
# Installs a prebuilt s4048-exporter binary (see build_binary.sh) as a
# systemd service. No Python needed on the target host. Run as root, with
# the binary already built and sitting next to this script (or pass its
# path as $1).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo $0 [path-to-binary])" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINARY_SRC="${1:-$SCRIPT_DIR/s4048-exporter}"
INSTALL_DIR=/opt/s4048-exporter
CONFIG_DIR=/etc/s4048-exporter
SERVICE_USER=s4048-exporter
SERVICE_NAME=s4048-exporter

if [[ ! -f "$BINARY_SRC" ]]; then
  echo "Binary not found at $BINARY_SRC - build it first with build_binary.sh (see README)" >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating service user $SERVICE_USER"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing binary to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 755 "$BINARY_SRC" "$INSTALL_DIR/s4048-exporter"
# devices.yaml is optional (see exporter.env.example) - build_binary.sh
# copies it next to the binary when present, this carries it along.
if [[ -f "$(dirname "$BINARY_SRC")/devices.yaml" ]]; then
  cp "$(dirname "$BINARY_SRC")/devices.yaml" "$INSTALL_DIR/devices.yaml"
fi

echo "==> Writing config to $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/exporter.env" ]]; then
  cp "$SCRIPT_DIR/exporter.env.example" "$CONFIG_DIR/exporter.env"
  echo "    Wrote default config - EDIT $CONFIG_DIR/exporter.env with the real switch credentials before starting."
fi
chmod 600 "$CONFIG_DIR/exporter.env"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR"

echo "==> Installing systemd unit"
sed "s#__EXEC_START__#$INSTALL_DIR/s4048-exporter#" \
  "$SCRIPT_DIR/s4048-exporter.service.template" > "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo
echo "Installed. Next steps:"
echo "  1. Edit $CONFIG_DIR/exporter.env with the switch's real host/user/password"
echo "  2. systemctl restart $SERVICE_NAME"
echo "  3. journalctl -u $SERVICE_NAME -f     # watch logs"
