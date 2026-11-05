#!/usr/bin/env bash
# Build policy-helper for macOS (PyInstaller onefile) and put the site baseline next to it.
#
# Output: dist/policy-helper-mac/policy-helper + baseline.json
# Install by copying that folder to an admin-only location and pointing Claude Code's
# policyHelper.path at the binary, e.g.
#   sudo mkdir -p "/Library/Application Support/PolicyHelper"
#   sudo cp dist/policy-helper-mac/policy-helper dist/policy-helper-mac/baseline.json \
#       "/Library/Application Support/PolicyHelper/"
#   sudo chown -R root:wheel "/Library/Application Support/PolicyHelper"
# The baseline holds site values (gateway URLs) and is never committed; start from
# baseline.example.json. Windows twin: build.ps1 (onedir).
#
# Usage: bash build.sh /path/to/baseline.json        (PYTHON=/path/to/python3.11+ to pick one)
set -euo pipefail
BASELINE="${1:?usage: bash build.sh /path/to/baseline.json}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
[ -f "$BASELINE" ] || { echo "baseline not found: $BASELINE" >&2; exit 1; }
PY="${PYTHON:-python3}"
OUT="$HERE/dist/policy-helper-mac"

echo "== 1/4 build venv (Python 3.11+)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "Python 3.11+ is required (PYTHON=... to choose one)" >&2; exit 1; }
[ -d .build-venv ] || "$PY" -m venv .build-venv
.build-venv/bin/python -m pip install --quiet --upgrade pip pyinstaller

echo "== 2/4 PyInstaller onefile"
.build-venv/bin/python -m PyInstaller --noconfirm --onefile --name policy-helper --paths src \
    --distpath "$OUT" --workpath build --specpath build packaging/entry.py

echo "== 3/4 place baseline.json next to the binary"
cp "$BASELINE" "$OUT/baseline.json"

echo "== 4/4 smoke test (must print one envelope and exit 0)"
"$OUT/policy-helper" | "$PY" -c 'import json, sys
sys.exit(0 if isinstance(json.load(sys.stdin).get("managedSettings"), dict) else 1)' \
    || { echo "helper printed no managedSettings - check the baseline (see stderr above)" >&2; exit 1; }
echo "OK  $OUT/policy-helper"
