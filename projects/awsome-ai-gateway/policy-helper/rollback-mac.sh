#!/usr/bin/env bash
# rollback-mac.sh — install-mac.sh 가 만든 백업으로 Claude Code 연결을 되돌린다 (macOS).
#
#   sudo bash rollback-mac.sh ~/ph-backup-<시각>
#
# 백업 때 managed-settings.json 이 있었으면 그 파일을 복원하고, 없었으면 지금 파일을 지운다
# (드롭인은 손대지 않았으므로 그대로). PolicyHelper 폴더는 남겨 둔다 — 연결이 풀리면 실행되지 않는다.
# 다음 Claude Code 실행부터 /status 의 (helper) 가 사라진다.
#
# Undo install-mac.sh from its backup folder: restore managed-settings.json if it existed,
# otherwise delete the one install-mac.sh created. Leaves the PolicyHelper folder in place.
set -euo pipefail
BK="${1:?usage: sudo bash rollback-mac.sh <backup dir from install-mac.sh>}"
CC="/Library/Application Support/ClaudeCode"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -f "$BK/had-managed-settings.json" ] || { echo "$BK is not an install-mac.sh backup" >&2; exit 1; }
if [ "$(cat "$BK/had-managed-settings.json")" = yes ]; then
  install -m 644 -o root -g wheel "$BK/ClaudeCode/managed-settings.json" "$CC/managed-settings.json"
  echo "restored $CC/managed-settings.json from backup:"
  cat "$CC/managed-settings.json"
else
  rm -f "$CC/managed-settings.json"
  echo "removed $CC/managed-settings.json (it did not exist before the install)"
fi
echo "PolicyHelper folder left in place: /Library/Application Support/PolicyHelper (remove with: sudo rm -rf it)"
echo "new Claude Code sessions no longer run the helper; Cowork: Cmd+Q and reopen"
