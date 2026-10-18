#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# install-eks.sh — AWS EKS Fargate 환경에 LLM Gateway 설치/업그레이드
# ------------------------------------------------------------------------------
# Terraform outputs 에서 IRSA role ARN, Aurora/Redis 엔드포인트를 자동 추출해
# Helm install 의 --set 으로 주입합니다.
#
# 사용법:
#   ./install-eks.sh <env>
#     env: dev | prod
#
# 전제:
#   - terraform/environments/llm-gateway-<env> 에 terraform apply 가 성공적으로 완료됨
#   - aws cli 인증 (aws sts get-caller-identity 통과)
#   - helm, kubectl, terraform, jq 설치
# ==============================================================================

set -euo pipefail

# ---- 리전 확정 ----
# 이 스크립트의 aws 호출(secretsmanager describe-secret 등)은 --region 을 명시하지 않아
# 셸 기본 리전을 따른다. 리전이 안 잡힌 셸에서 실행하면 Secrets Manager preflight 가
# 엉뚱한 리전을 뒤져 "Secret 없음" 으로 오판하므로, 추측하지 말고 즉시 중단한다.
# 우선순위: AWS_REGION > AWS_DEFAULT_REGION > ~/.aws/config (aws configure get region).
# 그 뒤 둘을 통일. config 폴백이 없으면 env 없이 ~/.aws/config 로만 리전을 잡은 셸에서
# aws CLI 자체는 정상 동작하는데도 이 가드가 거부하는 오탐이 난다.
: "${AWS_REGION:=${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null)}}"
if [ -z "$AWS_REGION" ]; then
    echo "ERROR: region is not set. export AWS_DEFAULT_REGION=<region> (e.g. ap-northeast-2) and retry." >&2
    exit 1
fi
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

# ---- 인자 ----
ENV="${1:-}"
if [ -z "$ENV" ] || { [ "$ENV" != "dev" ] && [ "$ENV" != "prod" ]; }; then
    echo "Usage: $0 <dev|prod>"
    exit 1
fi

# ---- 경로 설정 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$DEPLOY_DIR/terraform/environments/llm-gateway-$ENV"
CHART_DIR="$DEPLOY_DIR/charts/llm-gateway"
VALUES_FILE="$CHART_DIR/values-eks-fargate-$ENV.yaml"
RELEASE_NAME="llm-gateway"
NAMESPACE="llm-gateway"

# ---- 색 출력 ----
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }
err()   { echo -e "${R}✗${N}  $*" >&2; }

# ---- 전제 도구 확인 ----
check_prereqs() {
    local missing=()
    for cmd in aws kubectl helm terraform jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        err "필요한 도구 누락: ${missing[*]}"
        echo "  macOS: brew install ${missing[*]/terraform/hashicorp/tap/terraform}"
        exit 1
    fi

    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        err "AWS 인증 실패. aws configure / aws sso login 먼저 실행하세요."
        exit 1
    fi

    if [ ! -d "$TF_DIR" ]; then
        err "Terraform 환경 디렉토리 없음: $TF_DIR"
        exit 1
    fi

    if [ ! -f "$VALUES_FILE" ]; then
        err "Values 파일 없음: $VALUES_FILE"
        exit 1
    fi
}

# ---- Terraform outputs 추출 ----
read_tf_outputs() {
    info "Terraform outputs 추출 중 ($TF_DIR)"
    pushd "$TF_DIR" >/dev/null

    if [ ! -d ".terraform" ]; then
        err "terraform init 가 안 된 것으로 보입니다. cd $TF_DIR && terraform init 먼저 실행."
        exit 1
    fi

    # terraform apply 가 완료됐는지 확인
    if ! terraform output -json >/dev/null 2>&1; then
        err "terraform output 실패. terraform apply 를 먼저 성공시켜야 합니다."
        exit 1
    fi

    local outputs
    outputs=$(terraform output -json)

    CLUSTER_NAME=$(echo "$outputs" | jq -r '.cluster_name.value')
    AWS_REGION=$(echo "$outputs" | jq -r '.cluster_endpoint.value' | awk -F. '{print $3}')
    GATEWAY_ROLE_ARN=$(echo "$outputs" | jq -r '.gateway_proxy_role_arn.value')
    ADMIN_API_ROLE_ARN=$(echo "$outputs" | jq -r '.admin_api_role_arn.value')
    # RDS Proxy on 이면 proxy_endpoint, off 면 aurora_endpoint 를 반환 (modules/aurora-postgresql outputs).
    # dev 기본 off → aurora_endpoint, prod 기본 on → proxy_endpoint. 환경에 관계없이 올바른 값 선택.
    AURORA_HOST=$(echo "$outputs" | jq -r '.application_db_endpoint.value // .aurora_endpoint.value')
    AURORA_DB_NAME=$(echo "$outputs" | jq -r '.aurora_database_name.value // "gateway"')
    AURORA_SECRET_ARN=$(echo "$outputs" | jq -r '.aurora_master_user_secret_arn.value')
    REDIS_HOST=$(echo "$outputs" | jq -r '.elasticache_endpoint.value')
    REDIS_AUTH_SECRET_ARN=$(echo "$outputs" | jq -r '.elasticache_auth_token_secret_arn.value')

    # Cognito (OIDC IDP) outputs — terraform 에서 만들어진 경우만 존재
    COGNITO_ISSUER_URL=$(echo "$outputs" | jq -r '.cognito_issuer_url.value // empty')
    COGNITO_CLIENT_ID=$(echo "$outputs" | jq -r '.cognito_client_id.value // empty')
    COGNITO_HOSTED_UI=$(echo "$outputs" | jq -r '.cognito_hosted_ui_domain.value // empty')

    # notification-worker SES IRSA (R24-S7) — enable_notification_ses=true 로 apply
    # 된 스택에만 존재. EMAIL_SENDER_TYPE=ses 인데 이 값이 비어 있으면 SA 에 role 이
    # 없어 발송이 전량 실패하므로 그 조합을 여기서 잡는다.
    NOTIFICATION_SES_ROLE_ARN=$(echo "$outputs" | jq -r '.notification_worker_ses_role_arn.value // empty')

    popd >/dev/null

    # 필수 값 검증
    for var in CLUSTER_NAME GATEWAY_ROLE_ARN ADMIN_API_ROLE_ARN AURORA_HOST REDIS_HOST; do
        if [ -z "${!var}" ] || [ "${!var}" = "null" ]; then
            err "Terraform output 누락: $var"
            exit 1
        fi
    done

    ok "Terraform outputs 추출 완료"
    echo "    cluster_name           : $CLUSTER_NAME"
    echo "    gateway_proxy_role_arn : $GATEWAY_ROLE_ARN"
    echo "    admin_api_role_arn     : $ADMIN_API_ROLE_ARN"
    echo "    aurora_endpoint        : $AURORA_HOST"
    echo "    elasticache_endpoint   : $REDIS_HOST"
    echo "    cognito_issuer_url     : ${COGNITO_ISSUER_URL:-'(없음 — OIDC 비활성)'}"
    echo "    cognito_client_id      : ${COGNITO_CLIENT_ID:-'(없음)'}"
    echo "    cognito_hosted_ui_domain: ${COGNITO_HOSTED_UI:-'(없음)'}"
}

# ---- kubectl context 설정 ----
setup_kubeconfig() {
    info "kubectl context 설정"
    aws eks update-kubeconfig \
        --region "$AWS_REGION" \
        --name "$CLUSTER_NAME" \
        --alias "$CLUSTER_NAME"
    ok "kubectl context = $CLUSTER_NAME"
}

# ---- 네임스페이스 준비 ----
ensure_namespace() {
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        ok "Namespace '$NAMESPACE' 이미 존재"
    else
        info "Namespace '$NAMESPACE' 생성"
        kubectl create namespace "$NAMESPACE"
        ok "Namespace 생성 완료"
    fi
}

# ---- ClusterSecretStore 생성 (ESO 설치 직후 한 번만 필요) ----
ensure_cluster_secret_store() {
    info "ClusterSecretStore 'aws-secrets-manager' 확인"

    # ESO Pod Ready 대기
    if ! kubectl wait --for=condition=Ready pods \
           -l app.kubernetes.io/name=external-secrets \
           -n external-secrets --timeout=120s >/dev/null 2>&1; then
        warn "External Secrets Operator Pod 가 아직 Ready 아닙니다. 대기 후 재시도 하세요."
    fi

    if kubectl get clustersecretstore aws-secrets-manager >/dev/null 2>&1; then
        ok "ClusterSecretStore 이미 존재"
        return
    fi

    info "ClusterSecretStore 생성"
    if ! kubectl apply -f - <<EOF 2>&1 | grep -v "failed calling webhook"
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
    then
        # ESO v0.10.x 에서 webhook TLS cert SAN 이 Pod IP 로 발급되어 검증 실패하는
        # 알려진 이슈. validating webhook 을 failurePolicy=Ignore 로 완화 후 재시도.
        # 이후 webhook 이 안정화되면 수동으로 failurePolicy=Fail 로 되돌릴 수 있음.
        warn "validating webhook 호출 실패. failurePolicy=Ignore 로 우회 후 재시도"
        for vwc in secretstore-validate externalsecret-validate; do
            if kubectl get validatingwebhookconfiguration "$vwc" >/dev/null 2>&1; then
                count=$(kubectl get validatingwebhookconfiguration "$vwc" \
                    -o jsonpath='{range .webhooks[*]}{.name}{"\n"}{end}' | wc -l | tr -d ' ')
                for i in $(seq 0 $((count-1))); do
                    kubectl patch validatingwebhookconfiguration "$vwc" --type='json' \
                        -p="[{\"op\":\"replace\",\"path\":\"/webhooks/$i/failurePolicy\",\"value\":\"Ignore\"}]" \
                        >/dev/null 2>&1 || true
                done
            fi
        done
        kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
    fi
    ok "ClusterSecretStore 생성 완료"
}

# ---- helm install 직전 ExternalSecret 관련 잔존물 정리 ----
# 두 가지 이슈를 선제적으로 방어 (troubleshooting.md 참조):
#   1) ESO webhook cert SAN 불일치 → helm pre-install hook 의 ExternalSecret 검증 실패
#   2) 이전 failed install 에서 남은 K8s Secret 이 새 ExternalSecret 과 owner UID 충돌
# 한 번 해결되면 재발 적지만, Fargate 첫 배포 / 재배포 사이클에서 자주 재현.
preinstall_cleanup_es_artifacts() {
    info "helm install 직전 ExternalSecret 잔존물 정리"

    # (a) ESO validating webhook 을 임시 삭제 — cert SAN race 회피.
    #     webhook 은 ExternalSecret/SecretStore CR 생성 시점 schema 검증 용도.
    #     런타임 sync 와 무관하므로 제거해도 안전. ESO cert-controller 가 몇 분 내 재생성.
    #     ⚠️ install 시에만 — upgrade 때마다 지우면 cert-controller 재생성 전까지
    #        ES/SecretStore schema 검증이 꺼진다(R24-N13).
    if [ "$HELM_ACTION" = "install" ]; then
        for vwc in externalsecret-validate secretstore-validate; do
            if kubectl get validatingwebhookconfiguration "$vwc" >/dev/null 2>&1; then
                kubectl delete validatingwebhookconfiguration "$vwc" >/dev/null 2>&1 && \
                    ok "validating webhook '$vwc' 임시 삭제 (cert-controller 가 재생성 예정)"
            fi
        done
    fi

    # (b) Owner 시대(creationPolicy: Owner)에 만들어진 Secret 의 stale ownerReference
    #     제거 — R24-S1 로 ES 가 Orphan 이 됐지만, 기존 Secret 은 여전히 삭제된 ES 의
    #     UID 를 가리켜 GC 가 Secret 을 지운다(Orphan 전환 첫 upgrade 창).
    #     Secret 자체는 ESO 가 갱신하므로 지우지 말고 ownerReferences 만 뗀다.
    for secret_name in llm-gateway-app llm-gateway-db llm-gateway-redis; do
        local owner_kind
        owner_kind=$(kubectl -n "$NAMESPACE" get secret "$secret_name" \
            -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || echo "")
        if [ "$owner_kind" = "ExternalSecret" ]; then
            kubectl -n "$NAMESPACE" patch secret "$secret_name" --type=json \
                -p='[{"op":"remove","path":"/metadata/ownerReferences"}]' >/dev/null 2>&1 && \
                ok "Secret '$secret_name' 의 stale ownerReference 제거 (Orphan 전환)"
        fi
    done
}

# ---- Secrets Manager 값을 ExternalSecret 이 자동으로 가져오도록 secret path 확인 ----
check_secrets_manager() {
    info "Secrets Manager 경로 확인 (/llm-gateway/${ENV}/...)"
    local missing=()
    for path in "app" "db" "redis"; do
        local secret_path="/llm-gateway/${ENV}/${path}"
        if ! aws secretsmanager describe-secret --secret-id "$secret_path" >/dev/null 2>&1; then
            missing+=("$secret_path")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        warn "Secrets Manager에 아래 경로의 Secret이 없습니다:"
        for s in "${missing[@]}"; do echo "    - $s"; done
        echo ""
        echo "    다음 중 한 가지로 생성하세요:"
        echo ""
        echo "    1) DB 비번 (Aurora가 이미 저장한 값을 복제):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/db \\"
        echo "         --secret-string '{\"password\":\"\$(aws secretsmanager get-secret-value --secret-id $AURORA_SECRET_ARN --query SecretString --output text | jq -r .password)\",\"notification_worker_password\":\"CHANGE_ME\"}'"
        echo ""
        echo "    2) Redis AUTH (ElastiCache가 이미 저장):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/redis \\"
        echo "         --secret-string '{\"password\":\"\$(aws secretsmanager get-secret-value --secret-id $REDIS_AUTH_SECRET_ARN --query SecretString --output text)\"}'"
        echo ""
        echo "    3) App secrets (직접 생성):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/app \\"
        echo "         --secret-string '{\"virtual_key_encryption_key\":\"\$(openssl rand -hex 32)\",\"nextauth_secret\":\"\$(openssl rand -hex 32)\",\"jwt_jwks_cache_key\":\"\$(openssl rand -hex 32)\",\"internal_api_token\":\"\$(openssl rand -hex 32)\"}'"
        echo "       ⚠️ internal_api_token 필수(R24-M2): ExternalSecret 이 속성 하나라도"
        echo "          없으면 Secret 전체 sync 가 실패해 gateway/admin-api/admin-ui 전부 못 뜬다."
        echo ""
        read -p "    계속 진행 (y) / 중단 (N)? " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        ok "Secrets Manager 경로 존재 확인 완료"
    fi
}

# ---- Observability 스택 (HPA + 관측) ----
# EKS Fargate 에선 metrics-server 가 kubelet authz 제약으로 동작하지 않음.
# 따라서 prometheus-adapter 가 metrics.k8s.io API 를 서빙하며, 이는 사전에
# kube-prometheus-stack 이 pod CPU/memory 를 수집하고 있어야 함.
# 순서: 1) kube-prometheus-stack → 2) prometheus-adapter.
# 자세한 배경은 deployment/observability/README.md 참조.
ensure_observability() {
    info "Observability 스택 확인/설치 (Fargate HPA + 관측 공통)"
    bash "$DEPLOY_DIR/observability/kube-prometheus-stack/install.sh"
    bash "$DEPLOY_DIR/observability/prometheus-adapter/install.sh"
    bash "$DEPLOY_DIR/observability/otel-collector/install.sh"
}

# ---- Helm install / upgrade ----
# SET_ARGS 는 helm_install 이 조립하고 patch_nextauth_url 이 재사용하므로 전역.
declare -a SET_ARGS=()

# ---- 과거 patch_nextauth_url 이 남긴 field ownership 정리 ----
# 증상: helm upgrade 가 이 에러로 실패.
#   conflict with "helm" using apps/v1:
#     .spec.template.spec.containers[name="admin-ui"].env[name="NEXTAUTH_URL"].value
#
# 원인: k8s server-side apply 의 소유권 키는 manager *이름* 이 아니라
# (이름, operation) 쌍이다. `kubectl set env` 는 operation=Update 로 기록되고,
# helm 은 operation=Apply 로 기록한다. 따라서 과거 코드의 `--field-manager=helm` 은
# 소유권을 helm 에 넘겨준 게 아니라, 이름만 같고 operation 이 다른 *두 번째* 소유자를
# 만들었다. 에러 메시지의 `using apps/v1` 접미사가 그 증거 —
# k8s 는 충돌 상대가 Update 소유자일 때만 이 접미사를 붙인다.
#
# 조치: 그 Update 항목만 managedFields 에서 제거해 helm(Apply) 단독 소유로 되돌린다.
# `--force-conflicts` 를 쓰지 않는 이유: 그건 릴리즈 전체에 적용돼서, HPA 가 소유한
# .spec.replicas 까지 helm 이 덮어써 스케일이 되돌아간다 (이 클러스터는 HPA 사용).
prune_stale_field_managers() {
    local deploy="${RELEASE_NAME}-admin-ui"
    kubectl get deployment "$deploy" -n "$NAMESPACE" >/dev/null 2>&1 || return 0

    # operation=Update 이면서 manager 가 helm / kubectl-set 인 항목의 인덱스.
    # 뒤에서부터 지워야 앞쪽 인덱스가 밀리지 않는다.
    local idxs
    idxs=$(kubectl get deployment "$deploy" -n "$NAMESPACE" -o json 2>/dev/null \
        | jq -r '[.metadata.managedFields | to_entries[]
                  | select(.value.operation == "Update")
                  | select(.value.manager == "helm" or .value.manager == "kubectl-set")
                  | .key] | reverse | .[]' 2>/dev/null || true)
    [ -z "$idxs" ] && return 0

    warn "admin-ui 에 과거 'kubectl set env' 소유권 잔존 → 정리 (helm 충돌 예방)"
    local i
    for i in $idxs; do
        kubectl patch deployment "$deploy" -n "$NAMESPACE" --type=json \
            -p="[{\"op\":\"remove\",\"path\":\"/metadata/managedFields/$i\"}]" \
            >/dev/null 2>&1 || true
    done
    ok "admin-ui field ownership 정리 완료"
}

helm_install() {
    info "Helm ${HELM_ACTION} 실행 중"

    # ECR 레지스트리 주소 — *순수 registry URL* (계정.dkr.ecr.region.amazonaws.com)
    # chart values.yaml 의 image.repository 가 이미 "llm-gateway/<service>" prefix 를
    # 포함하므로, registry 에는 llm-gateway 를 넣으면 안 됨. 넣으면 pull 경로가
    # .../llm-gateway/llm-gateway/<service>:<tag> 로 중복되어 ImagePullBackOff.
    local aws_account_id
    aws_account_id=$(aws sts get-caller-identity --query Account --output text)
    local ecr_registry="${aws_account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    # SSRF 방어용 STS 허용 리전(admin-api VK 발급). 클라이언트가 GetCallerIdentity 를
    # presign 하는 곳 = 배포 리전이므로 배포 리전이 반드시 목록에 있어야 한다.
    # (values 기본값이 ap-northeast-2 라, 안 넣으면 다른 리전 배포에서 VK 발급 거부.)
    #
    # 단 helm 의 --set/-f 는 리스트를 병합하지 않고 통째로 치환한다. 배포 리전만
    # 넘기면 values 에 적어 둔 다중 리전이 조용히 잘리므로(= SSRF allowlist 축소),
    # 지금 values 로 렌더되는 값을 그대로 읽어 배포 리전을 합집합으로 더한다.
    # 읽기는 helm 에게 맡긴다 — YAML 을 직접 파싱하면 인라인 주석·flow 스타일
    # (["a","b"])·차트 기본값 상속에서 조용히 틀린 목록이 나온다(실제로 겪음).
    local rendered
    if ! rendered=$(helm template "$RELEASE_NAME" "$CHART_DIR" -f "$VALUES_FILE" 2>&1); then
        err "helm template 실패 — values 를 렌더할 수 없어 STS 허용 리전을 확정할 수 없음:"
        printf '%s\n' "$rendered" | tail -5 >&2
        exit 1
    fi
    # 아래 두 곳은 파이프 대신 herestring 을 쓴다 — `printf | grep -q`/`| awk …exit` 는
    # 소비자가 먼저 닫아 printf 가 SIGPIPE 로 죽고, set -o pipefail 때문에 "못 찾음"
    # 으로 잘못 판정된다(입력 크기에 따라 간헐적으로 재현).
    if [[ "$rendered" != *"name: ALLOWED_STS_REGIONS"* ]]; then
        err "렌더 결과에 ALLOWED_STS_REGIONS 가 없음 — 차트와 스크립트가 어긋났는지 확인 필요"
        exit 1
    fi
    local sts_current
    sts_current=$(awk '/name: ALLOWED_STS_REGIONS/{getline; sub(/.*value: */,""); gsub(/"/,""); print; exit}' <<<"$rendered")
    STS_REGIONS_CSV=$(printf '%s\n%s\n' "${sts_current//,/$'\n'}" "$AWS_REGION" |
        awk 'NF && !seen[$0]++' | paste -sd, -)

    # --set 명령 조립
    SET_ARGS=(
        --set "global.imageRegistry=${ecr_registry}"
        --set "aws.region=${AWS_REGION}"
        # SSRF 방어용 STS 허용 리전(admin-api VK 발급). 클라이언트가 GetCallerIdentity 를
        # presign 하는 곳 = 배포 리전이므로 aws.region 과 같은 값으로 자동 주입한다.
        # (values 기본값이 ap-northeast-2 라, 안 주입하면 다른 리전 배포에서 VK 발급 거부.)
        --set "aws.allowedStsRegions={${STS_REGIONS_CSV}}"
        # VK 발급 허용 AWS 계정 — 배포자 계정을 기본으로. role 이름 검사는
        # 공격자 계정의 동명 role/IAM user 로 우회되므로 계정 경계가 필요하다.
        --set "aws.allowedAwsAccounts={${aws_account_id}}"
        --set "database.external.host=${AURORA_HOST}"
        --set "database.external.name=${AURORA_DB_NAME}"
        --set "redis.external.host=${REDIS_HOST}"
        --set "redis.external.tls=true"
        --set-string "gatewayProxy.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${GATEWAY_ROLE_ARN}"
        --set-string "adminApi.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${ADMIN_API_ROLE_ARN}"
    )

    # notification-worker SES IRSA — terraform 에 role 이 만들어져 있을 때만 주입.
    # provider=ses 인데 role 이 없는 조합은 발송 전량 실패라 미리 경고한다(R24-S7).
    if [ -n "${NOTIFICATION_SES_ROLE_ARN:-}" ] && [ "$NOTIFICATION_SES_ROLE_ARN" != "null" ]; then
        SET_ARGS+=(--set-string "notificationWorker.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${NOTIFICATION_SES_ROLE_ARN}")
    elif grep -qE 'provider:\s*"ses"' "$VALUES_FILE" 2>/dev/null; then
        warn "notificationWorker.email.provider=ses 인데 notification_worker_ses_role_arn output 이 없다 —"
        warn "terraform tfvars 의 enable_notification_ses=true 로 apply 후 재실행할 것 (발송 전량 실패 예정)"
    fi

    # OIDC (Cognito) — terraform 에서 만든 경우 자동 주입.
    # ⚠️ 우선순위: --set 은 어떤 -f values 파일보다 항상 이긴다. R16 게이트로
    #    OIDC_ISSUER_URL 이 설정된 환경에선 무서명 dev 토큰이 전부 거부되므로,
    #    dev-login 의도 환경(DEV_LOGIN_ENABLED=true)을 이 스크립트+terraform
    #    issuer 로 배포하면 dev 토큰이 전부 401 이 된다. dev-login 배포는
    #    이 스크립트가 아니라 values-eks-fargate-*.local.yaml 직접 helm 경로를
    #    쓸 것(그 파일은 issuerUrl 을 의도적으로 비워 둔다).
    if [ -n "${COGNITO_ISSUER_URL}" ]; then
        SET_ARGS+=(
            --set "adminApi.oidc.enabled=true"
            --set "adminApi.oidc.issuerUrl=${COGNITO_ISSUER_URL}"
            --set "adminApi.oidc.providerName=oidc:cognito"
            --set "adminApi.oidc.groupsClaim=cognito:groups"
        )
        # audience — Cognito app client_id. 비우면 aud 검증이 꺼져 같은 풀의
        # 다른 app client 토큰도 통과한다(R16). client_id 가 있을 때만 주입.
        if [ -n "${COGNITO_CLIENT_ID}" ]; then
            SET_ARGS+=(
                --set "adminApi.oidc.audience=${COGNITO_CLIENT_ID}"
            )
        fi
        # 다시 if 로 닫지 않고 dummy true (아래 fi 와 짝 맞춤 위해)
        true
    fi

    # Migration Job 은 pre-install hook 으로 실행됨:
    #   1. ExternalSecret pre-install hook weight -30 → ESO 가 K8s Secret sync
    #   2. migration ServiceAccount + Role + RoleBinding pre-install weight -10
    #   3. migration Job pre-install weight -5
    #      - initContainer 가 Secret 존재 여부를 최대 5분 대기
    #      - Secret 생기면 main container (alembic upgrade) 실행
    # 전체가 helm install 한 번에 끝남. 비활성화 하려면 MIGRATION_ENABLED=false env 사용.
    if [ "${MIGRATION_ENABLED:-true}" != "true" ]; then
        warn "Migration Job 비활성화 (MIGRATION_ENABLED=false). 별도 수동 실행 필요"
        SET_ARGS+=(--set "migration.enabled=false")
    fi

    # ---- 이미지 태그의 단일 소스: 라이브 릴리스 (R24-M3) ----
    # 커밋된 values-eks-fargate-*.yaml 의 image.tag 핀은 빌드 시점 스냅샷이라 라이브
    # (SHA 태그)보다 거의 항상 뒤처진다. -f values 그대로 upgrade 하면:
    #   1) 전 서비스가 조용히 다운그레이드되고
    #   2) 구 migration 이미지는 alembic head 가 DB 현재 리비전보다 낮아
    #      pre-upgrade hook 이 "Can't locate revision" 으로 실패한다(실사고 기록).
    # 릴리스가 이미 있으면 저장 values 의 서비스별 태그를 읽어 그대로 이어 쓴다.
    # 새 태그로 올릴 때는 rebuild-image.sh(DEPLOY=1) 또는 IMAGE_TAG=<sha> env 사용.
    IMAGE_KEYS=(gatewayProxy adminApi adminUi notificationWorker costRecorderWorker migration scheduler)
    # 키 → ECR 리포지토리 이름 (llm-gateway/<repo>) — IMAGE_TAG preflight 에서 사용.
    # 연관 배열(declare -A)은 macOS 기본 bash 3.2 에서 없다 — case 함수로 (R24-S6r3).
    ecr_repo() {
        case "$1" in
            gatewayProxy)        echo gateway-proxy ;;
            adminApi|scheduler)  echo admin-api ;;
            adminUi)             echo admin-ui ;;
            notificationWorker)  echo notification-worker ;;
            costRecorderWorker)  echo cost-recorder-worker ;;
            migration)           echo migration ;;
            *) echo "BUG: ecr_repo 미등록 키 $1" >&2; return 1 ;;
        esac
    }
    live_values='{}'
    if [ "$HELM_ACTION" = "upgrade" ]; then
        # helm get values 실패 시 '{}' 폴백은 전 서비스를 values 핀(구 태그)으로
        # 조용히 다운그레이드시킨다(R24-S1r2) — 조용한 폴백 대신 중단.
        if ! live_values="$(helm get values "$RELEASE_NAME" -n "$NAMESPACE" -o json 2>/dev/null)"; then
            err "helm get values 실패 — 라이브 태그를 읽지 못하면 values 핀으로 다운그레이드된다."
            err "RBAC/네트워크를 확인하고 재시도할 것. 의도적으로 핀을 쓰려면 IMAGE_TAG=<sha> 지정."
            exit 1
        fi
        for key in "${IMAGE_KEYS[@]}"; do
            live_tag="$(jq -r --arg k "$key" '.[$k].image.tag // empty' <<<"$live_values" 2>/dev/null || true)"
            if [ -n "$live_tag" ]; then
                SET_ARGS+=(--set "$key.image.tag=$live_tag")
            else
                warn "라이브 values 에 $key.image.tag 없음 — values 핀을 그대로 쓴다 (부분 다운그레이드 위험)"
            fi
        done
    fi
    # IMAGE_TAG 는 install/upgrade 모두에 적용한다 — upgrade 블록 안에 두면 첫
    # install 에서 태그가 무시돼 values 의 stale 핀이 그대로 배포된다(R24-S1r2).
    if [ -n "${IMAGE_TAG:-}" ]; then
        warn "IMAGE_TAG=$IMAGE_TAG 지정 — 전 서비스를 이 태그로 올린다 (라이브 태그 무시)"
        # ECR preflight (R24-N4r2): IMAGE_TAG 는 7개 리포지토리 전부에 그 태그가
        # 있어야 한다. rebuild-image 는 서비스 단위 빌드라 보통 성립하지 않는다 —
        # 없는 태그로 upgrade 하면 ImagePullBackOff 후 atomic 롤백만 된다.
        # ImageNotFound 만 명확한 실패로 보고 중단, 나머지 에러(권한/네트워크)는 경고.
        aws_account_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
        if [ -n "$aws_account_id" ]; then
            missing=()
            for key in "${IMAGE_KEYS[@]}"; do
                repo="llm-gateway/$(ecr_repo "$key")"
                out=$(aws ecr describe-images --repository-name "$repo" \
                        --image-ids imageTag="$IMAGE_TAG" --query 'imageIds[0]' \
                        --output text 2>&1) && continue
                if grep -q "ImageNotFoundException\|RepositoryNotFoundException" <<<"$out"; then
                    missing+=("$repo:$IMAGE_TAG")
                else
                    warn "ECR 조회 실패($repo) — 태그 존재 확인 건너뜀: ${out%%$'\n'*}"
                fi
            done
            if [ ${#missing[@]} -gt 0 ]; then
                err "IMAGE_TAG=$IMAGE_TAG 가 다음 리포지토리에 없다:"
                printf '    %s\n' "${missing[@]}" >&2
                err "서비스별로 태그가 다르면 rebuild-image.sh DEPLOY=1 경로를 쓸 것."
                exit 1
            fi
        fi
        for key in "${IMAGE_KEYS[@]}"; do
            SET_ARGS+=(--set "$key.image.tag=$IMAGE_TAG")
        done
    fi

    # ---- migration 이미지 head 게이트 (R24-M3/S5, 기준 수정 S2r2) ----
    # 비교 기준은 HEAD 가 아니라 **이번에 배포될 앱 태그** 다. upgrade 는 앱 태그를
    # 라이브 그대로 유지하고 IMAGE_TAG 는 mig/app 을 같은 태그로 올리므로,
    # HEAD 와 비교하면 롤백·values-only upgrade 가 전부 오탐으로 막힌다.
    # 진짜 위험은 "앱 태그가 기대하는 스키마가 migration 이미지에 없는" 조합 —
    # mig_tag..app_tag 사이에 db/ 변경이 있을 때다.
    values_tag() { # $1=values 키 → values 파일의 image.tag
        awk -v k="^$1:" '$0~k{f=1;next} f&&/^[a-zA-Z]/{exit} f&&/tag:/{gsub(/["'\'' ]/,"",$2);print $2;exit}' \
            "$VALUES_FILE" 2>/dev/null || true
    }
    mig_tag="${IMAGE_TAG:-$(jq -r '.migration.image.tag // empty' <<<"$live_values" 2>/dev/null || true)}"
    mig_tag="${mig_tag:-$(values_tag migration)}"
    app_tag="${IMAGE_TAG:-$(jq -r '.gatewayProxy.image.tag // empty' <<<"$live_values" 2>/dev/null || true)}"
    app_tag="${app_tag:-$(values_tag gatewayProxy)}"
    # mig→app 사이에 db/versions/ 에 *추가된* 파일이 있는지만 본다
    # (--diff-filter=A). three-dot 은 merge-base 없는 shallow clone 이나,
    # 같은 db 트리를 다른 이력으로 만든 squash-merge 에서 오탐을 낸다
    # (R24-N1r5). 새 revision 파일 추가가 유일한 유해 신호다 — 기존 마이그레이션
    # 파일은 불변.
    if [ -n "$mig_tag" ] && [ -n "$app_tag" ] && [ "$mig_tag" != "$app_tag" ] \
       && git -C "$DEPLOY_DIR/.." rev-parse --verify --quiet "${mig_tag}^{commit}" >/dev/null 2>&1 \
       && git -C "$DEPLOY_DIR/.." rev-parse --verify --quiet "${app_tag}^{commit}" >/dev/null 2>&1 \
       && ! git -C "$DEPLOY_DIR/.." diff --quiet --diff-filter=A "$mig_tag" "$app_tag" -- db/versions/ 2>/dev/null; then
        err "migration 태그($mig_tag)와 앱 태그($app_tag) 사이에 db/ 변경이 있다 —"
        err "구 migration 이미지로는 앱이 기대하는 revision 을 모른다."
        err "먼저 DEPLOY=1 deployment/scripts/rebuild-image.sh migration $ENV 로 스키마 이미지부터 배포할 것."
        exit 1
    fi

    # helm upgrade --install 로 통합: release 없으면 install, 있으면 upgrade.
    # `--cleanup-on-fail` 은 upgrade 경로에서만 유효한 플래그라서, install 모드에서
    # 별도 helm install 명령을 쓰면 에러. upgrade --install 로 통일하면 항상 OK.
    # rollback 플래그 분기는 v3 때문에 필요하다: v3 에는 --rollback-on-failure 가 아예
    # 없어 unknown flag 로 죽는다. 반대로 v4 의 --atomic 은 deprecation warning 일 뿐
    # 여전히 동작한다. 사전 요구사항이 Helm >=3.14 이므로 런타임에 감지해 골라 쓴다.
    HELM_MAJOR=$(helm version --template '{{.Version}}' 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')
    if [ "${HELM_MAJOR:-3}" -ge 4 ]; then
        ROLLBACK_FLAG="--rollback-on-failure"
    else
        ROLLBACK_FLAG="--atomic"
    fi
    #
    # DEBUG_MODE=true 로 실행하면 실패 시 rollback/cleanup 을 하지 않아 실패한 Pod 와
    # Deployment 가 그대로 남음 → `kubectl logs <pod> --previous` 로 crash 원인 조사 가능.
    if [ "${DEBUG_MODE:-false}" = "true" ]; then
        warn "DEBUG_MODE=true → 실패 시 rollback 하지 않음 (리소스 유지, logs 조사 가능)"
        helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
            --namespace "$NAMESPACE" \
            --values "$VALUES_FILE" \
            "${SET_ARGS[@]}" \
            --timeout 15m \
            --wait
    else
        if [ "${FORCE_CONFLICTS:-false}" = "true" ]; then
            warn "FORCE_CONFLICTS=true → server-side apply 충돌을 helm 이 takeover"
            helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
                --namespace "$NAMESPACE" \
                --values "$VALUES_FILE" \
                "${SET_ARGS[@]}" \
                --force-conflicts \
                "$ROLLBACK_FLAG" \
                --cleanup-on-fail \
                --timeout 15m \
                --wait
        else
            helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
                --namespace "$NAMESPACE" \
                --values "$VALUES_FILE" \
                "${SET_ARGS[@]}" \
                "$ROLLBACK_FLAG" \
                --cleanup-on-fail \
                --timeout 15m \
                --wait
        fi
    fi

    ok "Helm ${HELM_ACTION} 완료"
}

# ---- Post-install: admin-ui NEXTAUTH_URL 패치 (방식 A) ----
# 방식 A (도메인 없음) 에선 ALB DNS 가 helm install 후에야 확정됨 → ingress.adminUi.host 가
# 빈 값이라 NEXTAUTH_URL 이 "http://" 로 잘려버림. 여기서 ALB DNS 를 읽어 env 업데이트.
patch_nextauth_url() {
    info "admin-ui NEXTAUTH_URL 패치 (ALB DNS 기반)"

    # 가드: chart 가 ingress.adminUi.host 로부터 NEXTAUTH_URL 을 이미 결정한 경우
    # (도메인 모드, 방식 B) 에는 ALB DNS 패치가 불필요. host 가 비어 있을 때만
    # chart 가 "http://" / "https://" 만 렌더 (host 자리 비어서 잘림) → 그 경우에만 진행.
    # 효과: 두 manager(helm SSA vs kubectl set env)가 같은 필드를 다투는 충돌을 영구 차단.
    local rendered
    rendered=$(helm template "$RELEASE_NAME" "$CHART_DIR" --values "$VALUES_FILE" \
        --show-only templates/admin-ui/deployment.yaml 2>/dev/null \
        | awk '/name: NEXTAUTH_URL/{f=1;next} f && /value:/{sub(/^[[:space:]]*value:[[:space:]]*/,"");gsub(/^"|"$/,"");print;exit}' \
        || true)
    if [[ ! "$rendered" =~ ^https?://$ ]]; then
        ok "NEXTAUTH_URL 이 chart 에서 결정됨 (${rendered}) — post-install 패치 스킵"
        return
    fi

    local alb_dns=""
    local attempt=0
    while [ $attempt -lt 60 ]; do
        alb_dns=$(kubectl get ingress "${RELEASE_NAME}-admin-ui" -n "$NAMESPACE" \
            -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo '')
        [ -n "$alb_dns" ] && break
        sleep 3
        attempt=$((attempt + 1))
    done
    if [ -z "$alb_dns" ]; then
        warn "admin-ui ALB DNS 를 3분 내 확보 실패 — NEXTAUTH_URL 수동 패치 필요"
        return
    fi
    # scheme 은 차트가 렌더한 값을 그대로 쓴다 — HTTPS 리스너(https://)인데
    # http:// 를 넣으면 ssl-redirect + Secure 쿠키 조합에서 콜백이 깨진다.
    local nextauth_url="${rendered%%://*}://${alb_dns}"
    info "NEXTAUTH_URL=${nextauth_url}"
    # 현재 deployment 의 NEXTAUTH_URL 과 비교해 필요할 때만 업데이트 (idempotent)
    local current
    current=$(kubectl get deployment "${RELEASE_NAME}-admin-ui" -n "$NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="NEXTAUTH_URL")].value}' 2>/dev/null || echo '')
    if [ "$current" = "$nextauth_url" ]; then
        ok "NEXTAUTH_URL 이미 올바름 — 스킵"
        return
    fi

    # `kubectl set env` 대신 helm upgrade 로 되돌린다 (--set adminUi.nextauthUrl=...).
    # chart 의 default 표현식이 이 값을 그대로 쓰므로 결과 env 는 동일하고,
    # 소유자는 helm(Apply) 하나로 유지된다 → 다음 배포에서 충돌이 발생하지 않는다.
    # (과거 kubectl set env 방식이 만든 Update 소유자가 바로 이번 배포 실패의 원인.)
    #
    # migration.enabled=false: 방금 같은 실행의 helm_install 이 이미 alembic upgrade 를
    # 돌렸다. 이 두 번째 upgrade 는 env 한 개만 바꾸는 것이므로 pre-upgrade
    # migration hook 을 다시 띄울 이유가 없다 (재실행은 no-op 이지만 배포가 수 분 길어짐).
    # ⚠️ --set 은 릴리즈 저장 values 에 누적된다 — 이후 `helm upgrade --reuse-values`
    #    로 수동 업그레이드하면 migration hook 이 꺼진 채로 남는다(실측: rev175 가
    #    0045 를 적용 못 함). 수동 upgrade 는 `--set migration.enabled=true` 를 함께
    #    주거나 install-eks.sh 를 다시 쓸 것(비-reuse 경로는 chart 기본값 true 로
    #    자동 복구된다).
    # DEBUG_MODE 계약("실패 시 rollback 하지 않고 리소스 유지")은 이 경로에도
    # 적용한다 — 여기만 --atomic 을 붙이면 디버깅용 리소스가 롤백으로 사라진다.
    if [ "${DEBUG_MODE:-false}" = "true" ]; then
        helm upgrade "$RELEASE_NAME" "$CHART_DIR" \
            --namespace "$NAMESPACE" \
            --values "$VALUES_FILE" \
            "${SET_ARGS[@]}" \
            --set "adminUi.nextauthUrl=${nextauth_url}" \
            --set "migration.enabled=false" \
            --timeout 5m --wait
    else
        helm upgrade "$RELEASE_NAME" "$CHART_DIR" \
            --namespace "$NAMESPACE" \
            --values "$VALUES_FILE" \
            "${SET_ARGS[@]}" \
            --set "adminUi.nextauthUrl=${nextauth_url}" \
            --set "migration.enabled=false" \
            "${ROLLBACK_FLAG:---atomic}" \
            --cleanup-on-fail \
            --timeout 5m \
            --wait
    fi
    ok "admin-ui NEXTAUTH_URL 패치 완료"
}

# ---- 배포 상태 확인 ----
verify_deployment() {
    info "Deployment 상태 확인"
    kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}" -o wide
    echo ""
    info "Ingress (ALB 생성에 1~2분 소요)"
    kubectl get ingress -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}"
    echo ""
    ok "모든 Pod가 Ready 상태여야 합니다. 아닌 경우:"
    echo "    kubectl describe pod <pod-name> -n $NAMESPACE"
    echo "    kubectl logs <pod-name> -n $NAMESPACE --tail=50"
}

# ---- main ----
main() {
    echo "=============================================================="
    echo "  LLM Gateway — EKS Fargate ${ENV} 배포"
    echo "=============================================================="

    check_prereqs

    # kubeconfig 를 먼저 맞춘다 — 이전 context(dev/prod 혼동)로 helm status 를 조회하면
    # 릴리스 유무를 잘못 판단한다(R24-N10).
    read_tf_outputs
    setup_kubeconfig

    # 이미 설치되어 있으면 upgrade, 없으면 install
    if helm status "$RELEASE_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
        HELM_ACTION="upgrade"
        info "기존 릴리즈 감지 → upgrade 모드"
    else
        HELM_ACTION="install"
        info "신규 설치 모드"
    fi

    ensure_namespace
    ensure_observability
    ensure_cluster_secret_store
    check_secrets_manager
    preinstall_cleanup_es_artifacts
    prune_stale_field_managers
    helm_install
    patch_nextauth_url
    verify_deployment

    echo ""
    ok "배포 완료"
    echo ""
    echo "다음 단계:"
    echo "    ./scripts/smoke-test.sh --env $ENV"
    echo ""
}

main "$@"