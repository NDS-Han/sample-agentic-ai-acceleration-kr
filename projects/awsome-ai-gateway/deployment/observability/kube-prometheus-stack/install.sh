#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# kube-prometheus-stack 설치 — production 관측성 + HPA 메트릭 소스
# ----------------------------------------------------------------------------
# 역할:
#   1. Prometheus: pod CPU/memory scrape (cAdvisor 경유), 장기 메트릭 보존
#   2. Grafana: 대시보드 / 알람 UI
#   3. kube-state-metrics: K8s 리소스 (Deployment/Pod/HPA) 메타 지표
#   4. **HPA 메트릭 소스**: EKS Fargate 에서 metrics-server 가 작동하지 않으므로
#      prometheus-adapter 가 이 Prometheus 를 읽어 metrics.k8s.io 를 서빙.
#
# 전제: Grafana IRSA role (Terraform 에서 생성됨). GRAFANA_ROLE_ARN env 또는
#       terraform output(prod) 에서 자동 읽음.
# Idempotent: 이미 설치돼 있으면 upgrade, 없으면 install.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

KPS_VERSION="${KPS_VERSION:-60.1.0}"
NS="${NS:-observability}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }

# ─── Grafana IRSA role ARN (terraform output 또는 env) ─────────────────
if [ -z "${GRAFANA_ROLE_ARN:-}" ]; then
  info "GRAFANA_ROLE_ARN 미설정 → terraform output 에서 조회"
  if GRAFANA_ROLE_ARN=$(terraform -chdir="../../terraform/environments/${ENV:-prod}" output -raw grafana_role_arn 2>/dev/null); then
    ok "GRAFANA_ROLE_ARN = $GRAFANA_ROLE_ARN"
  else
    warn "Grafana IRSA 미설정. CloudWatch 데이터소스를 사용할 수 없습니다 (Prometheus 메트릭은 정상)."
    warn "  필요 시: GRAFANA_ROLE_ARN=<ARN> 환경변수로 전달"
    GRAFANA_ROLE_ARN=""
  fi
fi

# ─── Namespace (observability 는 공용) ─────────────────────────────────
kubectl apply -f ../namespace.yaml

# ─── Grafana admin Secret (최초 1 회만 생성) ───────────────────────────
if ! kubectl -n "$NS" get secret kps-grafana-admin >/dev/null 2>&1; then
  info "Grafana admin Secret 최초 생성"
  bash ../admin-secret.sh
fi

# ─── Helm repo ─────────────────────────────────────────────────────────
info "Helm repo 추가 (idempotent)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

# ─── Install / Upgrade ─────────────────────────────────────────────────
if helm status kps -n "$NS" >/dev/null 2>&1; then
    info "기존 릴리즈 감지 → upgrade 모드"
else
    info "신규 설치 모드"
fi

# ─── CloudWatch datasource defaultRegion — 리전은 --set 으로만 주입 ──────────
# values.yaml 에 박아두면 다른 리전 배포에서 조용히 틀린 리전을 조회한다
# (dev 는 ap-south-1 인데 values 기본값은 ap-northeast-2 — 실측 불일치).
# 명시 GRAFANA_CW_REGION → 없으면 aws configure region → 그마저 없으면
# 클러스터 ARN 의 리전 필드(현재 context)에서 추출.
CW_REGION="${GRAFANA_CW_REGION:-}"
if [ -z "$CW_REGION" ]; then
  CW_REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$CW_REGION" ]; then
  # 이 저장소의 context 이름 = 클러스터 이름 (install-eks.sh 규약). ARN 리전 필드 사용.
  cur_ctx="$(kubectl config current-context 2>/dev/null || true)"
  CW_REGION="$(aws eks describe-cluster --name "$cur_ctx" \
    --query 'cluster.arn' --output text 2>/dev/null | cut -d: -f4 || true)"
fi
if [ -n "$CW_REGION" ]; then
  info "CloudWatch defaultRegion = $CW_REGION"
else
  warn "리전 감지 실패 — values.yaml 폴백값이 쓰인다. GRAFANA_CW_REGION=<region> 으로 명시 권장"
fi

REGION_SET=()
[ -n "$CW_REGION" ] && REGION_SET=(--set "grafana.additionalDataSources[0].jsonData.defaultRegion=${CW_REGION}")

# ─── Alerting — gateway.yaml `observability.alerting` 가 결정 (기본 꺼짐) ─────────
# `./deploy render` 가 deployment/gen/<env>/eks/observability.env 에 ALERTING_ENABLED /
# ALERT_SLACK_SECRET_NAME 를 쓴다. 이 스크립트는 그 파일(또는 같은 이름의 env 변수)을 읽는다.
#   꺼짐(키 없음/false): Alertmanager 파드·전달 리소스 없음. 릴리즈는 알림 도입 전과 같고,
#                       게이트웨이 PrometheusRule(alerting-rules.yaml)만 추가로 설치된다(전달 없음).
#   켜짐(true): values-alerting.yaml(Alertmanager 1 파드, EKS 잡음 규칙 끔, env 라벨) +
#              env 별 Slack Secret 이 있으면 Slack 전달, 없으면 "null"(전달 꺼짐).
# env: ALERT_ENV → 없으면 현재 kube context 의 클러스터 이름 끝(llm-gateway-dev → dev).
ALERT_ENV="${ALERT_ENV:-}"
if [ -z "$ALERT_ENV" ]; then
  cur_cluster="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.cluster}' 2>/dev/null || true)"
  cur_cluster="${cur_cluster##*/}"          # EKS context 는 ARN — 마지막 경로 조각이 클러스터 이름
  [ -n "$cur_cluster" ] && ALERT_ENV="${cur_cluster##*-}"
fi
ALERT_ENV="${ALERT_ENV:-unknown}"
OBS_ENV_FILE="${OBSERVABILITY_ENV_FILE:-../../gen/${ALERT_ENV}/eks/observability.env}"
if [ -z "${ALERTING_ENABLED:-}" ] && [ -f "$OBS_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$OBS_ENV_FILE"; set +a
fi
ALERTING_ENABLED="${ALERTING_ENABLED:-false}"

ALERT_SET=(-f alerting-rules.yaml)
if [ "$ALERTING_ENABLED" = "true" ]; then
  SLACK_SECRET="${ALERT_SLACK_SECRET_NAME:-${SLACK_SECRET_NAME:-alertmanager-slack-${ALERT_ENV}}}"
  # 알림에 env 라벨을 붙인다(Slack 제목의 [dev]/[prod]). 로컬 쿼리·HPA 에는 영향 없음.
  ALERT_SET+=(-f values-alerting.yaml --set-string "prometheus.prometheusSpec.externalLabels.env=${ALERT_ENV}")
  slack_url_b64="$(kubectl -n "$NS" get secret "$SLACK_SECRET" -o jsonpath='{.data.api_url}' 2>/dev/null || true)"
  if [ -n "$slack_url_b64" ]; then
    ok "Alertmanager: Slack 전달 켬 (env=${ALERT_ENV}, Secret ${NS}/${SLACK_SECRET})"
    ALERT_SET+=(-f alertmanager-slack.yaml
      --set "alertmanager.alertmanagerSpec.volumes[0].name=slack-webhook"
      --set-string "alertmanager.alertmanagerSpec.volumes[0].secret.secretName=${SLACK_SECRET}"
      --set "alertmanager.alertmanagerSpec.volumes[0].secret.optional=true")
  else
    warn "Alertmanager: Secret ${NS}/${SLACK_SECRET} (key api_url) 없음 → 알림 전달 꺼짐 (env=${ALERT_ENV})."
    warn "  켜려면 Secret 을 만들고 이 스크립트를 다시 실행 (docs/NDS/observability.md §10)"
  fi
  unset slack_url_b64
else
  info "Alerting 꺼짐 (gateway.yaml observability.alerting.enabled 미설정/false) — Alertmanager 없음, 규칙만 설치"
fi

if [ -n "${GRAFANA_ROLE_ARN}" ]; then
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    --namespace "$NS" \
    --version "$KPS_VERSION" \
    -f values.yaml \
    "${ALERT_SET[@]}" \
    --set "grafana.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${GRAFANA_ROLE_ARN}" \
    "${REGION_SET[@]}" \
    --timeout 10m \
    --wait
else
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    --namespace "$NS" \
    --version "$KPS_VERSION" \
    -f values.yaml \
    "${ALERT_SET[@]}" \
    "${REGION_SET[@]}" \
    --timeout 10m \
    --wait
fi

ok "kube-prometheus-stack 설치 완료 (release=kps, ns=$NS, chart=$KPS_VERSION)"