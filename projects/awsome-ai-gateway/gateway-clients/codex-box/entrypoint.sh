#!/bin/sh
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# GW_URL(게이트웨이 ALB) + GATEWAY_VK(VK) env 로부터 codex config 를 생성하고 codex 실행.
#
# ── 모델 선택 (GW_PLANE / GW_TIER / GW_MODEL) ────────────────────────────────
# GPT-5.6 은 **두 개의 Bedrock plane** 으로 서비스되고, 게이트웨이 카탈로그엔 plane 별로
# 별도 alias 가 등록돼 있다. 같은 모델이지만 alias 가 다르면 다른 plane·다른 과금 행이다.
#
#   plane    | alias (tier=terra 예시)  | provider_model_id       | 인증        | migration
#   ---------+--------------------------+-------------------------+-------------+----------
#   mantle   | codex-gpt-5.6-terra      | openai.gpt-5.6-terra    | Bearer(단기)| 0025
#   runtime  | gpt-5.6-terra            | us.openai.gpt-5.6-terra | SigV4 + CRIS| 0032
#
# tier 는 sol(코딩/agentic) | terra(균형, 기본) | luna(경량) 3종이며 두 plane 모두 동일.
#
# ⚠️ **예전 주석의 `GW_MODEL=gpt-5.6-terra` 권고는 이제 의미가 달라졌다.** 0032 이전엔
# 그 문자열이 카탈로그에 없어서 서버가 무시하고 routing profile 의 default_model(Mantle)
# 로 라우팅했다 — 즉 "Codex 메타데이터만 5.6 으로 맞추는" 용도였다. 0032 부터는 그것이
# 실재하는 runtime plane alias 이므로 **plane 자체가 바뀐다**(과금 행·invocation log 포함).
# 그래서 이 스크립트는 plane 을 문자열에 숨기지 않고 GW_PLANE 으로 명시하게 한다.
#
# 서버 동작(routers/openai_compat.py `_resolve_model`): 클라이언트 model 이 두 plane 중
# 하나의 ACTIVE alias 로 resolve 되면 **그 값이 이긴다**. resolve 안 되면 default_model 로
# 폴백하고 `responses_requested_model_unresolved_using_default` 를 INFO 로 남긴다(=오타나
# 미등록 alias 를 서버 로그에서 진단하는 경로). status=INACTIVE 인 alias 는 폴백 없이 404.
# VK scope 는 별개로 계속 검사된다 — codex-gpt 만 허용된 VK 는 GPT-5.6 을 요청해도 못 쓴다.
#
# 기본값을 mantle 로 두는 이유: 기존 codex-box 사용자의 plane·과금·VK scope 를 바꾸지
# 않는다. mantle alias 가 없는 구 환경(0025 미적용)에서도 위 폴백 덕에 그대로 동작한다.
# runtime plane 으로 옮기려면 `GW_PLANE=runtime` (invocation log 로 본문 감사 가능).
set -eu

: "${GW_URL:?GW_URL 환경변수가 필요합니다 (게이트웨이 ALB)}"
: "${GATEWAY_VK:?GATEWAY_VK 환경변수가 필요합니다 (Virtual Key)}"

PLANE="${GW_PLANE:-mantle}"
TIER="${GW_TIER:-terra}"

# tier 를 검증한다. 오타(예: GW_TIER=tera)는 미등록 alias 가 되어 서버가 조용히
# default_model 로 폴백하므로, 클라이언트가 의도한 모델이 아닌 것을 쓰면서도 200 이 난다.
# 여기서 즉시 실패시키는 편이 진단 가능하다.
case "$TIER" in
  sol|terra|luna) ;;
  *) echo "codex-box: GW_TIER='$TIER' 은 유효하지 않습니다 (sol|terra|luna)" >&2; exit 2 ;;
esac

case "$PLANE" in
  mantle)  ALIAS="codex-gpt-5.6-${TIER}"; PROVIDER_NAME="LLM Gateway (Mantle GPT-5.6 ${TIER})" ;;
  runtime) ALIAS="gpt-5.6-${TIER}";       PROVIDER_NAME="LLM Gateway (Bedrock runtime GPT-5.6 ${TIER})" ;;
  *) echo "codex-box: GW_PLANE='$PLANE' 은 유효하지 않습니다 (mantle|runtime)" >&2; exit 2 ;;
esac

# GW_MODEL 은 전체 override(위 plane/tier 조합을 무시). 카탈로그에 운영자가 직접 추가한
# alias 를 쓰거나, 폴백을 노려 일부러 미등록 문자열을 보낼 때 사용한다.
if [ -n "${GW_MODEL:-}" ]; then
  if [ -n "${GW_PLANE:-}${GW_TIER:-}" ]; then
    echo "codex-box: GW_MODEL 이 설정되어 GW_PLANE/GW_TIER 는 무시됩니다" >&2
  fi
  ALIAS="$GW_MODEL"
  PROVIDER_NAME="LLM Gateway (${GW_MODEL})"
  # plane 은 서버가 alias 로 결정하므로, override 시엔 우리가 안다고 주장하지 않는다.
  PLANE="override"
fi

# Codex 내장 모델 테이블 키는 'gpt-5.6-sol|terra|luna' 로, **runtime plane alias 와 정확히
# 동일하다**(codex-cli 0.149.1 바이너리 실측). 따라서 mantle alias(codex-gpt-5.6-*)를 주면
# "Model metadata not found" 경고와 함께 기본값으로 폴백한다. 그 폴백의 실질적 피해는
# context window 오인이므로 `model_context_window` 로 직접 준다 — 같은 실측에서 확인된
# 지원 키다(`model_max_output_tokens` 는 0.149.1 에 존재하지 않아 줄 수 없다).
# 272000 = GPT-5.6 context_window(Codex 메타데이터 실측). 상한 872000 은 컨텍스트 밴드
# 과금 구간이라 기본값으로 열지 않는다 — 272K 초과분은 별도 밴드 단가로 청구된다.
CONTEXT_WINDOW="${GW_CONTEXT_WINDOW:-272000}"

echo "codex-box: plane=${PLANE} model=${ALIAS} context_window=${CONTEXT_WINDOW}" >&2

mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<TOML
model = "${ALIAS}"
model_provider = "gateway"
model_context_window = ${CONTEXT_WINDOW}

[model_providers.gateway]
name = "${PROVIDER_NAME}"
base_url = "${GW_URL}/v1"
wire_api = "responses"
env_key = "GATEWAY_VK"
TOML

# codex 가 originator 헤더(codex_cli_rs)를 자동으로 보냄 → 게이트웨이가 client=codex 로 식별.
exec codex "$@"
