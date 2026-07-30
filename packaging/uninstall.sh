#!/usr/bin/env bash
# Stops and removes the s4048-exporter systemd service. Pass --purge to also
# delete /opt/s4048-exporter, /etc/s4048-exporter (including the credential
# file) and the service user.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo $0 [--purge])" >&2
  exit 1
fi

SERVICE_NAME=s4048-exporter
INSTALL_DIR=/opt/s4048-exporter
CONFIG_DIR=/etc/s4048-exporter
SERVICE_USER=s4048-exporter

systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
  id "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER"
  echo "Removed service, install dir, config (with credentials), and service user."
else
  echo "Removed service. $INSTALL_DIR and $CONFIG_DIR left in place - rerun with --purge to delete those too."
fi
