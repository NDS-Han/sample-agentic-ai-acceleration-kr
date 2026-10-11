#!/usr/bin/env bash
# ==============================================================================
# NDS-01-deploy-litellm-pricing-lambda.sh — LiteLLM 단가 조회 Lambda 배포/삭제
# ------------------------------------------------------------------------------
# nds/dev 전용(NCR — upstream 대상 아님): LiteLLM Model Catalog 조회를
# admin-api 인라인 호출에서 Lambda 프록시로 격리한다.
#
# 하는 일:
#   apply  : 실행 role + Lambda 함수 생성/갱신 → admin-api IRSA role 에
#            lambda:InvokeFunction 인라인 정책 부여 → values 에
#            adminApi.env.LITELLM_PRICING_LAMBDA 주입
#   delete : 위 리소스를 역순으로 제거(정책 → 함수 → role) + values 키 삭제
#   status : 기본 — 현재 배포 상태만 표시
#
# Lambda 는 VPC 밖에 둔다(api.litellm.ai 공개 egress 필요, Aurora 접속 불필요 —
# 카탈로그 원본만 반환하고 정규화/DB 쓰기는 admin-api 가 한다).
#
# 사용법:
#   ./NDS-01-deploy-litellm-pricing-lambda.sh            # status
#   ./NDS-01-deploy-litellm-pricing-lambda.sh --apply
#   ./NDS-01-deploy-litellm-pricing-lambda.sh --delete
#
# values 변경분은 install-eks.sh <env> 로 반영된다.
# ==============================================================================
# 주의: _lib.sh 의 discover() 는 마지막 가드([ -z ] && die)가 성공해도 status 1 을
# 반환하는 기존 결함이 있어, 형제 스크립트와 마찬가지로 -e 없이 -uo pipefail 만 쓴다.
# 대신 변경을 가하는 호출은 전부 `|| die` 를 명시한다.
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/_lib.sh"

APPLY=0; DELETE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)  APPLY=1;  shift ;;
    --delete) DELETE=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[ "$APPLY" = 1 ] && [ "$DELETE" = 1 ] && die "--apply 와 --delete 는 동시에 쓸 수 없습니다"

require_env
command -v yq >/dev/null 2>&1 || die "yq not found — update-scripts/21 스크립트와 같은 도구 요구사항"

REPO_ROOT=$(cd "$LIB_DIR/../../.." && pwd)
LAMBDA_SRC="$REPO_ROOT/litellm-pricing-lambda/lambda_function.py"
[ -f "$LAMBDA_SRC" ] || die "Lambda 소스 없음: $LAMBDA_SRC"
VALUES="$REPO_ROOT/deployment/charts/llm-gateway/values-eks-fargate-${DEPLOY_ENV}.yaml"
[ -f "$VALUES" ] || die "values 파일 없음: $VALUES"

FN_NAME="llm-gateway-${DEPLOY_ENV}-litellm-pricing"
FN_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FN_NAME}"
ROLE_NAME="llm-gateway-${DEPLOY_ENV}-litellm-pricing"
ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
INVOKE_POLICY="LitellmPricingInvoke"

# admin-api IRSA role — terraform output 우선, 없으면 명명 규칙으로 폴백.
TF_DIR="$REPO_ROOT/deployment/terraform/environments/llm-gateway-${DEPLOY_ENV}"
ADMIN_ROLE_ARN=$(cd "$TF_DIR" 2>/dev/null && terraform output -raw admin_api_role_arn 2>/dev/null || true)
: "${ADMIN_ROLE_ARN:=arn:aws:iam::${AWS_ACCOUNT_ID}:role/llm-gateway-${DEPLOY_ENV}-admin-api}"
ADMIN_ROLE_NAME="${ADMIN_ROLE_ARN##*/}"

VALUES_KEY_EXPR='.adminApi.env.LITELLM_PRICING_LAMBDA // "-"'

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
INVOKE_DOC=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"InvokeLitellmPricing","Effect":"Allow","Action":"lambda:InvokeFunction","Resource":"${FN_ARN}"}]}
EOF
)

hdr "LiteLLM pricing Lambda — ${DEPLOY_ENV}"
fn_state=$(aws lambda get-function --function-name "$FN_NAME" \
    --query 'Configuration.State' --output text 2>/dev/null || echo "미배포")
role_state=$(aws iam get-role --role-name "$ROLE_NAME" \
    --query 'Role.Arn' --output text 2>/dev/null || echo "미생성")
invoke_state=$(aws iam get-role-policy --role-name "$ADMIN_ROLE_NAME" \
    --policy-name "$INVOKE_POLICY" --query 'PolicyName' --output text 2>/dev/null || echo "미부여")
val_cur=$(yq -r "$VALUES_KEY_EXPR" "$VALUES")

printf "  function %-42s %s\n" "$FN_NAME" "$fn_state"
printf "  role     %-42s %s\n" "$ROLE_NAME" "$role_state"
printf "  invoke   %-42s %s\n" "$ADMIN_ROLE_NAME:$INVOKE_POLICY" "$invoke_state"
printf "  values   adminApi.env.LITELLM_PRICING_LAMBDA = %s\n" "$val_cur"

if [ "$APPLY" = 0 ] && [ "$DELETE" = 0 ]; then
  echo
  note "적용: $0 --apply    제거: $0 --delete"
  exit 0
fi

if [ "$DELETE" = 1 ]; then
  hdr "제거"
  aws iam delete-role-policy --role-name "$ADMIN_ROLE_NAME" \
    --policy-name "$INVOKE_POLICY" 2>/dev/null \
    && ok "invoke 정책 제거 ($ADMIN_ROLE_NAME)" || note "invoke 정책 없음 — 건너뜀"
  aws lambda delete-function --function-name "$FN_NAME" 2>/dev/null \
    && ok "Lambda 삭제: $FN_NAME" || note "Lambda 없음 — 건너뜀"
  if [ "$role_state" != "미생성" ]; then
    aws iam detach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
    for p in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
        --query 'PolicyNames[]' --output text 2>/dev/null); do
      aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$p" \
        || die "role 인라인 정책 삭제 실패: $p"
    done
    aws iam delete-role --role-name "$ROLE_NAME" || die "role 삭제 실패: $ROLE_NAME"
    ok "role 삭제: $ROLE_NAME"
  fi
  if [ "$val_cur" != "-" ]; then
    cp "$VALUES" "$VALUES.bak.$(date +%Y%m%d%H%M%S)"
    yq -i 'del(.adminApi.env.LITELLM_PRICING_LAMBDA)' "$VALUES" || die "values 편집 실패"
    ok "values 에서 LITELLM_PRICING_LAMBDA 제거 (백업 생성)"
  fi
  note "adminApi.env 변경분 반영: cd $REPO_ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV"
  exit 0
fi

# ── apply ────────────────────────────────────────────────────────────────────
hdr "Lambda 패키징·배포"
ZIP=$(mktemp --suffix=.zip) || die "mktemp 실패"
trap 'rm -f "$ZIP"' EXIT
python3 - "$LAMBDA_SRC" "$ZIP" <<'PYEOF' || die "zip 패키징 실패"
import sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(src, "lambda_function.py")
print("zip:", dst)
PYEOF

if [ "$role_state" = "미생성" ]; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" \
    --description "llm-gateway LiteLLM pricing catalog fetch proxy" >/dev/null \
    || die "role 생성 실패: $ROLE_NAME"
  aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
    || die "managed policy attach 실패"
  ok "role 생성: $ROLE_NAME (BasicExecutionRole만 — DB/Secret 접근 없음)"
  note "role 전파 대기 10s"; sleep 10
fi

if [ "$fn_state" = "미배포" ]; then
  aws lambda create-function --function-name "$FN_NAME" \
    --runtime python3.12 --handler lambda_function.lambda_handler \
    --role "$ROLE_ARN" --zip-file "fileb://$ZIP" \
    --timeout 60 --memory-size 256 \
    --description "LiteLLM model_catalog fetch proxy for llm-gateway admin-api" \
    >/dev/null || die "Lambda 생성 실패: $FN_NAME"
  ok "Lambda 생성: $FN_NAME"
else
  aws lambda update-function-code --function-name "$FN_NAME" \
    --zip-file "fileb://$ZIP" >/dev/null || die "Lambda 코드 갱신 실패"
  aws lambda wait function-updated --function-name "$FN_NAME" || die "갱신 대기 실패"
  aws lambda update-function-configuration --function-name "$FN_NAME" \
    --timeout 60 --memory-size 256 >/dev/null || die "Lambda 설정 갱신 실패"
  ok "Lambda 갱신: $FN_NAME"
fi

aws iam put-role-policy --role-name "$ADMIN_ROLE_NAME" \
  --policy-name "$INVOKE_POLICY" --policy-document "$INVOKE_DOC" \
  || die "invoke 정책 부여 실패 ($ADMIN_ROLE_NAME)"
ok "invoke 정책 부여: $ADMIN_ROLE_NAME → $FN_NAME"

if [ "$val_cur" != "$FN_NAME" ]; then
  cp "$VALUES" "$VALUES.bak.$(date +%Y%m%d%H%M%S)"
  FN="$FN_NAME" yq -i '.adminApi.env.LITELLM_PRICING_LAMBDA = strenv(FN)' "$VALUES" \
    || die "values 편집 실패"
  ok "values 갱신: adminApi.env.LITELLM_PRICING_LAMBDA=$FN_NAME"
fi

echo
ok "완료 — adminApi.env 변경분 반영: cd $REPO_ROOT && ./deployment/scripts/install-eks.sh $DEPLOY_ENV"
note "직접 invoke 검증: aws lambda invoke --function-name $FN_NAME --payload '{}' /tmp/o.json && cat /tmp/o.json"
