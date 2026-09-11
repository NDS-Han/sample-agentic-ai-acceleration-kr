#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Terraform State Backend 최초 1회 셋업 — S3 + DynamoDB
# ------------------------------------------------------------------------------
# Terraform이 자기 자신으로 state 백엔드를 만들 수 없는 chicken-and-egg 때문에
# 이 스크립트로 "state를 저장할 S3 + lock용 DynamoDB" 를 먼저 만듭니다.
# 한 번 실행하면 반복 실행 불필요 (idempotent).
# ==============================================================================

set -euo pipefail

# ---- 기본값 (필요 시 환경변수로 오버라이드) ----
# TFSTATE_BUCKET 은 ACCOUNT_ID 가 필요하므로 0) STS 확인 뒤에 채운다 (아래).
: "${AWS_REGION:=ap-northeast-2}"
: "${TFLOCK_TABLE:=llm-gateway-vanilla-tflock}"

# 0) AWS 인증 확인
if ! aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "❌ AWS 인증이 안 됩니다. aws configure / aws sso login 먼저 수행."
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# S3 버킷 이름은 계정이 아니라 전역(global) 네임스페이스다 — 계정 비종속 이름
# (예: llm-gateway-tfstate) 은 이미 남이 선점했을 수 있고, 실제로 선점돼 있다
# (실측: head-bucket → 403 Forbidden, 이 계정 소유 목록에는 없음).
# 그래서 기본값을 account-scoped 로 만들어 전역 고유성을 확보하고, backend.tf 주석 /
# docs/guides/IRSA-SETUP.md 가 쓰는 이름과 동일하게 맞춘다.
: "${TFSTATE_BUCKET:=llm-gateway-vanilla-tfstate-${ACCOUNT_ID}}"

echo "==> Bootstrapping Terraform state backend"
echo "    region        : ${AWS_REGION}"
echo "    state bucket  : ${TFSTATE_BUCKET}"
echo "    lock table    : ${TFLOCK_TABLE}"
echo ""
echo "✓ AWS Account: ${ACCOUNT_ID}"

# 1) S3 버킷 생성 (없을 때만)
# head-bucket 의 exit code 만으로는 판단할 수 없다 — 실측(aws-cli 2.28.22):
#   내 버킷=0, 남의 버킷=254(403), 없는 버킷=254(404) → 403 과 404 가 같은 코드다.
# 옛 구현(`if aws s3api head-bucket ... 2>/dev/null`)은 403 을 "없음"으로 오판해
# 남이 선점한 이름에 create-bucket 을 시도했고, 사용자에게는 원인과 무관한
# BucketAlreadyExists 만 남았다. stderr 의 HTTP 코드를 보고 세 경우를 분리한다.
HEAD_RC=0
HEAD_ERR=$(aws s3api head-bucket --bucket "${TFSTATE_BUCKET}" 2>&1 >/dev/null) || HEAD_RC=$?
if [ "${HEAD_RC}" -eq 0 ]; then
    echo "✓ S3 bucket ${TFSTATE_BUCKET} 이미 존재"
elif printf '%s' "${HEAD_ERR}" | grep -Eq '\(404\)|NoSuchBucket|Not Found'; then
    echo "==> S3 bucket 생성: ${TFSTATE_BUCKET}"
    # LocationConstraint 를 거부하는 리전은 us-east-1 뿐이고, 나머지는 모두 요구한다.
    # CLI 가 --region 으로부터 자동 보완하지 않는다 — 실측: ap-northeast-2 에서 생략하면
    # IllegalLocationConstraintException, us-east-1 에서 지정하면 InvalidLocationConstraint.
    if [ "${AWS_REGION}" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "${TFSTATE_BUCKET}" --region "${AWS_REGION}"
    else
        aws s3api create-bucket \
            --bucket "${TFSTATE_BUCKET}" \
            --region "${AWS_REGION}" \
            --create-bucket-configuration LocationConstraint="${AWS_REGION}"
    fi
elif printf '%s' "${HEAD_ERR}" | grep -Eq '\(403\)|AccessDenied|Forbidden'; then
    echo "❌ S3 bucket ${TFSTATE_BUCKET} 는 이미 존재하지만 이 계정(${ACCOUNT_ID}) 으로 접근할 수 없습니다 (403)."
    echo "   S3 버킷 이름은 전역 고유이므로 다른 계정이 선점한 이름일 수 있습니다."
    echo "   남의 버킷에 versioning/암호화/lifecycle 을 덮어쓰지 않기 위해 여기서 중단합니다."
    echo "   해결: 전역 고유한 이름을 지정해 다시 실행 —"
    echo "     TFSTATE_BUCKET=llm-gateway-vanilla-tfstate-${ACCOUNT_ID} ./scripts/bootstrap-tfstate.sh"
    echo "   (내 계정 소유인데도 403 이면 s3:ListBucket/HeadBucket 권한부터 확인)"
    exit 1
else
    echo "❌ S3 bucket ${TFSTATE_BUCKET} 상태를 판별할 수 없습니다 (head-bucket rc=${HEAD_RC})."
    echo "   ${HEAD_ERR}"
    exit 1
fi

# 2) Versioning (이전 state 복원용)
aws s3api put-bucket-versioning \
    --bucket "${TFSTATE_BUCKET}" \
    --versioning-configuration Status=Enabled
echo "✓ Versioning 활성화"

# 3) Server-side 암호화
aws s3api put-bucket-encryption \
    --bucket "${TFSTATE_BUCKET}" \
    --server-side-encryption-configuration '{
      "Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        "BucketKeyEnabled": true
      }]
    }'
echo "✓ SSE-S3 암호화 설정"

# 4) Public access 완전 차단
aws s3api put-public-access-block \
    --bucket "${TFSTATE_BUCKET}" \
    --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
echo "✓ Public access 차단"

# 5) Lifecycle — 오래된 버전 정리
aws s3api put-bucket-lifecycle-configuration \
    --bucket "${TFSTATE_BUCKET}" \
    --lifecycle-configuration '{
      "Rules": [{
        "ID": "expire-old-versions",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 90}
      }]
    }'
echo "✓ 90일 지난 이전 버전 자동 삭제"

# 6) DynamoDB 테이블 (lock) — 이미 있으면 스킵
if aws dynamodb describe-table --table-name "${TFLOCK_TABLE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "✓ DynamoDB table ${TFLOCK_TABLE} 이미 존재"
else
    echo "==> DynamoDB table 생성: ${TFLOCK_TABLE}"
    aws dynamodb create-table \
        --table-name "${TFLOCK_TABLE}" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "${AWS_REGION}" \
        --tags Key=Project,Value=llm-gateway Key=ManagedBy,Value=bootstrap-script

    echo "   DynamoDB 테이블 생성 대기..."
    aws dynamodb wait table-exists --table-name "${TFLOCK_TABLE}" --region "${AWS_REGION}"
fi

echo ""
echo "✅ Bootstrap 완료. 다음 단계:"
echo ""
# ⚠️ 디렉터리 이름은 dev/prod 가 아니라 llm-gateway-dev / llm-gateway-prod 다.
#    그리고 backend.tf 는 **partial config** 라(key/region/encrypt 만 선언)
#    bucket/dynamodb_table 을 -backend-config 로 주입하지 않으면 init 이 실패한다.
echo "   cd deployment/terraform/environments/llm-gateway-dev  (또는 llm-gateway-prod)"
echo "   cp terraform.tfvars.example terraform.tfvars"
echo "   # terraform.tfvars 편집"
echo "   terraform init \\"
echo "       -backend-config=\"bucket=${TFSTATE_BUCKET}\" \\"
echo "       -backend-config=\"dynamodb_table=${TFLOCK_TABLE}\""
echo "   terraform plan"
echo "   terraform apply"
echo ""