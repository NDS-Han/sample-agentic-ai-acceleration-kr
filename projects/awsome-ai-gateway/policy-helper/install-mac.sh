#!/usr/bin/env bash
# install-mac.sh — 빌드한 policy-helper 를 관리자 전용 폴더에 넣고 Claude Code 에 연결한다 (macOS).
#
#   sudo bash install-mac.sh dist/policy-helper-mac
#
# 순서: ① 전제 확인(root, plist 없음, 파일 둘) → ② /Library/Application Support/ClaudeCode 통째 백업
# (~/ph-backup-<시각>/; 주 파일이 있었는지도 기록) → ③ /Library/Application Support/PolicyHelper 에
# 바이너리(755)·baseline.json(644), root:wheel → ④ 설치본을 일반 사용자로 한 번 실행해 managedSettings
# 가 나오는지 확인(실패하면 여기서 멈춤 — 연결 전이라 Claude Code 는 그대로) → ⑤ managed-settings.json
# 에 policyHelper 블록(path·timeoutMs 10000·refreshIntervalMs 300000) 추가. 다음 Claude Code 실행부터 적용.
# 되돌리기: sudo bash rollback-mac.sh ~/ph-backup-<시각>
#
# Install the built policy-helper into an admin-only folder and wire Claude Code to it.
# Backs up the whole ClaudeCode dir first, smoke-tests the installed binary as the real
# user before touching managed-settings.json, then adds the policyHelper block.
set -euo pipefail
DIST="${1:?usage: sudo bash install-mac.sh <dist-dir with policy-helper + baseline.json>}"
CC="/Library/Application Support/ClaudeCode"
PH="/Library/Application Support/PolicyHelper"
TIMEOUT_MS=10000
REFRESH_MS=300000

echo "== 1/5 preconditions"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] || { echo "run via sudo from the normal user account" >&2; exit 1; }
[ -f "$DIST/policy-helper" ] && [ -f "$DIST/baseline.json" ] \
  || { echo "need $DIST/policy-helper and $DIST/baseline.json (run build.sh first)" >&2; exit 1; }
if defaults read com.anthropic.claudecode >/dev/null 2>&1; then
  echo "an MDM plist (com.anthropic.claudecode) is present: the file's policyHelper would be ignored. Stop." >&2
  exit 1
fi
USER_HOME=$(eval echo "~$SUDO_USER")
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$DIST/baseline.json" \
  || { echo "baseline.json is not valid JSON" >&2; exit 1; }

echo "== 2/5 backup $CC"
TS=$(date +%Y%m%d-%H%M%S)
BK="$USER_HOME/ph-backup-$TS"
mkdir -p "$BK"
if [ -d "$CC" ]; then cp -Rp "$CC" "$BK/ClaudeCode"; else echo "(no $CC yet)"; mkdir -p "$BK/ClaudeCode"; fi
if [ -f "$CC/managed-settings.json" ]; then echo yes > "$BK/had-managed-settings.json"; else echo no > "$BK/had-managed-settings.json"; fi
chown -R "$SUDO_USER" "$BK"
echo "backup: $BK (managed-settings.json existed before: $(cat "$BK/had-managed-settings.json"))"

echo "== 3/5 install to $PH (root:wheel)"
mkdir -p "$PH"
install -m 755 -o root -g wheel "$DIST/policy-helper" "$PH/policy-helper"
install -m 644 -o root -g wheel "$DIST/baseline.json" "$PH/baseline.json"
chown root:wheel "$PH"; chmod 755 "$PH"
xattr -dr com.apple.quarantine "$PH" 2>/dev/null || true
ls -la "$PH"

echo "== 4/5 smoke test as $SUDO_USER (reads that user's VK cache, writes that user's policy cache)"
set +e
sudo -u "$SUDO_USER" -H "$PH/policy-helper" > "$BK/smoke-stdout.json" 2> "$BK/smoke-stderr.txt"
RC=$?
set -e
echo "exit=$RC"; sed 's/^/  stderr: /' "$BK/smoke-stderr.txt"
python3 - "$BK/smoke-stdout.json" <<'PY' || { echo "helper output unusable — NOT wiring policyHelper. Claude Code is unchanged." >&2; exit 1; }
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ms = d.get("managedSettings")
if not isinstance(ms, dict) or not ms:
    sys.exit("no managedSettings object in stdout")
hub = [k for k in ("strictKnownMarketplaces", "enabledPlugins", "managedMcpServers", "permissions", "availableModels") if k in ms]
print("  managedSettings keys:", sorted(ms))
print("  server fragment keys seen:", hub or "none (baseline only — VPN off, not logged in, or no cache yet)")
PY
[ "$RC" = 0 ] || { echo "helper exited $RC — NOT wiring. Claude Code is unchanged." >&2; exit 1; }

echo "== 5/5 add policyHelper block to $CC/managed-settings.json"
mkdir -p "$CC"
python3 - "$CC/managed-settings.json" "$PH/policy-helper" "$TIMEOUT_MS" "$REFRESH_MS" <<'PY'
import json, os, sys
path, exe, timeout_ms, refresh_ms = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
doc = {}
if os.path.isfile(path):
    text = open(path, encoding="utf-8-sig").read().strip()
    doc = json.loads(text) if text else {}
    if not isinstance(doc, dict):
        sys.exit(f"{path}: top level is not a JSON object")
doc["policyHelper"] = {"path": exe, "timeoutMs": timeout_ms, "refreshIntervalMs": refresh_ms}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, ensure_ascii=False); f.write("\n")
os.replace(tmp, path)
print(json.dumps(doc["policyHelper"], indent=2))
PY
chown root:wheel "$CC/managed-settings.json"; chmod 644 "$CC/managed-settings.json"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CC/managed-settings.json"

cat <<MSG

DONE. policyHelper applies from the NEXT launch.
  check : open a new terminal, run: claude  → /status shows "Enterprise managed settings (helper)"
          /plugin and /mcp show what the server policy sent (a fragment needs VPN + a valid VK)
  cowork: quit Claude Desktop with Cmd+Q and reopen it, send one message
  undo  : sudo bash rollback-mac.sh "$BK"
MSG
