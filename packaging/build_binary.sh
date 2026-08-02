#!/usr/bin/env bash
# Builds a standalone s4048-exporter executable with PyInstaller.
#
# Run this on a machine with the same OS/architecture (and ideally the same
# or older glibc) as the target LXC - the binary bundles native libraries
# (cryptography/bcrypt/libssl) that are not portable across wildly different
# distros. Building it *inside* a throwaway copy of the target LXC's
# template is the safest option.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTER_SRC="$SCRIPT_DIR/../exporter"
COMMON_SRC="$SCRIPT_DIR/../common"
BUILD_VENV="$SCRIPT_DIR/.build-venv"
DIST_DIR="$SCRIPT_DIR"

# python3-venv: same "import venv succeeds but pip doesn't get installed"
# gotcha as install.sh - always ensured, not just checked for.
# binutils: PyInstaller needs objdump on Linux to analyze bundled native
# libraries (cryptography/bcrypt/libssl); without it this fails partway
# through with "ERROR: On Linux, objdump is required" (confirmed live on
# a fresh minimal Debian/Ubuntu LXC, which doesn't ship it by default).
if [[ $EUID -eq 0 ]]; then
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip binutils
else
  echo "==> Ensuring python3-venv/python3-pip/binutils are installed (needs sudo)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip binutils
fi

python3 -m venv "$BUILD_VENV"
if [[ ! -x "$BUILD_VENV/bin/pip" ]]; then
  echo "venv at $BUILD_VENV has no pip - python3-venv likely failed to install above." >&2
  echo "Try: rm -rf $BUILD_VENV && re-run this script." >&2
  exit 1
fi
"$BUILD_VENV/bin/pip" install --upgrade -q pip
"$BUILD_VENV/bin/pip" install -q pyinstaller -r "$EXPORTER_SRC/requirements.txt"

# exporter.py does bare imports (`from db import Database`, `from
# ssh_client import ...`, etc.) of files that now live in ../common, not
# next to exporter.py - PyInstaller's import analysis needs --paths to
# find them, the same way they'd need to be on PYTHONPATH to run
# un-bundled (confirmed live: a build without this silently omitted
# db.py/store.py/devices.py/ssh_client.py/metrics.py from the binary).
"$BUILD_VENV/bin/pyinstaller" \
  --onefile \
  --name s4048-exporter \
  --paths "$COMMON_SRC" \
  --distpath "$DIST_DIR" \
  --workpath "$SCRIPT_DIR/.build-work" \
  --specpath "$SCRIPT_DIR/.build-work" \
  "$EXPORTER_SRC/exporter.py"

rm -rf "$SCRIPT_DIR/.build-work" "$BUILD_VENV"

# devices.yaml is data, not code - not bundled into the binary, just
# copied alongside it (see exporter.py's sys.frozen handling for why it
# looks next to the binary, not in PyInstaller's temp extraction dir).
cp "$COMMON_SRC/devices.yaml" "$DIST_DIR/devices.yaml"

echo
echo "Built: $DIST_DIR/s4048-exporter"
echo "Copy it (with install-binary.sh, exporter.env.example, and devices.yaml)"
echo "to the LXC and run:"
echo "  sudo ./install-binary.sh"
