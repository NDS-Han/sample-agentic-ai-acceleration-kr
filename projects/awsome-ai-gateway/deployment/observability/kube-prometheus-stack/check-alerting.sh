#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# 알림 설정 오프라인 검증 — 클러스터에 아무것도 쓰지 않는다
# ----------------------------------------------------------------------------
# 1. helm template 으로 두 모드(전달 꺼짐 / Slack)를 렌더
# 2. promtool check rules + promtool test rules (tests/alerting-rules.test.yaml)
# 3. amtool check-config 로 두 모드의 alertmanager.yaml 검사 + 라우팅 확인
# 4. EKS 컨트롤 플레인 잡음 규칙이 렌더 결과에 없는지 확인
# admission webhook 이 꺼져 있어 잘못된 PrometheusRule 도 클러스터가 받아 준다 — 배포 전 실행.
# 필요: helm, docker, python3(+PyYAML).
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

KPS_VERSION="${KPS_VERSION:-60.1.0}"
PROM_IMAGE="${PROM_IMAGE:-quay.io/prometheus/prometheus:v2.52.0}"   # 차트 60.1.0 의 Prometheus
AM_IMAGE="${AM_IMAGE:-quay.io/prometheus/alertmanager:v0.27.0}"     # 차트 60.1.0 의 Alertmanager
OUT="$(mktemp -d /tmp/check-alerting.XXXXXX)"
chmod 755 "$OUT"   # 컨테이너는 nobody 로 읽는다
trap 'rm -rf "$OUT"' EXIT

CHART="${KPS_CHART:-}"
if [ -z "$CHART" ]; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  CHART="prometheus-community/kube-prometheus-stack"
fi

render() {  # $1 = 출력 파일, 나머지 = 추가 helm 인자 (install.sh 와 같은 레이어 순서)
  local out="$1"; shift
  helm template kps "$CHART" --version "$KPS_VERSION" --namespace observability \
    -f values.yaml -f alerting-rules.yaml "$@" > "$out"
}
ON=(-f values-alerting.yaml --set-string prometheus.prometheusSpec.externalLabels.env=check)
# disabled = gateway.yaml observability.alerting 미설정/false (Alertmanager 없음, 규칙만)
# off      = enabled 이지만 Slack Secret 없음 (전달 "null")
# slack    = enabled + Secret
render "$OUT/disabled.yaml"
render "$OUT/off.yaml" "${ON[@]}"
render "$OUT/slack.yaml" "${ON[@]}" -f alertmanager-slack.yaml \
  --set "alertmanager.alertmanagerSpec.volumes[0].name=slack-webhook" \
  --set-string "alertmanager.alertmanagerSpec.volumes[0].secret.secretName=alertmanager-slack-check" \
  --set "alertmanager.alertmanagerSpec.volumes[0].secret.optional=true"

python3 - "$OUT" <<'EOF'
import base64, os, sys, yaml
out = sys.argv[1]

# disabled: no Alertmanager / delivery resources; Prometheus not wired to an Alertmanager; gateway rules present.
kinds = {}
for d in yaml.safe_load_all(open(f"{out}/disabled.yaml")):
    if d:
        for x in (d.get("items") or [d]) if d.get("kind") == "List" else [d]:
            kinds.setdefault(x.get("kind"), []).append(x.get("metadata", {}).get("name"))
assert "Alertmanager" not in kinds, "disabled: Alertmanager rendered"
for k, n in (("ServiceAccount", "kps-alertmanager"), ("Service", "kps-alertmanager"),
             ("ServiceMonitor", "kps-alertmanager"), ("Secret", "alertmanager-kps-alertmanager")):
    assert n not in kinds.get(k, []), f"disabled: {k}/{n} rendered"
assert "kube-prometheus-stack-llm-gateway-alerts" in kinds.get("PrometheusRule", []), "disabled: gateway rules missing"
print("[disabled] no Alertmanager/delivery resources; gateway PrometheusRule installed")

def docs(path):
    for d in yaml.safe_load_all(open(path)):
        if not d:
            continue
        yield from (d.get("items") or [d]) if d.get("kind") == "List" else [d]

for mode in ("off", "slack"):
    os.makedirs(f"{out}/{mode}", exist_ok=True)
    alerts = []
    for d in docs(f"{out}/{mode}.yaml"):
        kind, name = d.get("kind"), d.get("metadata", {}).get("name")
        if kind == "PrometheusRule":
            for g in d["spec"]["groups"]:
                alerts += [r["alert"] for r in g["rules"] if "alert" in r]
            if name == "kube-prometheus-stack-llm-gateway-alerts":
                yaml.safe_dump(d["spec"], open(f"{out}/rules.yaml", "w"), sort_keys=False)
                # 단위 테스트용 사본 — promtool test 는 주석 문구까지 비교한다. 템플릿 문법은
                # check rules 가 원본으로 검사하고, 테스트는 식·for·라벨만 본다.
                for g in d["spec"]["groups"]:
                    for r in g["rules"]:
                        r.pop("annotations", None)
                yaml.safe_dump(d["spec"], open(f"{out}/rules-test.yaml", "w"), sort_keys=False)
        if kind == "Secret" and name == "alertmanager-kps-alertmanager":
            for k, v in d["data"].items():
                open(f"{out}/{mode}/{k}", "wb").write(base64.b64decode(v))
        if kind == "Alertmanager":
            spec = d["spec"]
            vols = spec.get("volumes") or []
            print(f"[{mode}] Alertmanager replicas={spec.get('replicas')} storage={spec.get('storage')} "
                  f"volumes={[v['secret']['secretName'] for v in vols]}")
            assert spec.get("replicas") == 1
            assert (mode == "slack") == bool(vols), "Secret mount only in slack mode"
        if kind == "Prometheus":
            assert d["spec"]["alerting"]["alertmanagers"], "Prometheus not wired to Alertmanager"
            assert d["spec"]["externalLabels"]["env"] == "check"
    noise = {"KubeSchedulerDown", "KubeControllerManagerDown", "KubeProxyDown", "KubeVersionMismatch",
             "etcdMembersDown", "etcdInsufficientMembers"}
    left = noise & set(alerts)
    assert not left, f"[{mode}] noise rules still rendered: {left}"
    print(f"[{mode}] {len(alerts)} alert rules rendered; EKS control-plane noise rules absent")
    assert open(f"{out}/{mode}/alertmanager.yaml").read().count("hooks.slack.com") == 0, "URL in config"
EOF

echo "── promtool check rules / test rules"
cp tests/alerting-rules.test.yaml "$OUT/"
docker run --rm -v "$OUT:/w:ro" -w /w --entrypoint promtool "$PROM_IMAGE" check rules rules.yaml
docker run --rm -v "$OUT:/w:ro" -w /w --entrypoint promtool "$PROM_IMAGE" test rules alerting-rules.test.yaml

route() {  # $1 = mode, 나머지 = label=value …
  local mode="$1"; shift
  docker run --rm -v "$OUT/$mode:/etc/alertmanager/config:ro" --entrypoint amtool "$AM_IMAGE" \
    config routes test --config.file=/etc/alertmanager/config/alertmanager.yaml "$@"
}
expect_route() {  # $1 = mode, $2 = 기대 수신자, 나머지 = 라벨
  local mode="$1" want="$2"; shift 2
  local got; got="$(route "$mode" "$@")"
  [ "$got" = "$want" ] || { echo "✗ [$mode] $* → $got (want $want)"; exit 1; }
  echo "✓ [$mode] $* → $got"
}

for mode in off slack; do
  echo "── amtool check-config ($mode)"
  docker run --rm -v "$OUT/$mode:/etc/alertmanager/config:ro" --entrypoint amtool "$AM_IMAGE" \
    check-config /etc/alertmanager/config/alertmanager.yaml
done
expect_route off   null  alertname=CostStreamLagHigh severity=critical
expect_route slack null  alertname=Watchdog severity=none
expect_route slack null  alertname=InfoInhibitor severity=none
expect_route slack null  alertname=CPUThrottlingHigh severity=info
expect_route slack slack alertname=CostStreamLagHigh severity=warning
expect_route slack slack alertname=GatewayHigh5xxRatio severity=critical
echo "✓ alerting config OK"
