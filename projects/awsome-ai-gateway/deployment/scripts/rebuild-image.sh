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
# 연관 배열(declare -A)은 macOS 기본 bash 3.2 에서 없다 — case 함수로 (R24-S6r4).
values_key() {
  case "$1" in
    gateway-proxy)        echo gatewayProxy ;;
    admin-api)            echo adminApi ;;
    admin-ui)             echo adminUi ;;
    notification-worker)  echo notificationWorker ;;
    cost-recorder-worker) echo costRecorderWorker ;;
    migration)            echo migration ;;
    *) return 1 ;;
  esac
}
KEY="$(values_key "$SVC")" || { echo "❌ 알 수 없는 서비스: $SVC  (가능: gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker migration)"; exit 1; }

# ---- 태그 = git HEAD short SHA (불변·배포 일치) ----
TAG="${TAG:-$(git -C "$ROOT" rev-parse --short=8 HEAD)}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/llm-gateway"
CTX="$ROOT/$SVC"; [ "$SVC" = migration ] && CTX="$ROOT/db"
[ -d "$CTX" ] || { echo "❌ 빌드 컨텍스트 없음: $CTX"; exit 1; }

# dirty 검사는 빌드 컨텍스트(CTX) 기준 — SVC=migration 이면 db/ 를 봐야 하는데
# 예전엔 존재하지 않는 "migration/" 경로를 봐서 경고가 영구 불발이었다(R24-S5).
if [ -n "$(git -C "$ROOT" status --porcelain -- "${CTX#$ROOT/}" 2>/dev/null || true)" ]; then
  echo "⚠️  경고: ${CTX#$ROOT/}/ 아래 미커밋 변경이 있다 — 빌드 내용과 태그($TAG)가 어긋난다." >&2
  echo "    커밋 후 다시 실행하는 것을 권장한다." >&2
fi

# ---- 동일 태그 덮어쓰기 방지 ----
if aws ecr describe-images --repository-name "llm-gateway/$SVC" \
     --image-ids imageTag="$TAG" --query 'imageIds[0]' --output text 2>/dev/null | grep -q .; then
  echo "❌ $ECR_BASE/$SVC:$TAG 는 이미 ECR 에 있다." >&2
  echo "   같은 태그 덮어쓰기는 변경을 구별할 수 없게 만든다 — 커밋으로 새 SHA 를 만들거나 TAG=<명시값> 을 지정하라." >&2
  exit 1
fi

SET_ARGS=(--set "$KEY.image.tag=$TAG")
[ "$SVC" = "admin-api" ] && SET_ARGS+=(--set "scheduler.image.tag=$TAG")

if [ "${DEPLOY:-0}" = "1" ]; then
  # ---- kube context 가드 (R24-S3) ----
  # [dev|prod] 인자는 예전엔 아무 데도 안 쓰여서 현재 context 에 그대로 배포됐다.
  # 기대 context = llm-gateway-$ENV (다른 명명을 쓰면 KUBE_CONTEXT env 로 지정).
  EXPECTED_CTX="${KUBE_CONTEXT:-llm-gateway-$ENV}"
  CUR_CTX="$(kubectl config current-context 2>/dev/null || true)"
  if [ "$CUR_CTX" != "$EXPECTED_CTX" ]; then
    echo "❌ kube context 불일치: 현재='${CUR_CTX:-<none>}' 기대='$EXPECTED_CTX'" >&2
    echo "   잘못된 환경에 배포되는 것을 막기 위해 중단한다. context 가 다른 이름이면" >&2
    echo "   KUBE_CONTEXT=<name> 을 명시하거나 kubectl config use-context 로 전환할 것." >&2
    exit 1
  fi

  # ---- migration 게이트 (R24-S5) ----
  # db/ 가 라이브 migration 태그 이후로 바뀌었는데 다른 서비스만 올리면 구 이미지의
  # `alembic upgrade head` 가 no-op 이라 앱이 새 스키마 없이 올라간다(실사고 부류).
  # 라이브 태그가 git commit 으로 확인 가능할 때만 비교한다(1.0.x-* 태그는 스킵).
  if [ "$SVC" != "migration" ]; then
    live_mtag="$(helm get values llm-gateway -n llm-gateway -o json 2>/dev/null \
      | jq -r '.migration.image.tag // empty' 2>/dev/null || true)"
    # --diff-filter=A: live mig 에 없는 *새* revision 파일만 막는다 — 이력만
    # 다른 같은 트리(squash-merge)·shallow clone 에서의 오탐 방지(R24-N1r5).
    if [ -n "$live_mtag" ] \
       && git -C "$ROOT" rev-parse --verify --quiet "${live_mtag}^{commit}" >/dev/null \
       && ! git -C "$ROOT" diff --quiet --diff-filter=A "$live_mtag" HEAD -- db/versions/; then
      echo "❌ db/ 가 라이브 migration 태그($live_mtag) 이후 변경됐다." >&2
      echo "   먼저 DEPLOY=1 rebuild-image.sh migration $ENV 로 스키마부터 배포할 것." >&2
      exit 1
    fi
  fi

  # ---- helm 버전 preflight (R24-N13r2) ----
  # --reset-then-reuse-values 는 Helm ≥3.14 전용. push 후에야 flag 에러로 죽으면
  # 재실행이 "동일 태그 존재" 가드에 막히고 수동 명령도 안 나온다 — 미리 검사.
  HELM_MAJOR=$(helm version --template '{{.Version}}' 2>/dev/null \
    | sed -E 's/^v([0-9]+)\.([0-9]+).*/\1 \2/' || true)
  set -- $HELM_MAJOR
  # 비숫자 출력(helm 미설치, 예상 못한 포맷)은 통과시키지 않고 중단한다.
  case "${1:-x}" in ''|*[!0-9]*) MAJOR_OK=0 ;;
    *)
      if [ "$1" -gt 3 ] || { [ "$1" -eq 3 ] && [ "${2:-0}" -ge 14 ]; }; then
        MAJOR_OK=1
      else
        MAJOR_OK=0
      fi ;;
  esac
  if [ "$MAJOR_OK" = 0 ]; then
    echo "❌ --reset-then-reuse-values 는 Helm ≥3.14 가 필요 (현재: $(helm version --short 2>/dev/null || echo '확인 불가'))" >&2
    exit 1
  fi
fi

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

# --reuse-values 는 저장 values 를 그대로 쓴다 — 두 가지 함정이 있다.
#   1) install-eks.sh 의 NEXTAUTH 패치가 남긴 `migration.enabled=false` 도 그대로라,
#      새 SHA 에 마이그레이션이 섞여 있어도 훅이 안 돌아 스키마가 뒤처진다(rev175).
#      명시적으로 true 로 되돌려 둔다 — head 가 이미 최신이면 Job 은 no-op.
#   2) (R24-M5) 차트 values.yaml 에 **새로 추가된 기본 키**를 reuse 가 흡수하지 못한다
#      — 이전 릴리스의 coalesced values 를 쓰기 때문(BUDGET_PENDINGIDX_WARMUP 사고).
#      --reset-then-reuse-values(Helm ≥3.14)는 저장 값을 우선하면서 새 차트 기본값도
#      병합한다.
# --timeout: helm 기본 5m < migration activeDeadlineSeconds(600)+init 대기(300).
#   타임아웃으로 릴리스만 FAILED 가 되고 Job 은 스키마를 올린 채 계속 돌아
#   "새 스키마 + 구 앱" 조합이 된다(R24-S4) — 15m + atomic 으로 맞춘다.
HELM_CMD=(helm upgrade llm-gateway deployment/charts/llm-gateway
  -n llm-gateway --reset-then-reuse-values "${SET_ARGS[@]}"
  --set "migration.enabled=true"
  --timeout 15m --wait --atomic)

if [ "${DEPLOY:-0}" = "1" ]; then
  # ---- ExternalSecret ownerReference 전환 (R24-N6r2) ----
  # 구 ES(creationPolicy: Owner)가 만든 Secret 에는 ES ownerRef 가 남아 있다.
  # 새 ES(Orphan)가 들어올 때 구 ES 가 삭제되면 GC 가 그 Secret 도 삭제 → ESO 가
  # 수 초 뒤 재생성하는 창이 1회 생긴다. upgrade 전에 stale ownerRef 를 지워 둔다.
  for sec in llm-gateway-app llm-gateway-db llm-gateway-redis; do
    owner_kind=$(kubectl -n llm-gateway get secret "$sec" \
      -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || echo "")
    if [ "$owner_kind" = "ExternalSecret" ]; then
      kubectl -n llm-gateway patch secret "$sec" --type=json \
        -p='[{"op":"remove","path":"/metadata/ownerReferences"}]' >/dev/null 2>&1 \
        && echo "  stale ownerRef 제거: secret/$sec" || true
    fi
  done
  echo "── helm upgrade (migration hook 포함) ──"
  (cd "$ROOT" && "${HELM_CMD[@]}") || {
    echo "❌ helm upgrade 실패. 태그 가드 때문에 같은 명령 재실행은 안 되므로" >&2
    echo "   조사 후 직접 실행: (cd $ROOT && ${HELM_CMD[*]})" >&2
    exit 1
  }
  # migration 은 Job 이라 rollout 대상이 아니다 — hook 완료는 helm --wait 가
  # 이미 대기했고, deploy/llm-gateway-migration 은 존재하지 않는다(R24-N11).
  if [ "$SVC" != "migration" ]; then
    kubectl -n llm-gateway rollout status deploy/llm-gateway-$SVC --timeout=5m
  fi
else
  # DEPLOY=0 은 게이트를 거치지 않고 명령만 출력한다 — 수동 실행 시 구 스키마 위에
  # 새 앱이 올라가는 경로라, 라이브 mig 태그를 읽을 수 있으면 미리 경고한다.
  live_mtag="$(helm get values llm-gateway -n llm-gateway -o json 2>/dev/null \
    | jq -r '.migration.image.tag // empty' 2>/dev/null || true)"
  if [ "$SVC" != "migration" ] && [ -n "$live_mtag" ] \
     && git -C "$ROOT" rev-parse --verify --quiet "${live_mtag}^{commit}" >/dev/null 2>&1 \
     && ! git -C "$ROOT" diff --quiet --diff-filter=A "$live_mtag" HEAD -- db/versions/; then
    echo "⚠️  경고: db/versions/ 에 라이브 migration 태그($live_mtag)에 없는 새 revision 이 있다." >&2
    echo "    출력된 helm 명령 실행 전에 DEPLOY=1 rebuild-image.sh migration $ENV 를 먼저 돌릴 것." >&2
  fi
  ROLLOUT="kubectl -n llm-gateway rollout status deploy/llm-gateway-$SVC --timeout=5m"
  # migration 은 Deployment 가 아니라 pre-upgrade Job — rollout status 대상이 아니다.
  [ "$SVC" = "migration" ] && ROLLOUT="kubectl -n llm-gateway get job -l app.kubernetes.io/component=migration"
  cat <<EOF

✅ push 완료: $ECR_BASE/$SVC:$TAG

다음 — helm upgrade 로 롤아웃(pre-upgrade migration hook 도 함께 돈다):
    (cd $ROOT && ${HELM_CMD[*]})
    $ROLLOUT
EOF
fi
