#!/usr/bin/env bash
# Build policy-helper for macOS (PyInstaller onedir) and put the site baseline next to it.
#
# Output: dist/policy-helper-mac/  (policy-helper + _internal/ + baseline.json)
# onedir, not onefile: a onefile binary unpacks its Python runtime into a fresh temp folder on
# every run, and macOS scans those new files each time — measured 7.6 s per start on macOS 26
# (2026-11-08), close to Claude Code's 10 s policyHelper timeout. onedir keeps the runtime at a
# fixed path, so it is scanned once. Same layout as the Windows twin, build.ps1.
#
# Install with install-mac.sh (copies the folder to /Library/Application Support/PolicyHelper/,
# root:wheel, and wires policyHelper.path to the binary there).
# The baseline holds site values (gateway URLs) and is never committed; make it with
# make-baseline-mac.py or start from baseline.example.json.
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

echo "== 2/4 PyInstaller onedir"
rm -rf "$HERE/dist/pyi" "$OUT"
.build-venv/bin/python -m PyInstaller --noconfirm --onedir --name policy-helper --paths src \
    --distpath "$HERE/dist/pyi" --workpath build --specpath build packaging/entry.py
mv "$HERE/dist/pyi/policy-helper" "$OUT"
rmdir "$HERE/dist/pyi"

echo "== 3/4 place baseline.json next to the binary"
cp "$BASELINE" "$OUT/baseline.json"

echo "== 4/4 smoke test (must print one envelope and exit 0)"
"$OUT/policy-helper" | "$PY" -c 'import json, sys
sys.exit(0 if isinstance(json.load(sys.stdin).get("managedSettings"), dict) else 1)' \
    || { echo "helper printed no managedSettings - check the baseline (see stderr above)" >&2; exit 1; }
echo "OK  $OUT/policy-helper  (+ _internal/)"
