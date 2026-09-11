#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# gw.sh — 로컬 격리 컨테이너로 LLM Gateway 에 붙어 claude-code / codex 를 쓰는 헬퍼.
# 호스트 맥 환경(~/.claude, ~/.codex, 셸 설정)을 전혀 건드리지 않는다.
#
# 사용법:
#   ./gw.sh build                 # claude-box / codex-box 이미지 빌드 (최초 1회)
#   ./gw.sh vk                    # 호스트에서 VK 발급(OIDC 로그인) → ~/.gateway-vk 에 저장
#   ./gw.sh claude [args...]      # claude-box 컨테이너에서 claude 실행 (현재 디렉터리 마운트)
#   ./gw.sh codex  [args...]      # codex-box 컨테이너에서 codex 실행
#   ./gw.sh shell claude|codex    # 컨테이너 셸 진입(디버그)
#
# codex 모델/plane 선택(설정된 것만 컨테이너로 전달):
#   GW_PLANE=mantle|runtime (기본 mantle)  GW_TIER=sol|terra|luna (기본 terra)
#   GW_MODEL=<alias> 전체 override, GW_CONTEXT_WINDOW=<int>
#   예: GW_PLANE=runtime ./gw.sh codex "버그 고쳐줘"   # 표준 runtime plane(gpt-5.6-terra)
#
# VK 는 ~/.gateway-vk 파일(plain VK 문자열, 600)에서 읽는다. 1시간 만료 → 만료 시 `gw.sh vk` 재실행.
set -euo pipefail

# ── 설정 (게이트웨이 접속 좌표 — 아래 값을 채우거나 환경변수로 오버라이드) ──────────
GW_URL="${GW_URL:-<GATEWAY_URL>}"
ADMIN_URL="${ADMIN_URL:-<ADMIN_API_URL>}"
OIDC_ISSUER_URL="${OIDC_ISSUER_URL:-https://cognito-idp.ap-northeast-2.amazonaws.com/<COGNITO_USER_POOL_ID>}"
OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-<OIDC_CLIENT_ID>}"
VK_FILE="$HOME/.gateway-vk"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# docker 또는 finch
ENGINE="${GW_ENGINE:-docker}"; command -v "$ENGINE" >/dev/null 2>&1 || ENGINE=finch

read_vk() {
  [ -f "$VK_FILE" ] || { echo "VK 없음. 먼저 './gw.sh vk' 실행." >&2; exit 1; }
  cat "$VK_FILE"
}

# codex-box 의 모델 선택 knob 을 호스트 → 컨테이너로 전달한다.
#
# `docker run` 은 env 를 자동 상속하지 않으므로(-e 로 명시한 것만 들어간다), 이 목록이
# 없으면 호스트에서 `GW_PLANE=runtime ./gw.sh codex` 를 해도 컨테이너 안에선 조용히
# 기본값(mantle)이 쓰인다 — plane 을 바꿨다고 믿는데 과금은 다른 plane 에 찍히는 상황.
# 설정된 것만 넘겨서, 미설정 시 entrypoint.sh 의 기본값이 그대로 유효하게 둔다.
# 의미는 codex-box/entrypoint.sh 상단 주석 참조.
CODEX_MODEL_KNOBS=(GW_PLANE GW_TIER GW_MODEL GW_CONTEXT_WINDOW)

# CODEX_ENV_ARGS 배열을 채운다. 값에 공백이 있어도 깨지지 않게 배열로 전달하고,
# `$(...)` 로 문자열을 되받지 않는다(그러면 다시 word splitting 에 의존하게 된다).
# 호출부는 `${CODEX_ENV_ARGS[@]+"${CODEX_ENV_ARGS[@]}"}` 형태로 펼친다 — knob 이 하나도
# 설정되지 않으면 빈 배열이고, `set -u` 아래에서 빈 배열을 `"${arr[@]}"` 로 펼치면
# bash < 4.4 가 unbound variable 로 죽는다.
CODEX_ENV_ARGS=()
set_codex_env_args() {
  CODEX_ENV_ARGS=()
  local k
  for k in "${CODEX_MODEL_KNOBS[@]}"; do
    if [ -n "${!k:-}" ]; then CODEX_ENV_ARGS+=(-e "$k=${!k}"); fi
  done
}

# 게이트웨이 /v1/usage/me 폴링 → 예산/사용량 한 줄 출력(stderr). claude/codex 실행 전 표시.
# Codex CLI 는 statusline 훅이 없어, 이 방식으로 예산을 보여준다(claude-box 는 자체 statusline 도 있음).
print_budget() {
  local VK; VK="$(cat "$VK_FILE" 2>/dev/null)"; [ -z "$VK" ] && return 0
  # 예산 한 줄을 stderr 로 출력(claude/codex 출력과 안 섞이게). 내부 트레이스만 버린다.
  python3 - "$GW_URL" "$VK" <<'PY' || true
import sys, json, urllib.request
gw, vk = sys.argv[1], sys.argv[2]
try:
    req = urllib.request.Request(gw.rstrip('/')+"/v1/usage/me", headers={"Authorization":"Bearer "+vk})
    d = json.load(urllib.request.urlopen(req, timeout=4))
except Exception:
    sys.exit(0)
u=d.get("usage",{}); b=d.get("budget",{})
cost=float(u.get("total_cost_usd",0) or 0); tok=int(u.get("total_tokens",0) or 0)
maxb=float(b.get("max_usd",0) or 0); rem=float(b.get("remaining_usd",0) or 0)
seg = (f"예산 ${rem:.2f}/${maxb:.0f} 남음 ({b.get('pct',0):.0f}% 사용)" if maxb>0
       else f"소진 ${cost:.4f} (예산 미설정)")
print(f"🛡 gateway {d.get('period','')} · {seg} · {tok:,} tok", file=sys.stderr)
PY
}

case "${1:-}" in
  budget)
    [ -f "$VK_FILE" ] || { echo "VK 없음. './gw.sh vk' 먼저." >&2; exit 1; }
    print_budget
    # 모델별 breakdown 도 같이
    VK="$(cat "$VK_FILE")" python3 - "$GW_URL" <<'PY' 2>/dev/null || true
import sys, os, json, urllib.request
gw=sys.argv[1]; vk=os.environ["VK"]
req=urllib.request.Request(gw.rstrip('/')+"/v1/usage/me", headers={"Authorization":"Bearer "+vk})
d=json.load(urllib.request.urlopen(req,timeout=5))
for m in d.get("model_breakdown",[]):
    print(f"   {m['model']}: ${float(m['cost_usd']):.4f} ({m['requests']}회, in {m['input_tokens']} / out {m['output_tokens']})")
PY
    ;;
  build)
    echo "[build] claude-box ..."; "$ENGINE" build -t claude-box "$HERE/claude-box"
    echo "[build] codex-box ...";  "$ENGINE" build -t codex-box  "$HERE/codex-box"
    echo "✅ 빌드 완료 (claude-box, codex-box)"
    ;;

  vk)
    # 호스트(맥)에서 OIDC 로그인 → VK 발급. gateway-cli 가 있으면 그걸로, 없으면 안내.
    if command -v gateway-cli >/dev/null 2>&1; then
      gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
      VK="$(python3 - "$ADMIN_URL" <<'PY'
import json,os,sys,urllib.error,urllib.request
# id_token 을 보낸다 (access_token 아님). 이유:
#  1) 사용자 신원 claim(email/name/groups)이 id_token 에만 있다. Cognito access_token
#     에는 email 이 없어 admin-api 가 <sub>@unknown 으로 프로비저닝한다.
#  2) Entra ID 는 access_token 의 aud 가 리소스(예: MS Graph)로 발급되므로
#     admin-api 의 OIDC_AUDIENCE(=client_id) 검증에 걸려 401 이 된다.
# admin-api 도 id_token 을 전제로 동작한다
# (core/oidc_verifier.py 의 verify_at_hash=False 주석 참조).
p=os.path.expanduser("~/.gateway-cli/oidc-tokens.json")
try:
    d=json.load(open(p))
except FileNotFoundError:
    sys.exit(f"토큰 캐시 없음: {p} — 'gateway-cli login' 이 실패했습니다.")
tok=d.get("id_token") or ""
if not tok:
    sys.exit("id_token 이 없습니다. IdP 앱 등록에 'openid' scope 가 있는지 확인하세요.")
req=urllib.request.Request(sys.argv[1]+"/v1/auth/exchange",data=b"{}",method="POST",
  headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
try:
    print(json.load(urllib.request.urlopen(req,timeout=15))["virtual_key"])
except urllib.error.HTTPError as e:
    # 401/403 을 조용히 삼키면 "왜 안 되는지" 알 수 없다. 본문을 그대로 노출.
    sys.exit(f"exchange 실패 HTTP {e.code}: {e.read().decode('utf-8','replace')[:500]}")
PY
)"
      printf '%s' "$VK" > "$VK_FILE"; chmod 600 "$VK_FILE"
      echo "✅ VK 발급·저장 ($VK_FILE). 길이 ${#VK}. (1시간 만료 — 만료 시 './gw.sh vk' 재실행)"
    else
      echo "gateway-cli 가 호스트에 없습니다. 설치: uv tool install --from <repo>/gateway-cli gateway-cli" >&2
      echo "또는 발급한 VK 문자열을 직접 저장: printf '%s' '<VK>' > $VK_FILE && chmod 600 $VK_FILE" >&2
      exit 1
    fi
    ;;

  claude)
    shift
    VK="$(read_vk)"
    print_budget   # 실행 전 예산 한 줄 + claude-box 자체 statusline 도 표시됨
    exec "$ENGINE" run -it --rm \
      -e ANTHROPIC_BASE_URL="$GW_URL" \
      -e ANTHROPIC_AUTH_TOKEN="$VK" \
      -v "$PWD":/work \
      claude-box "$@"
    ;;

  codex)
    shift
    VK="$(read_vk)"
    print_budget   # Codex 는 statusline 훅이 없어 실행 전 예산을 여기서 표시
    set_codex_env_args
    exec "$ENGINE" run -it --rm \
      -e GW_URL="$GW_URL" \
      -e GATEWAY_VK="$VK" \
      ${CODEX_ENV_ARGS[@]+"${CODEX_ENV_ARGS[@]}"} \
      -v "$PWD":/work \
      codex-box "$@"
    ;;

  shell)
    box="${2:-claude}"
    VK="$(read_vk)"
    if [ "$box" = "codex" ]; then
      # 디버그 셸에서도 같은 knob 을 넘긴다 — 셸 안에서 entrypoint.sh 를 직접 실행해
      # 생성되는 config.toml 을 확인하는 것이 이 서브커맨드의 용도라서.
      set_codex_env_args
      exec "$ENGINE" run -it --rm --entrypoint /bin/sh -e GW_URL="$GW_URL" -e GATEWAY_VK="$VK" \
        ${CODEX_ENV_ARGS[@]+"${CODEX_ENV_ARGS[@]}"} -v "$PWD":/work codex-box
    else
      exec "$ENGINE" run -it --rm --entrypoint /bin/bash -e ANTHROPIC_BASE_URL="$GW_URL" -e ANTHROPIC_AUTH_TOKEN="$VK" -v "$PWD":/work claude-box
    fi
    ;;

  *)
    # 헤더 주석 블록 전체를 usage 로 출력. 고정 행 범위(예전의 `sed -n '4,18p'`)를 쓰면
    # 헤더에 한 줄 추가할 때마다 usage 가 조용히 잘린다 — 실제로 그렇게 잘려 있었다.
    awk 'NR > 3 { if ($0 ~ /^#/) print; else exit }' "$HERE/gw.sh"
    ;;
esac
