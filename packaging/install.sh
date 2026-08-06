#!/usr/bin/env bash
# Installs the S4048 exporter into a venv on this host (LXC or otherwise)
# and runs it as a systemd service. Run as root, from a checkout that still
# has ../exporter and ../common next to this script.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo $0)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTER_SRC="$SCRIPT_DIR/../exporter"
COMMON_SRC="$SCRIPT_DIR/../common"
INSTALL_DIR=/opt/s4048-exporter
CONFIG_DIR=/etc/s4048-exporter
SERVICE_USER=s4048-exporter
SERVICE_NAME=s4048-exporter

if [[ ! -f "$EXPORTER_SRC/exporter.py" ]]; then
  echo "Couldn't find $EXPORTER_SRC/exporter.py - run this from the packaging/ dir of the repo checkout" >&2
  exit 1
fi
if [[ ! -f "$COMMON_SRC/db.py" ]]; then
  echo "Couldn't find $COMMON_SRC/db.py - the exporter now shares db.py/store.py/devices.py/ssh_client.py/metrics.py with webui/, run this from a full repo checkout" >&2
  exit 1
fi

# `import venv` succeeding is NOT a reliable signal that venv creation
# will actually work - on Debian/Ubuntu, the venv module is part of the
# standard library and imports fine even without the python3-venv OS
# package, but without that package ensurepip has nothing to bootstrap
# from, so `python3 -m venv` silently creates a venv with no pip in it at
# all (confirmed live: venv/bin/pip simply didn't exist afterward, no
# error at venv-creation time to catch it). Always ensure the real
# package is installed - apt skips it near-instantly if already present,
# so this costs nothing on repeat runs.
echo "==> Ensuring python3-venv/python3-pip are installed"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating service user $SERVICE_USER"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing app to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/app"
cp "$COMMON_SRC"/db.py "$COMMON_SRC"/store.py "$COMMON_SRC"/devices.py "$COMMON_SRC"/ssh_client.py "$COMMON_SRC"/metrics.py "$COMMON_SRC"/junos_parsers.py "$COMMON_SRC"/devices.yaml "$INSTALL_DIR/app/"
cp "$EXPORTER_SRC"/parsers.py "$EXPORTER_SRC"/exporter.py "$EXPORTER_SRC"/requirements.txt "$INSTALL_DIR/app/"

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  echo "==> Creating venv"
  python3 -m venv "$INSTALL_DIR/venv"
fi
if [[ ! -x "$INSTALL_DIR/venv/bin/pip" ]]; then
  echo "venv at $INSTALL_DIR/venv has no pip - python3-venv likely failed to" >&2
  echo "install above, or a broken venv was left over from a previous run." >&2
  echo "Try: rm -rf $INSTALL_DIR/venv && re-run this script." >&2
  exit 1
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade -q pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/app/requirements.txt"

echo "==> Writing config to $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/exporter.env" ]]; then
  cp "$SCRIPT_DIR/exporter.env.example" "$CONFIG_DIR/exporter.env"
  echo "    Wrote default config - EDIT $CONFIG_DIR/exporter.env with the real switch credentials before starting."
fi
chmod 600 "$CONFIG_DIR/exporter.env"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR"

echo "==> Installing systemd unit"
sed "s#__EXEC_START__#$INSTALL_DIR/venv/bin/python $INSTALL_DIR/app/exporter.py#" \
  "$SCRIPT_DIR/s4048-exporter.service.template" > "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# Deliberately asymmetric, and the asymmetry is the point:
#
#  - A *first* install must not start: exporter.env still holds the
#    example credentials, so starting now just fails against the switch
#    and fills the journal with auth errors before anyone has configured
#    it. The next-steps below tell you to edit it and start.
#  - An *update* to a service that is already running must restart, or the
#    new code sits on disk doing nothing while the old process keeps
#    serving. That is not hypothetical: it is exactly how a deployment
#    ended up with a frontend calling an endpoint its own backend didn't
#    have yet, with every file correctly copied.
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "==> $SERVICE_NAME is running - restarting it to load the new code"
  systemctl restart "$SERVICE_NAME"
  systemctl is-active --quiet "$SERVICE_NAME" \
    && echo "    restarted, still active" \
    || echo "    WARNING: did not come back up - journalctl -u $SERVICE_NAME -n 50"
fi

echo
echo "Installed. Next steps:"
echo "  1. Edit $CONFIG_DIR/exporter.env with the switch's real host/user/password"
echo "  2. systemctl restart $SERVICE_NAME"
echo "  3. journalctl -u $SERVICE_NAME -f     # watch logs"
echo "  4. curl http://localhost:\$(grep EXPORTER_PORT $CONFIG_DIR/exporter.env | cut -d= -f2 || echo 9101)/metrics"
