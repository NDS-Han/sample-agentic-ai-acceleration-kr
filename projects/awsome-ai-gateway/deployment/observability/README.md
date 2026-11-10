# Observability Stack

LLM Gateway 의 상시 운영 관측성 + HPA 메트릭 소스.

## 구성

```
observability namespace (Fargate profile "platform" 포함)
├── kube-prometheus-stack (helm release "kps")
│   ├── Prometheus       (1 replica, emptyDir, retention 24h)
│   ├── Grafana          (1 replica, IRSA for CloudWatch data source)
│   ├── kube-state-metrics
│   └── prometheus-operator
│
├── prometheus-adapter (helm release "prometheus-adapter")
│   └── metrics.k8s.io/v1beta1 APIService 서빙
│       → HPA 의 targetCPUUtilizationPercentage / targetMemoryUtilizationPercentage
│         가 이 API 로부터 pod CPU/Memory 를 조회.
│         EKS Fargate 에선 metrics-server 가 kubelet authz 제약으로 동작하지 않아
│         prometheus-adapter 로 대체 (AWS 공식 권장).
│
├── OTel Collector (Deployment 1 replica)
│   ├── receivers: OTLP gRPC:4317, HTTP:4318
│   └── exporter:  prometheusremotewrite → http://kps-prometheus.observability:9090
│
└── Grafana:  kubectl port-forward svc/kps-grafana 3000:80
    Prometheus UI: kubectl port-forward svc/kps-prometheus 9090:9090
```

## 왜 prometheus-adapter 인가 (Fargate-specific)

EKS Fargate 는 kubelet 의 `/metrics/resource` 엔드포인트에 **webhook authz 레이어에서 모든 외부 ServiceAccount 를 거부**. 따라서 metrics-server 는 RBAC 이 옳아도 403 Forbidden 으로 scrape 실패 → HPA 가 `cpu: <unknown>/65%` 로 영구 멈춤.

AWS Containers 블로그 ["Autoscaling EKS on Fargate with custom metrics"](https://aws.amazon.com/blogs/containers/autoscaling-eks-on-fargate-with-custom-metrics/) 에 따르면 Fargate-only 클러스터에서 HPA 를 돌리는 유일한 표준 경로는 **Prometheus 가 cAdvisor 메트릭을 pod-level 에서 scrape** 하고, **prometheus-adapter 가 `metrics.k8s.io` APIService 로 변환** 하는 방식.

주요 주의사항:
- Fargate cAdvisor 는 **container 레벨 메트릭이 없고 pod 단위만** emit. `values.yaml` 의 resource 규칙은 `container!=""` filter 를 제거해야 함.
- metrics-server 가 동시 설치돼 있으면 APIService 소유권 충돌 → prometheus-adapter 설치 전에 `helm uninstall metrics-server -n kube-system`.

## executor 포화 메트릭 → HPA (custom.metrics.k8s.io)

CPU-HPA 의 사각지대: claude-code SSE 1개는 bedrock 스레드 슬롯 1개를 스트림
수명 내내 점유하는데, 스레드는 블로킹 대기라 **CPU 를 올리지 않는다**.
풀(WORKERS × BEDROCK_THREAD_POOL_SIZE, 기본 2×128=256/pod)이 고갈돼도
CPU-HPA 는 정상으로 읽어 정확히 포화 순간에 스케일이 안 된다.
그래서 포화의 직접 신호를 앱 메트릭으로 스케일링한다:

```
gateway (uvicorn 워커×2, OTel SDK push, 60s)
  └─ gateway_bedrock_executor_inflight   (running+queued 합 — 포화 선행 신호)
  └─ gateway_bedrock_executor_queue_depth (→ _ratio suffix)
  └─ gateway_active_connections
       │ OTLP :4317
       ▼
otel-collector  ── k8sattributes: 송신 podIP → k8s.pod.name/k8s.namespace.name 부착
       │          (필수 — pod 레이블 없으면 메트릭을 파드에 매핑 못 함)
       │          + ClusterRole otel-collector-k8sattributes (pods get/list/watch)
       │ prometheusremotewrite → kps-prometheus
       ▼
prometheus-adapter ── rules.custom: seriesQuery + 집계
       │             inflight/queue_depth: **max by (pod)** — 워커별 시리즈
       │               (service.instance.id=pod-pid) 중 가장 뜨거운 워커.
       │               워커별 풀(128)이 진짜 한계라 파드 합계는 불균등 분배의
       │               큐잉을 숨긴다. active_connections: sum by (pod).
       ▼
custom.metrics.k8s.io/v1beta1  →  HPA `type: Pods` averageValue
  (External 이 아니라 Pods — External 은 external.metrics.k8s.io 별도 API+규칙이
   필요하고, 파드 평균이라는 의미론도 Pods 가 정확하다)
```

- HPA 쪽 선언: `gatewayProxy.autoscaling.executorInflightTarget` (기본 주석처리,
  **워커당 풀** BEDROCK_THREAD_POOL_SIZE 의 ~70% 권장 → 128 이면 90).
  values-eks-fargate-{dev,prod}.yaml 에 90 설정. 0 이하/빈 값은 렌더 실패,
  sidecar otel 모드와의 조합도 렌더 실패(pod 레이블 미보장).
- 스케일 판정: `avg(inflight) > target → desired = ceil(replicas × avg/target)`.
  dev 에서 inflight=250 유지 시 1→2 스케일아웃 실측 확인.
- 전체 파이프라인 중 어디가 끊겼는지 볼 때: `kubectl get --raw
  /apis/custom.metrics.k8s.io/v1beta1` 에 메트릭이 있으면 adapter 까지는 정상,
  없으면 collector(k8sattributes)/remote-write/규칙 순으로 본다.
- ⚠️ 멀티워커 충돌: 같은 파드의 워커 2개가 동일 레이블셋으로 remote_write 하면
  Prometheus 가 out-of-order/중복 샘플을 거부해 **어느 워커 값이 남을지 불규칙**
  하다(과소집계는 랜덤하게 절반~전량 사이). `service.instance.id={hostname}-{pid}`
  (gateway ≥이번 변경 태그) 가 워커별 시리즈를 갈라 정확해진다.
  구버전 이미지에선 inflight 가 노이즈를 포함해 과소집계돼 스케일이 늦는다.
- ⚠️ stale 시리즈: 워커 재시작(pid 변경)으로 죽은 워커의 마지막 샘플이
  remote write 의 staleness 마커 부재로 최대 5분 살아있을 수 있다 —
  adapter metricsQuery 의 `last_over_time(…[2m])` 이 ~2분으로 줄여
  허위 스케일아웃을 막는다.
- ✅ lazy 생성 해소: `init_executor_metrics()` 가 lifespan 에서 계측기를
  생성하고 `add(0)` 으로 시리즈를 부팅 시점에 보고한다 — **미보고 파드가
  있으면 HPA 가 스케일 결정 전체(특히 스케일다운)를 건너뛰므로** 단순
  "데이터 부재" 가 아니라 배포 구성의 필수 조건이다. 관측 스택 미배포
  클러스터에선 타깃이 `<unknown>` 으로 남고 CPU-HPA 만 동작한다.

## 설치

`install-eks.sh` 가 아래 순서로 자동 호출 (helm install/upgrade 이전):

1. `kube-prometheus-stack/install.sh`  — Prometheus + Grafana
2. `prometheus-adapter/install.sh`     — metrics.k8s.io API 서빙 (HPA 소스)
3. `otel-collector/install.sh`         — gateway OTLP receiver + Prometheus remote-write

수동 설치:

```bash
# Terraform output 에서 Grafana IRSA 자동 읽기 (prod 기준) — 다른 env 은 ENV 지정
GRAFANA_ROLE_ARN=$(terraform -chdir=../terraform/environments/prod output -raw grafana_role_arn)

cd deployment/observability
GRAFANA_ROLE_ARN="$GRAFANA_ROLE_ARN" bash kube-prometheus-stack/install.sh
bash prometheus-adapter/install.sh
bash otel-collector/install.sh
```

## 검증

```bash
# 1. Prometheus pod Running
kubectl -n observability get pods

# 2. metrics.k8s.io API 응답
kubectl get --raw '/apis/metrics.k8s.io/v1beta1/pods' | head -c 100

# 3. kubectl top 동작
kubectl top pod -n llm-gateway

# 4. HPA 가 실제 수치 표시 (cpu: <unknown> 아님)
kubectl -n llm-gateway get hpa

# 5. Grafana 접속
kubectl port-forward -n observability svc/kps-grafana 3000:80
# http://localhost:3000  (admin / <kps-grafana-admin Secret 의 admin-password>)
```

## 운영 참고

- Prometheus emptyDir 24h retention — **pod restart 시 메트릭 소실**. 장기 보존이 필요하면:
  - Amazon Managed Prometheus (AMP) + AMP remote-write
  - 또는 PersistentVolume 전환 (Fargate 는 EFS CSI 필요)
- Grafana admin password 는 `kps-grafana-admin` Secret 에 저장됨. 최초 설치 시 `admin-secret.sh` 가 랜덤 생성.
- OTel Collector 는 현재 **replica=1**. multi-replica 시 out-of-order sample 오류 발생하므로 prometheusremotewrite 대신 다른 exporter 사용 필요.

## Dashboard Import

`grafana/dashboards/*.json` → Grafana UI → Dashboards → New → Import.

## 제거 (관측성 스택 전체)

```bash
bash uninstall.sh
```

주의: 제거 시 HPA 가 다시 `<unknown>` 으로 돌아감 → 모든 Deployment 가 minReplicas 에 고정됨.

## 파일 구조

```
deployment/observability/
├── README.md
├── namespace.yaml                      # observability namespace
├── admin-secret.sh                     # Grafana admin password Secret 생성
├── install.sh                          # legacy 호환 (kube-prometheus-stack + otel-collector 일괄)
├── uninstall.sh
│
├── kube-prometheus-stack/
│   ├── install.sh                      # idempotent helm install
│   └── values.yaml                     # helm values override
│
├── prometheus-adapter/
│   ├── install.sh                      # idempotent helm install (kps 의존)
│   └── values.yaml                     # Fargate-safe resource rules
│
├── otel-collector/                     # Deployment + ConfigMap + Service + SA
│
└── grafana/dashboards/                 # JSON dashboards
```
