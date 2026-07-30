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
BUILD_VENV="$SCRIPT_DIR/.build-venv"
DIST_DIR="$SCRIPT_DIR"

python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" install --upgrade -q pip
"$BUILD_VENV/bin/pip" install -q pyinstaller -r "$EXPORTER_SRC/requirements.txt"

"$BUILD_VENV/bin/pyinstaller" \
  --onefile \
  --name s4048-exporter \
  --distpath "$DIST_DIR" \
  --workpath "$SCRIPT_DIR/.build-work" \
  --specpath "$SCRIPT_DIR/.build-work" \
  "$EXPORTER_SRC/exporter.py"

rm -rf "$SCRIPT_DIR/.build-work" "$BUILD_VENV"

echo
echo "Built: $DIST_DIR/s4048-exporter"
echo "Copy it (with install-binary.sh and exporter.env.example) to the LXC and run:"
echo "  sudo ./install-binary.sh"
