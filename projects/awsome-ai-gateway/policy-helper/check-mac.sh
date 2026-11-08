#!/usr/bin/env bash
# check-mac.sh — policy-helper 설치 전, 이 Mac 의 상태를 읽기만 해서 보여 준다 (수정 없음).
# 비밀값은 찍지 않는다: VK 캐시는 존재·수정 시각·admin_api_url·만료·키의 sha256 앞 12자리만.
# 출력을 통째로 기준값(baseline)을 만드는 사람에게 전달한다. 실행: bash check-mac.sh
#
# Read-only inventory of this Mac before installing policy-helper. Prints no secret
# values (VK caches: existence, mtime, admin_api_url, expiry, sha256 prefix only).
# Usage: bash check-mac.sh
set -u
CC="/Library/Application Support/ClaudeCode"
PH="/Library/Application Support/PolicyHelper"
say() { printf '\n== %s\n' "$*"; }

say "system"
sw_vers 2>/dev/null; uname -m; echo "user: $(id -un)  home: $HOME"

say "claude (Claude Code CLI)"
command -v claude || echo "claude: not on PATH"
claude --version 2>/dev/null || true

say "Claude Desktop"
defaults read /Applications/Claude.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null \
  || echo "Claude.app not found"

say "MDM plist — must be ABSENT, or the file's policyHelper block is ignored"
defaults read com.anthropic.claudecode 2>&1 | head -20
ls /Library/Managed\ Preferences/ 2>/dev/null | grep -i anthropic || echo "no managed preference for claudecode"

say "managed settings directory"
ls -la "$CC" 2>&1
ls -la "$CC/managed-settings.d" 2>&1
for f in "$CC"/managed-settings.json "$CC"/managed-settings.d/*.json "$CC"/managed-mcp.json; do
  [ -f "$f" ] || continue
  say "FILE $f"
  cat "$f"; echo
done

say "existing PolicyHelper install (expected: absent)"
ls -la "$PH" 2>&1

say "VK caches — values are NOT printed"
for p in "$HOME/Library/Application Support/gateway-cli/vk-cache.json" "$HOME/.gateway-cli/vk-cache.json"; do
  if [ -f "$p" ]; then
    echo "$p"
    stat -f '  mtime %Sm  size %z' "$p"
    python3 - "$p" <<'PY'
import hashlib, json, sys, time
d = json.load(open(sys.argv[1], encoding="utf-8"))
vk = d.get("virtual_key") if isinstance(d.get("virtual_key"), str) else ""
exp = d.get("expires_at")
try:
    left = float(exp) - time.time()
    state = "valid, %dh left" % (left // 3600) if left > 0 else "EXPIRED"
except (TypeError, ValueError):
    state = "unreadable expires_at (%r)" % (exp,)
print("  keys:", sorted(d))
print("  admin_api_url:", d.get("admin_api_url"))
print("  expires_at:", exp, "->", state)
print("  virtual_key sha256[:12]:", hashlib.sha256(vk.encode()).hexdigest()[:12] if vk else "-")
PY
  else
    echo "$p: absent"
  fi
done

say "gateway-cli login (legacy ~/.gateway-cli) — email + expiry only"
ls -la "$HOME/.gateway-cli" 2>&1
T="$HOME/.gateway-cli/oidc-tokens.json"
if [ -f "$T" ]; then
  python3 - "$T" <<'PY'
import base64, json, sys, time
d = json.load(open(sys.argv[1], encoding="utf-8"))
tok = d.get("id_token") or ""
try:
    p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
    c = json.loads(base64.urlsafe_b64decode(p))
    print("  id_token email:", c.get("email"), " exp:", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c["exp"])))
except Exception as e:
    print("  id_token: not decodable (%s)" % type(e).__name__)
try:
    print("  refresh expires_at:", d.get("expires_at"))
except Exception:
    pass
PY
fi

say "gateway-cli programs"
for c in api-key-helper gateway-cli statusline; do
  printf '%s: ' "$c"; command -v "$c" || echo "not on PATH"
done
ls -la "$HOME/.local/bin/api-key-helper" 2>&1

say "related environment variables in this shell (names only, except URLs)"
env | grep -E '^(GATEWAY_CLI_|OIDC_|ADMIN_API_URL|ANTHROPIC_)' \
  | sed -E 's/^(ANTHROPIC_BASE_URL|ADMIN_API_URL|GATEWAY_CLI_GATEWAY_URL|OIDC_ISSUER_URL)=(.*)$/\1=\2/; t; s/=.*/=<set>/' \
  || echo "(none)"

say "build tools — build.sh needs Python 3.11+ (PYTHON=... to choose)"
for c in uv conda python3; do printf '%s: ' "$c"; command -v "$c" || echo "not on PATH"; done
python3 --version 2>&1
ls /opt/homebrew/bin/python3.1* /usr/local/bin/python3.1* 2>/dev/null || echo "no homebrew python3.1x"
uv python list --only-installed 2>/dev/null | head -5
conda env list 2>/dev/null | head -8

say "done"
