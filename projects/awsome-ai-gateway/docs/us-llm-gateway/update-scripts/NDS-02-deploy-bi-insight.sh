#!/usr/bin/env bash
# ==============================================================================
# NDS-02-deploy-bi-insight.sh — BI Insight(admin-chat-agent) 배포/제거
# ------------------------------------------------------------------------------
# nds/dev 전용(NCR — upstream 대상 아님): BI Insight는 AgentCore Runtime 위의
# 별도 배포물이다. terraform 모듈(infra) + arm64 컨테이너 이미지 + agent runtime
# 생성 + values 배선 4단계가 필요하다.
#
# 하는 일:
#   apply  : ① lambdas 빌드(query_db/get_schema zip 산출물)
#            ② tfvars enable_chat_agent/enable_chat_db_tools=true
#            ③ terraform apply -target=module.agentcore_runtime (ECR·S3·IAM·Lambda)
#            ④ admin-chat-agent 이미지 arm64 빌드 → ECR push (immutable 태그)
#            ⑤ agent runtime 생성/갱신 → READY 대기
#            ⑥ admin-api IRSA role 에 bedrock-agentcore:InvokeAgentRuntime 부여
#               (terraform 모듈이 안 해주므로 여기서 인라인 정책으로 부여)
#            ⑦ values 주입 — adminApi.env.{AGENTCORE_RUNTIME_ARN,
#              AGENTCORE_REGION, CHAT_STAGING_BUCKET}, adminUi.env.CHAT_ENABLED=true
#   delete : invoke 정책 제거 → runtime 삭제 → tfvars 두 플래그 false →
#            terraform apply(모듈 파괴) → values 의 위 키 정리(CHAT_ENABLED=false)
#   status : 기본 — tfvars/런타임/values 배선 현재 상태만 표시
#
# 사용법:
#   ./NDS-02-deploy-bi-insight.sh            # status
#   ./NDS-02-deploy-bi-insight.sh --apply    # 전체 배포 (terraform apply 는 대화형 확인)
#   ./NDS-02-deploy-bi-insight.sh --delete   # runtime + 인프라 제거 + values 정리
#
# values 변경분은 install-eks.sh <env> 로 반영된다. CHAT_ENABLED=false 면
# admin-ui 가 사이드바 BI Insight 메뉴·퀵챗·/chat 페이지를 숨긴다(layout.tsx).
#
# 주의: _lib.sh 의 discover() 는 마지막 가드([ -z ] && die)가 성공해도 status 1 을
# 반환하는 기존 결함이 있어, 형제 스크립트와 마찬가지로 -e 없이 -uo pipefail 만 쓴다.
# 대신 변경을 가하는 호출은 전부 `|| die` 를 명시한다.
# ==============================================================================
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

APPLY=0; DELETE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)  APPLY=1;  shift ;;
    --delete) DELETE=1; shift ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[ "$APPLY" = 1 ] && [ "$DELETE" = 1 ] && die "--apply 와 --delete 는 동시에 쓸 수 없습니다"

require_env
command -v yq >/dev/null 2>&1 || die "yq not found — update-scripts/21 스크립트와 같은 도구 요구사항"
command -v terraform >/dev/null 2>&1 || die "terraform not found"
command -v docker >/dev/null 2>&1 || die "docker not found (arm64 이미지 빌드에 필요)"

REPO_ROOT=$(cd "$LIB_DIR/../../.." && pwd)
AGENT_DIR="$REPO_ROOT/admin-chat-agent"
TF_DIR="$REPO_ROOT/deployment/terraform/environments/llm-gateway-${DEPLOY_ENV}"
TFVARS="$TF_DIR/terraform.tfvars"
VALUES="$REPO_ROOT/deployment/charts/llm-gateway/values-eks-fargate-${DEPLOY_ENV}.yaml"
[ -d "$TF_DIR" ]    || die "terraform env 없음: $TF_DIR"
[ -f "$TFVARS" ]    || die "tfvars 없음: $TFVARS"
[ -f "$VALUES" ]    || die "values 파일 없음: $VALUES"
[ -d "$AGENT_DIR" ] || die "agent 소스 없음: $AGENT_DIR"

tf_out() { (cd "$TF_DIR" && terraform output -raw "$1" 2>/dev/null || true); }
tf_flag() { sed -n "s/^${1}[[:space:]]*=[[:space:]]*//p" "$TFVARS" | tr -d ' ' | head -1; }
tf_set()  { sed -i "s/^${1}[[:space:]]*=.*/${1}    = ${2}/" "$TFVARS" || die "tfvars 편집 실패: $1"; }

# ── 현재 상태 수집 ────────────────────────────────────────────────────────────
FLAG_AGENT=$(tf_flag enable_chat_agent)
FLAG_TOOLS=$(tf_flag enable_chat_db_tools)
ECR_URL=$(tf_out chat_agent_ecr_url)
ROLE_ARN=$(tf_out chat_agent_execution_role_arn)
STAGING=$(tf_out chat_agent_staging_bucket)
AGENT_NAME=$(tf_out chat_agent_name)          # 하이픈 — runtime 이름은 언더스코어로
RT_NAME=$(printf '%s' "$AGENT_NAME" | tr '-' '_')

# admin-api IRSA role — terraform output 우선, 없으면 명명 규칙으로 폴백.
ADMIN_ROLE_ARN=$(tf_out admin_api_role_arn)
: "${ADMIN_ROLE_ARN:=arn:aws:iam::${AWS_ACCOUNT_ID}:role/llm-gateway-${DEPLOY_ENV}-admin-api}"
ADMIN_ROLE_NAME="${ADMIN_ROLE_ARN##*/}"
INVOKE_POLICY="BIInsightInvoke"

# AgentCore runtime 조회 — 이름으로 찾는다(ARN 은 생성 후에야 존재).
RT_ID=""; RT_STATUS="미배포"; RT_ARN=""
if [ -n "$RT_NAME" ]; then
  RT_JSON=$(aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
      --query "agentRuntimes[?agentRuntimeName=='$RT_NAME']|[0]" --output json 2>/dev/null || echo "null")
  if [ "$RT_JSON" != "null" ] && [ -n "$RT_JSON" ]; then
    RT_ID=$(printf '%s' "$RT_JSON" | yq -r '.agentRuntimeId // ""')
    RT_STATUS=$(printf '%s' "$RT_JSON" | yq -r '.status // "?"')
    RT_ARN=$(printf '%s' "$RT_JSON" | yq -r '.agentRuntimeArn // ""')
  fi
fi

INVOKE_STATE=$(aws iam get-role-policy --role-name "$ADMIN_ROLE_NAME" \
    --policy-name "$INVOKE_POLICY" --query 'PolicyName' --output text 2>/dev/null || echo "미부여")

VAL_ARN=$(yq -r '.adminApi.env.AGENTCORE_RUNTIME_ARN // "-"' "$VALUES")
VAL_REGION=$(yq -r '.adminApi.env.AGENTCORE_REGION // "-"' "$VALUES")
VAL_BUCKET=$(yq -r '.adminApi.env.CHAT_STAGING_BUCKET // "-"' "$VALUES")
VAL_UI=$(yq -r '.adminUi.env.CHAT_ENABLED // "-"' "$VALUES")

hdr "BI Insight (admin-chat-agent) — ${DEPLOY_ENV}"
printf "  tfvars   enable_chat_agent=%s  enable_chat_db_tools=%s\n" "${FLAG_AGENT:-?}" "${FLAG_TOOLS:-?}"
printf "  tf-out   ecr=%s\n" "${ECR_URL:-미생성}"
printf "  tf-out   role=%s  staging=%s  name=%s\n" "${ROLE_ARN:-미생성}" "${STAGING:-미생성}" "${AGENT_NAME:-미생성}"
printf "  runtime  %s (id=%s)\n" "$RT_STATUS" "${RT_ID:-없음}"
printf "  invoke   %s:%s — %s\n" "$ADMIN_ROLE_NAME" "$INVOKE_POLICY" "$INVOKE_STATE"
printf "  values   adminApi.AGENTCORE_RUNTIME_ARN=%s\n" "${VAL_ARN:-(빈 값)}"
printf "  values   adminApi.AGENTCORE_REGION=%s  CHAT_STAGING_BUCKET=%s\n" "$VAL_REGION" "$VAL_BUCKET"
printf "  values   adminUi.CHAT_ENABLED=%s\n" "$VAL_UI"

if [ "$APPLY" = 0 ] && [ "$DELETE" = 0 ]; then
  echo
  note "배포: $0 --apply    제거: $0 --delete"
  exit 0
fi

# ── delete ───────────────────────────────────────────────────────────────────
if [ "$DELETE" = 1 ]; then
  hdr "제거"
  aws iam delete-role-policy --role-name "$ADMIN_ROLE_NAME" \
    --policy-name "$INVOKE_POLICY" 2>/dev/null \
    && ok "invoke 정책 제거 ($ADMIN_ROLE_NAME:$INVOKE_POLICY)" || note "invoke 정책 없음 — 건너뜀"
  if [ -n "$RT_ID" ]; then
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$RT_ID" \
      --region "$AWS_REGION" || die "agent runtime 삭제 실패: $RT_ID"
    ok "agent runtime 삭제 요청: $RT_NAME ($RT_ID)"
  else
    note "agent runtime 없음 — 건너뜀"
  fi

  tf_set enable_chat_agent false
  tf_set enable_chat_db_tools false
  ok "tfvars: enable_chat_agent=false, enable_chat_db_tools=false"

  (cd "$TF_DIR" && terraform apply -target=module.agentcore_runtime) \
    || die "terraform apply(모듈 파괴) 실패 — ECR/S3/Lambda/IAM 정리를 확인할 것"
  ok "terraform: module.agentcore_runtime 제거 완료"

  cp "$VALUES" "$VALUES.bak.$(date +%Y%m%d%H%M%S)"
  yq -i '.adminApi.env.AGENTCORE_RUNTIME_ARN = "" |
         .adminApi.env.CHAT_STAGING_BUCKET = "" |
         .adminUi.env.CHAT_ENABLED = "false"' "$VALUES" || die "values 편집 실패"
  ok "values 정리: ARN/버킷 비우기 + CHAT_ENABLED=false (백업 생성)"
  note "반영: cd $REPO_ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV"
  exit 0
fi

# ── apply ────────────────────────────────────────────────────────────────────
hdr "① BI tool Lambda 빌드 (query_db / get_schema)"
"$AGENT_DIR/lambdas/build-lambdas.sh" || die "Lambda 빌드 실패"
ok "Lambda 산출물 빌드 완료 (module build/ 디렉터리)"

hdr "② terraform — module.agentcore_runtime"
tf_set enable_chat_agent true
tf_set enable_chat_db_tools true
ok "tfvars: enable_chat_agent=true, enable_chat_db_tools=true"
(cd "$TF_DIR" && terraform apply -target=module.agentcore_runtime) \
  || die "terraform apply 실패"
ok "모듈 적용 완료"

# apply 후 outputs 재조회
ECR_URL=$(tf_out chat_agent_ecr_url);   [ -n "$ECR_URL" ] || die "chat_agent_ecr_url output 없음"
ROLE_ARN=$(tf_out chat_agent_execution_role_arn); [ -n "$ROLE_ARN" ] || die "chat_agent_execution_role_arn output 없음"
STAGING=$(tf_out chat_agent_staging_bucket);      [ -n "$STAGING" ] || die "chat_agent_staging_bucket output 없음"
AGENT_NAME=$(tf_out chat_agent_name);             [ -n "$AGENT_NAME" ] || die "chat_agent_name output 없음"
RT_NAME=$(printf '%s' "$AGENT_NAME" | tr '-' '_')

hdr "③ arm64 이미지 빌드 + ECR push"
VER=$(sed -n 's/^version = "\(.*\)"/\1/p' "$AGENT_DIR/pyproject.toml" | head -1)
[ -n "$VER" ] || die "admin-chat-agent/pyproject.toml 에서 version 을 못 읽음"
TAG="${VER}-arm64"
ECR_REPO="${ECR_URL#https://}"
ECR_REG="${ECR_REPO%%/*}"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REG" >/dev/null \
  || die "ECR 로그인 실패: $ECR_REG"

# immutable ECR — 같은 태그가 이미 있으면 push 는 실패하므로 미리 확인.
if aws ecr describe-images --repository-name "${ECR_REPO#*/}" --region "$AWS_REGION" \
     --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  note "이미지 태그 이미 존재: $TAG — 재사용 (코드 변경이면 pyproject version 을 올릴 것)"
else
  docker build --platform linux/arm64 -t "$ECR_REPO:$TAG" "$AGENT_DIR" \
    || die "arm64 빌드 실패 — x86_64 호스트면 docker buildx qemu(binfmt) 가 필요"
  docker push "$ECR_REPO:$TAG" || die "ECR push 실패: $ECR_REPO:$TAG"
  ok "이미지 push: $ECR_REPO:$TAG"
fi

hdr "④ AgentCore Runtime 생성/갱신"
RT_JSON=$(mktemp --suffix=.json)
trap 'rm -f "$RT_JSON"' EXIT
# create 와 update 는 식별자 키가 다르다 (agentRuntimeName vs agentRuntimeId) —
# 공통 본문을 먼저 쓰고 앞에 식별자만 다르게 붙인다.
RT_BODY='"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "'"$ECR_REPO:$TAG"'"}},
  "roleArn": "'"$ROLE_ARN"'",
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "protocolConfiguration": {"serverProtocol": "HTTP"},
  "environmentVariables": {
    "MODEL_OPUS": "global.anthropic.claude-opus-4-7",
    "MODEL_SONNET": "global.anthropic.claude-sonnet-4-6",
    "MODEL_HAIKU": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "CHAT_STAGING_BUCKET": "'"$STAGING"'"
  }'

if [ -z "$RT_ID" ]; then
  printf '{\n  "agentRuntimeName": "%s",\n  %s\n}\n' "$RT_NAME" "$RT_BODY" > "$RT_JSON"
  aws bedrock-agentcore-control create-agent-runtime --cli-input-json "file://$RT_JSON" \
    --region "$AWS_REGION" >/dev/null || die "agent runtime 생성 실패: $RT_NAME"
  ok "agent runtime 생성 요청: $RT_NAME"
else
  printf '{\n  "agentRuntimeId": "%s",\n  %s\n}\n' "$RT_ID" "$RT_BODY" > "$RT_JSON"
  aws bedrock-agentcore-control update-agent-runtime --cli-input-json "file://$RT_JSON" \
    --region "$AWS_REGION" >/dev/null || die "agent runtime 갱신 실패: $RT_ID"
  ok "agent runtime 갱신 요청: $RT_NAME ($RT_ID)"
fi

# READY 대기 (생성/갱신 모두 비동기)
note "runtime READY 대기 (최대 5분)"
for _ in $(seq 1 60); do
  sleep 5
  CUR=$(aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" \
      --query "agentRuntimes[?agentRuntimeName=='$RT_NAME']|[0]" --output json 2>/dev/null || echo "null")
  [ "$CUR" = "null" ] && continue
  RT_ID=$(printf '%s' "$CUR" | yq -r '.agentRuntimeId // ""')
  RT_STATUS=$(printf '%s' "$CUR" | yq -r '.status // "?"')
  RT_ARN=$(printf '%s' "$CUR" | yq -r '.agentRuntimeArn // ""')
  [ "$RT_STATUS" = "READY" ] && break
done
[ "$RT_STATUS" = "READY" ] || die "runtime 이 READY 로 안 됨 (현재: $RT_STATUS) — AWS 콘솔에서 확인"
[ -n "$RT_ARN" ] || die "runtime ARN 조회 실패"
ok "runtime READY: $RT_ARN"

hdr "⑤ admin-api IRSA invoke 권한"
# terraform 모듈은 admin-api role 에 InvokeAgentRuntime 을 부여하지 않는다 —
# admin-api(SigV4)가 runtime 을 호출하려면 여기서 인라인 정책으로 줘야 한다.
# 리소스는 실제 runtime ARN 하나로 좁힌다.
INVOKE_DOC=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"InvokeBIInsight","Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":"%s"}]}' "$RT_ARN")
aws iam put-role-policy --role-name "$ADMIN_ROLE_NAME" \
  --policy-name "$INVOKE_POLICY" --policy-document "$INVOKE_DOC" \
  || die "invoke 정책 부여 실패 ($ADMIN_ROLE_NAME)"
ok "invoke 정책 부여: $ADMIN_ROLE_NAME → $RT_ARN"

hdr "⑥ values 주입"
cp "$VALUES" "$VALUES.bak.$(date +%Y%m%d%H%M%S)"
ARN="$RT_ARN" REGION="$AWS_REGION" BUCKET="$STAGING" yq -i '
  .adminApi.env.AGENTCORE_RUNTIME_ARN = strenv(ARN) |
  .adminApi.env.AGENTCORE_REGION = strenv(REGION) |
  .adminApi.env.CHAT_STAGING_BUCKET = strenv(BUCKET) |
  .adminUi.env.CHAT_ENABLED = "true"' "$VALUES" || die "values 편집 실패"
ok "values 갱신: AGENTCORE_RUNTIME_ARN·REGION·CHAT_STAGING_BUCKET + CHAT_ENABLED=true"

echo
ok "완료 — 변경분 반영: cd $REPO_ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV"
note "반영되면 사이드바에 BI Insight 메뉴와 퀵챗이 나타난다 (ADMIN 전용)"
