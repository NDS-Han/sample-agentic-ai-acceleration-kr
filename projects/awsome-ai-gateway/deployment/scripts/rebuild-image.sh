#!/usr/bin/env bash
# ============================================================================
# rebuild-image.sh — 서비스 하나만 다시 빌드해서 ECR 에 올린다 (코드 패치 후 사용).
#
#   태그 = 현재 git HEAD 의 short SHA (라이브 배포 규약 — rev173/174 도 SHA 태그).
#   ⚠️ helm template 의 values 기본 태그(1.0.x-*)를 쓰면 안 된다 — 라이브는
#      --set 으로 SHA 태그를 주입하므로 values 태그는 실제 당기는 이미지와
#      무관하다. 그 값으로 빌드·푸시하면 restart 해도 아무것도 안 바뀌고,
#      ECR 의 과거 템플릿 태그만 덮어쓴다.
#   ⚠️ 같은 태그 덮어쓰기 금지 — 새 SHA 태그 + helm upgrade --set 조합이어야
#      매니페스트가 바뀌어 롤아웃과 pre-upgrade migration hook 이 둘 다 돈다.
#      (rollout restart 만으로는 migration Job 이 안 돌아 스키마가 뒤처질 수 있다.)
#
# 사용:  bash deployment/scripts/rebuild-image.sh gateway-proxy [dev|prod]
#        DEPLOY=1 bash deployment/scripts/rebuild-image.sh admin-api dev  # push 후 자동 helm upgrade
# ============================================================================
set -euo pipefail

# ---- 리전 확정 (install-eks.sh 와 동일 규약: 추측하지 않고 중단) ----
: "${AWS_REGION:=${AWS_DEFAULT_REGION:-}}"
if [ -z "$AWS_REGION" ]; then
  echo "ERROR: region is not set. export AWS_DEFAULT_REGION=<region> (e.g. us-west-2) and retry." >&2
  exit 1
fi
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

SVC="${1:-}"
ENV="${2:-dev}"
[ -n "$SVC" ] || { echo "Usage: $0 <service> [dev|prod]   (예: $0 gateway-proxy dev)"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ---- 서비스 → values 키 맵 (차트 루트 키) ----
# admin-api 는 scheduler 와 이미지를 공유한다 — 반드시 둘 다 같은 태그로 갱신
# (values-eks-fargate-*.yaml 의 "adminApi.image.tag 와 항상 같은 값" 주석).
declare -A VALUES_KEY=(
  [gateway-proxy]="gatewayProxy"
  [admin-api]="adminApi"
  [admin-ui]="adminUi"
  [notification-worker]="notificationWorker"
  [cost-recorder-worker]="costRecorderWorker"
  [migration]="migration"
)
KEY="${VALUES_KEY[$SVC]:-}"
[ -n "$KEY" ] || { echo "❌ 알 수 없는 서비스: $SVC  (가능: ${!VALUES_KEY[*]})"; exit 1; }

# ---- 태그 = git HEAD short SHA (불변·배포 일치) ----
TAG="${TAG:-$(git -C "$ROOT" rev-parse --short=8 HEAD)}"
if [ -n "$(git -C "$ROOT" status --porcelain -- "$SVC" 2>/dev/null || true)" ]; then
  echo "⚠️  경고: $SVC/ 아래 미커밋 변경이 있다 — 빌드 내용과 태그($TAG)가 어긋난다." >&2
  echo "    커밋 후 다시 실행하는 것을 권장한다." >&2
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/llm-gateway"
CTX="$ROOT/$SVC"; [ "$SVC" = migration ] && CTX="$ROOT/db"
[ -d "$CTX" ] || { echo "❌ 빌드 컨텍스트 없음: $CTX"; exit 1; }

# ---- 동일 태그 덮어쓰기 방지 ----
if aws ecr describe-images --repository-name "llm-gateway/$SVC" \
     --image-ids imageTag="$TAG" --query 'imageIds[0]' --output text 2>/dev/null | grep -q .; then
  echo "❌ $ECR_BASE/$SVC:$TAG 는 이미 ECR 에 있다." >&2
  echo "   같은 태그 덮어쓰기는 변경을 구별할 수 없게 만든다 — 커밋으로 새 SHA 를 만들거나 TAG=<명시값> 을 지정하라." >&2
  exit 1
fi

SET_ARGS=(--set "$KEY.image.tag=$TAG")
[ "$SVC" = "admin-api" ] && SET_ARGS+=(--set "scheduler.image.tag=$TAG")

echo "── 재빌드 ──"
echo "  service : $SVC"
echo "  tag     : $TAG   (git HEAD short SHA — helm values 기본값이 아님)"
echo "  context : $CTX"
echo "  target  : $ECR_BASE/$SVC:$TAG"
echo "  helm set: ${SET_ARGS[*]}"
echo

aws ecr get-login-password | docker login --username AWS --password-stdin \
  "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com" >/dev/null
docker build --platform linux/amd64 -t "$ECR_BASE/$SVC:$TAG" "$CTX"
docker push "$ECR_BASE/$SVC:$TAG"

HELM_CMD=(helm upgrade llm-gateway deployment/charts/llm-gateway
  -n llm-gateway --reuse-values "${SET_ARGS[@]}")

if [ "${DEPLOY:-0}" = "1" ]; then
  echo "── helm upgrade (migration hook 포함) ──"
  (cd "$ROOT" && "${HELM_CMD[@]}")
  kubectl -n llm-gateway rollout status deploy/llm-gateway-$SVC --timeout=5m
else
  cat <<EOF

✅ push 완료: $ECR_BASE/$SVC:$TAG

다음 — helm upgrade 로 롤아웃(pre-upgrade migration hook 도 함께 돈다):
    (cd $ROOT && ${HELM_CMD[*]})
    kubectl -n llm-gateway rollout status deploy/llm-gateway-$SVC --timeout=5m
EOF
fi
