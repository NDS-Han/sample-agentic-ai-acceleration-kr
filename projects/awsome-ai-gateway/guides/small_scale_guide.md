# 소규모(CBT) 운영 가이드 — Pod 최소 설정 · HPA · 스케일아웃 한계

소규모 사용자(수십 명) 구간에서 Pod 리소스를 최소화하고, HPA 동작을 실제로 관측하기 위한 가이드입니다.

## 요약

| 질문 | 답 |
|---|---|
| 설정은 어디서 확인·변경하나 | `deployment/charts/llm-gateway/values-eks-fargate-<env>.yaml` **단일 파일**. `install-eks.sh` 의 `--set` 오버라이드는 resources/replicas/autoscaling 을 건드리지 않습니다. |
| HPA 메트릭은 무엇으로 | **CPU 유지**(즉시 사용 가능한 유일한 메트릭). 단 adapter 가 CPU 를 **3분 이동평균**으로 서빙하므로 CBT 규모의 버스트에는 잘 반응하지 않습니다 → 관측이 목적이면 `requests.cpu` 와 target 을 함께 낮추세요. 동시성 메트릭(`gateway_active_connections`)은 **이미 Prometheus 에 들어와 있어** 남은 작업이 작습니다(§2.3). |
| 스케일아웃 한계치를 걸어야 하나 | **`maxReplicas` 는 쿼터 방어의 올바른 레버가 아닙니다**(쿼터는 계정/리전/모델 단위). 그러나 **스로틀링 우려 자체는 타당합니다** — 리전·모델에 따라 분당 쿼터가 매우 작을 수 있습니다(§3.1). 1순위는 **쿼터 실측 → 증설 신청 또는 모델/프로파일 변경**, 2순위가 GLOBAL rate limit 행입니다. `maxReplicas` 축소는 별개의 두 증폭 경로(§3.2) 때문에 하는 것입니다. |

---

## 1. 설정 위치 및 최소 설정 (Q1)

### 1.1 단일 진실의 원천

```
deployment/charts/llm-gateway/values-eks-fargate-<env>.yaml
```

- `deployment/scripts/install-eks.sh:48` → `VALUES_FILE="$CHART_DIR/values-eks-fargate-$ENV.yaml"`, `:19-21` 에서 `ENV` 는 `dev|prod` 로 제한됩니다.
- 스크립트는 **helm upgrade 를 2번** 호출합니다(`:467`, `:551-557`). 둘 다 같은 values 파일 + 같은 `SET_ARGS` 를 씁니다.
- `SET_ARGS`(`:413-430`)는 이미지 레지스트리, 리전, STS 허용 리전, DB/Redis 접속 정보, IRSA role ARN, OIDC, `adminUi.nextauthUrl`, `migration.enabled` 등 **환경 결합 값**입니다. 검증: `grep -rn -- '--set' deployment --include="*.sh" | grep -iE 'resour|replica|autoscal'` → **0건**. 즉 **sizing 은 values 파일이 유일한 표면**입니다.

배포 전 라이브와 대조:

```bash
helm get values llm-gateway -n llm-gateway -o yaml > /tmp/live.yaml   # 편집본과 키 단위 비교
kubectl get deploy -n llm-gateway \
  -o custom-columns='NAME:.metadata.name,REPL:.spec.replicas,REQ:.spec.template.spec.containers[0].resources.requests'
```

### 1.2 워크로드 인벤토리

Deployment 6개 + Helm hook Job 1개(migration). CronJob 없음. 전 Pod 이 **단일 컨테이너 · initContainer 없음**이라 requests 합계가 명확합니다.

| 워크로드 | HPA | replicas 조정 | 비고 |
|---|---|---|---|
| gateway-proxy | ✅ | `autoscaling.*` | 트래픽 경로 |
| admin-api | ✅ | `autoscaling.*` | |
| admin-ui | ✅ | `autoscaling.*` | |
| cost-recorder-worker | ✅ | `autoscaling.*` | >1 안전(Redis Streams consumer group) |
| notification-worker | ✅ | `autoscaling.*` | ⚠️ **1 고정 권장** (§3.3) |
| scheduler | ❌ 없음 | ❌ 불가 | `templates/scheduler/deployment.yaml:17` 에 `replicas: 1` 하드코딩. PDB 템플릿도 없음 |

### 1.3 Fargate 리소스 산정 규칙

EKS Fargate 는 Pod 마다 microVM 을 띄우고 **컨테이너 requests 합계를 고정 사다리로 반올림**해 그 크기로 프로비저닝합니다.

```
프로비저닝 단계 = roundup( Σ(컨테이너 requests) + 256Mi(k8s 오버헤드) )
```

- **requests 만** 반영됩니다. **limits 는 무시**됩니다.
- 하한은 **0.25 vCPU / 0.5GB**.
- ⚠️ **vCPU 와 메모리는 커플링됩니다.** 사다리에서 **0.5 vCPU 는 최소 1GB** 입니다 — memory request 를 아무리 줄여도 cpu request 가 0.25 vCPU 구간으로 내려오지 않으면 0.5GB 에 도달할 수 없습니다.
- **사다리 단계를 못 내리는 request 감소는 아무 의미가 없습니다.**
- ⚠️ Fargate 에서 limits 는 버스트 천장이 아닙니다. **컨테이너는 limit 으로 스로틀되기 전에 microVM 경계에서 굶거나 OOM-kill 됩니다.** 그래서 microVM 보다 큰 limit(예: 1GB microVM 안의 1.5Gi limit)은 처음부터 도달 불가이고, **limit 은 메모리 사고를 막아주지 못합니다.**

실제 프로비저닝된 크기 확인(과금 단위의 유일한 근거):

```bash
kubectl get pod -n llm-gateway \
  -o custom-columns=NAME:.metadata.name,CAP:.metadata.annotations.CapacityProvisioned
```

### 1.4 최소화 여지 (기본 프로파일 기준)

| 워크로드 | 기본 requests | 프로비저닝 | 권고 requests | 결과 | 위험 |
|---|---|---|---|---|---|
| admin-api | **500m** / 768Mi | **0.5vCPU** / 1GB | **250m** / 768Mi | 0.25vCPU / 1GB | 낮음(관측 CPU 수 m) |
| admin-ui | **300m / 512Mi** | **0.5vCPU / 1GB** | **250m / 256Mi** | **0.25vCPU / 0.5GB**(하한) | 낮음(관측 약 100Mi) |
| scheduler | 250m / **512Mi** | 0.25vCPU / **1GB** | 250m / **256Mi** | **0.25vCPU / 0.5GB**(하한) | 낮음(관측 약 100Mi) |
| gateway-proxy | 500m / **1Gi** | 0.5vCPU / **2GB** | 500m / **768Mi** | 0.5vCPU / **1GB** | ⚠️ **높음 — 아래 참조** |
| cost-recorder-worker | 250m / 512Mi | 0.25vCPU / 1GB | 변경 없음 | 이미 최소 | — |
| notification-worker | 250m / 512Mi | 0.25vCPU / 1GB | 변경 없음 | 이미 최소 | — |

- cost-recorder-worker / notification-worker 는 관측 사용량이 **약 320~360Mi** 로 256Mi 를 이미 넘습니다. 하한으로 내리면 OOM — **그대로 두세요.**
- admin-api 는 관측 약 225Mi 로 256Mi 와의 여유가 10%대 → memory 하한은 권하지 않습니다. cpu 만 내리세요.
- gateway-proxy 는 cpu 500m(=0.5 vCPU) 을 유지하는 한 **768Mi 가 메모리 request 의 실질 하한**입니다(§1.3 커플링).

> ⚠️ **gateway-proxy 메모리 축소는 가장 위험한 변경입니다.** 2GB microVM 에서 컨테이너가 쓸 수 있던 여유가 1GB 단계로 내려가면 **약 768Mi 수준으로 줄어듭니다.** 관측된 RSS 는 **약 530Mi 이지만 이는 사실상 무부하 상태**(CPU 10m 내외)의 값이고, **동시 SSE 스트림 수십 개 조건에서의 메모리 사용량은 측정된 바 없습니다.** 게다가 `limits.memory` 는 §1.3 대로 보호막이 되지 못합니다.
> ⇒ 이 항목만 **마지막에 따로 적용**하고, 적용 후 부하 구간에서 `kubectl top pod` 과 `OOMKilled` 여부를 모니터링하세요. 롤백 트리거: RSS 가 650Mi 를 넘거나 재시작이 관측되면 즉시 `1Gi` 로 복귀.
> 위험 대비 효과가 가장 좋은 것은 **admin-api cpu 축소와 admin-ui/scheduler 하한 이동**입니다. 이 3건만으로도 대부분의 축소 효과를 얻습니다.

> 위 사용량은 동일 차트를 소규모로 운영할 때의 참고값입니다. 적용 전 `kubectl top pod -n llm-gateway` 로 각자 환경에서 재측정하세요.

### 1.5 적용 방법 — 기준선을 직접 편집하지 말고 오버레이를 스택

되돌리기가 파일 하나 빼는 것으로 끝납니다.

```bash
helm upgrade llm-gateway deployment/charts/llm-gateway -n llm-gateway \
  -f deployment/charts/llm-gateway/values-eks-fargate-<env>.yaml \
  -f deployment/charts/llm-gateway/values-eks-fargate-<env>-cbt.yaml
```

`values-eks-fargate-<env>-cbt.yaml`:

```yaml
# CBT 최소 프로파일. 기준선 위에 스택해서 사용.
# ⚠️ replicas 를 내리는 컴포넌트는 PDB 도 반드시 함께 내려야 합니다 (§4.3-2)

gatewayProxy:
  resources:
    requests:
      cpu: "500m"        # 0.5 vCPU 유지 → memory 하한은 768Mi (§1.3)
      # memory: "768Mi"  # ⚠️ 위험 항목 — §1.4 경고 확인 후 마지막에 별도 적용
  autoscaling:
    enabled: true        # HPA 관측이 목적이므로 유지
    minReplicas: 2       # 부하가 아니라 가용성 하한 (maxUnavailable:0 + 다중 AZ)
    maxReplicas: 6       # 기본 30 → 6 (§3.2)
    targetCPUUtilizationPercentage: 65
  podDisruptionBudget:
    minAvailable: 1
  env:
    WORKERS: "2"
    UVICORN_WORKERS: "2" # ⚠️ 결함 우회 — §4.1

adminApi:
  resources:
    requests: { cpu: "250m", memory: "768Mi" }
  autoscaling: { enabled: true, minReplicas: 2, maxReplicas: 4, targetCPUUtilizationPercentage: 65 }
  podDisruptionBudget: { minAvailable: 1 }

adminUi:
  resources:
    requests: { cpu: "250m", memory: "256Mi" }   # 0.25vCPU / 0.5GB 하한
  autoscaling: { enabled: true, minReplicas: 1, maxReplicas: 2 }
  podDisruptionBudget: { enabled: false }        # ⚠️ 1 replica 이므로 필수

costRecorderWorker:
  resources:
    requests: { cpu: "250m", memory: "512Mi" }   # 사용량 320Mi+ — 더 내리지 말 것
  autoscaling: { enabled: true, minReplicas: 1, maxReplicas: 3 }
  podDisruptionBudget: { enabled: false }        # ⚠️ 1 replica 이므로 필수

notificationWorker:
  autoscaling: { enabled: false }                # ⚠️ 중복 발송 방지 — §3.3
  replicaCount: 1
  resources:
    requests: { cpu: "250m", memory: "512Mi" }
  podDisruptionBudget: { enabled: false }        # ⚠️ 1 replica 이므로 필수

scheduler:
  resources:
    requests: { cpu: "250m", memory: "256Mi" }   # 0.25vCPU / 0.5GB 하한
```

적용 전 렌더 확인 → 적용 후 상태 확인:

```bash
helm template llm-gateway deployment/charts/llm-gateway \
  -f .../values-eks-fargate-<env>.yaml -f .../values-eks-fargate-<env>-cbt.yaml \
  | grep -A8 'kind: HorizontalPodAutoscaler'

kubectl get pdb -n llm-gateway    # ALLOWED DISRUPTIONS >= 1 (0 이면 노드 재활용 차단)
kubectl get hpa -n llm-gateway    # TARGETS 가 숫자여야 함 (<unknown> 이면 장애)
kubectl top pod -n llm-gateway
```

---

## 2. HPA 메트릭 (Q2)

### 2.1 이미 들어 있습니다 — 새로 만들 것 없음

HPA 템플릿 5개(`autoscaling/v2`): gateway-proxy, admin-api, admin-ui, cost-recorder-worker, notification-worker. scheduler 는 의도적으로 없습니다.

렌더 구조:

| 워크로드 | CPU 메트릭 | memory 메트릭 |
|---|---|---|
| gateway-proxy, admin-api | **조건부**(`targetCPUUtilizationPercentage` 가 truthy 일 때만) | 블록 존재하나 **한 번도 렌더 안 됨** |
| admin-ui | **무조건** | 블록 존재하나 렌더 안 됨 |
| cost-recorder-worker, notification-worker | **무조건** | 블록 자체 없음 |

`targetMemoryUtilizationPercentage` 를 설정한 values 파일이 없어 **어디서도 메모리 메트릭은 렌더되지 않습니다.** **메모리 메트릭은 쓰지 마세요** — 유휴 RSS 가 이미 높아 상시 발동합니다.

### 2.2 메트릭 경로 — metrics-server 가 아닙니다

- EKS Fargate 는 kubelet authz 제약으로 metrics-server 가 동작하지 않습니다. 대신 **prometheus-adapter** 가 `v1beta1.metrics.k8s.io` 를 서빙합니다(설치 절차: `deployment/docs/eks-fargate/04-helm-install.md:106`).
- `kubectl top pod` 과 HPA 가 실제 CPU % 를 반환하면 정상 — **CPU HPA 는 추가 작업 없이 동작합니다.**
- ⛔ **metrics-server 를 설치하지 마세요.** `v1beta1.metrics.k8s.io` APIService 등록이 prometheus-adapter 와 충돌합니다.
- ⚠️ **adapter 는 CPU 를 3분 이동평균(`rate(...[3m])`)으로 보고합니다.** 부하 계단이 발생해도 절반 반영까지 약 1.5분, 완전 반영까지 약 3분이 걸립니다. CBT 세션의 짧은 버스트에는 **구조적으로 둔감**합니다.

### 2.3 동시성 메트릭은 생각보다 가깝습니다

앱 메트릭은 **이미 수집되고 있습니다** — ConfigMap 이 OTLP 엔드포인트를 주입하고(`templates/common/configmap.yaml:76-78`), 앱이 약 20개 OTel 계측을 emit 하며(`gateway-proxy/src/app/observability/metrics.py`), otel-collector 가 `prometheusremotewrite` 로 Prometheus 에 씁니다. **`gateway_active_connections` 는 지금도 Prometheus 에서 조회 가능합니다**(`metrics.py:112-114` 생성, `middleware/otel.py:43/:63` 증감).

즉 앱 `/metrics` 엔드포인트도, ServiceMonitor 도 새로 만들 필요가 없습니다. 남은 작업은 **3가지**입니다:

1. ⚠️ **Pod 식별 라벨 추가** — 현재 OTLP 시계열에 Pod 을 구분하는 라벨이 없어 **모든 gateway-proxy Pod 이 하나의 시계열로 합쳐집니다.** Pods-type HPA 룰은 이 상태로는 쓸 수 없습니다. `k8s.pod.name` 을 리소스 속성으로 추가해야 합니다.
2. prometheus-adapter ConfigMap 에 `rules:` 항목 추가(→ adapter 차트가 `custom.metrics.k8s.io` APIService 를 등록).
3. 차트 HPA 템플릿에 Pods 메트릭 지원 추가(현재 `hpa.yaml:19-34` 는 cpu/memory 만 지원).

**권고 순위**

1. **CBT 는 CPU 로 시작** — 즉시 동작하고 추가 인프라가 없습니다. 단 3분 창의 둔감함을 인지하고 쓰세요.
2. **동시성 메트릭을 후속 과제로** — 위 3단계. CBT 에서 실제 동시 스트림 수를 관측한 뒤 목표값을 정하는 것이 순서상 맞습니다.
3. **메모리 — 사용 금지.**
4. cost-recorder-worker 에 한해 KEDA + Redis Stream `XLEN` 이 이론적으로 적합(`values.yaml:913` 에 언급). CBT 범위 밖.

### 2.4 CBT 규모에서 CPU HPA 는 잘 발동하지 않습니다

- 관측: gateway-proxy Pod 당 CPU **10m 내외**, request 500m → 사용률 1~2%. target 65% 에는 Pod 당 약 325m 필요.
- ⚠️ **단, 이 관측은 무부하 상태입니다** — 측정 시점 24시간 동안 `/v1/messages` 요청이 0건이었고 헬스체크·스캐너 노이즈만 있었습니다. **50명 부하 하의 CPU 거동에 대한 증거가 아닙니다.**
- ⚠️ **"I/O 바운드라 CPU 는 절대 안 오른다"는 단정은 근거가 없습니다.** 레포의 부하테스트 오버레이는 **GIL 경합을 피하려고 `WORKERS` 를 1로 내립니다** — 즉 실부하에서 CPU/GIL 이 관측된 병목이었습니다.
- 방어 가능한 진술은 더 좁습니다: **동시 사용자 50명 규모에서 500m request Pod 이 3분 이동평균으로 측정된 65% 에 도달할 가능성은 낮다.** 그래서 CBT 중 HPA 동작을 보려면 `requests.cpu` 를 250m 로, target 을 50 내외로 낮추는 것이 맞습니다.

관측 실험용 프로파일:

```yaml
gatewayProxy:
  resources:
    requests: { cpu: "250m" }   # 관측률 2배 — 단 버스트 상한도 0.25 vCPU 로 하락
  env: { WORKERS: "1", UVICORN_WORKERS: "1" }   # cpu 를 내리면 필수 (§4.2)
  autoscaling: { targetCPUUtilizationPercentage: 50, minReplicas: 1, maxReplicas: 3 }
```

⚠️ 이때 memory request 를 512Mi 로 내리지 마세요 — gateway-proxy 의 관측 RSS(약 530Mi)가 이미 그 값을 넘습니다.

### 2.5 반응 속도 — 빠른 스케일링보다 헤드룸

부하 도착 → 신규 처리능력까지의 실제 지연 사슬:

| 구간 | 시간 |
|---|---|
| adapter 의 CPU 3분 이동평균 반영 | **약 1.5~3분 (지배적)** |
| Prometheus 스크레이프 | 약 30초 |
| HPA 컨트롤러 sync 주기 | 15초 |
| `scaleUp` stabilization window | 30초 |
| Fargate Pod `created → Ready` | **약 60~120초** |

⇒ 현실적으로 **3분 이상**입니다. `stabilizationWindowSeconds` 를 낮춰도 소용없습니다 — **3분 메트릭 창과 microVM 부팅이 지배적**입니다.

⇒ **버스트형 세션은 시작 전에 `minReplicas` 를 미리 올려두세요.** HPA 가 따라잡기를 기대하지 마세요.

> 참고: `values-eks-fargate-prod-loadtest.yaml` 이 "peak 부하 도착 시점에 이미 N pod ready" 를 위해 Pod 을 선프로비저닝하는 선례를 보여줍니다. 단 이 파일에는 **`adminApi:` 블록만 있고 `gatewayProxy:` 블록은 없으므로** gateway-proxy 의 HPA 거동에 대한 증거로 인용할 수는 없습니다. 일반 교훈(버스트 전 선프로비저닝)만 유효합니다.

---

## 3. 스케일아웃 한계치 (Q3)

### 3.1 ⚠️ 결론: `maxReplicas` 는 잘못된 레버지만, 스로틀링 우려 자체는 타당합니다

**(a) Pod 를 늘려도 쿼터는 안 올라갑니다.** Bedrock TPM/RPM 쿼터는 **계정 · 리전 · 모델 · 엔드포인트 단위**이며 호출자 Pod 수 단위가 아닙니다. Pod 를 늘리면 **429 가 나타나는 위치만 이동**합니다. 이 점에서 `maxReplicas` 는 쿼터 방어 수단이 아닙니다.

**(b) 그러나 분당 쿼터는 리전·모델에 따라 매우 작을 수 있습니다.** 조사 과정에서 확인된 사실:

- **서울 리전(ap-northeast-2)** 의 Claude Sonnet 4 V1 교차리전 추론 쿼터는 **분당 200 요청 / 분당 200,000 토큰**(둘 다 `Adjustable=true`, applied == AWS default)입니다. 분당 200 요청 = **초당 약 3.3 요청**이 계정 전체 합산치입니다.
- 같은 리전에서 **Claude Opus 4 V1 은 분당 쿼터 행이 아예 없습니다.**
- 반면 다른 리전/모델 조합(예: `global.` 프로파일 계열)은 분당 수백만 토큰 규모로, **같은 모델도 리전에 따라 30~50배 차이**가 납니다.

⇒ **사용자 수십 명 규모에서도 분당 쿼터에 닿을 수 있습니다.** 고객의 우려는 기각 대상이 아닙니다.

**(c) 다만 원인은 "신규 계정"이 아닙니다.** AWS 문서는 *"계정에 할당된 기본 쿼터는 리전 요소, 결제 이력, 부정 사용 여부, 증설 승인 등에 따라 달라질 수 있다"* 고 명시합니다 — 신규 계정 여부는 여러 변수 중 하나이고, **기성 계정에서 applied == default 인 것으로 "신규 계정도 같다"를 증명할 수는 없습니다.** tokens-per-DAY 행에는 *"신규 AWS 계정은 축소된 쿼터를 받을 수 있다"* 는 문구가 명시적으로 있고, 이 행은 `Adjustable=False` 입니다.

**⇒ 반드시 자기 계정·리전·모델로 실측하세요.** 쿼터는 계정·리전 간 이관되지 않습니다.

```bash
unset AWS_BEARER_TOKEN_BEDROCK   # ⚠️ 이 변수가 있으면 SigV4 결과가 무효화됩니다

# 실제 허용 모델 확인 (여기 없는 모델의 쿼터를 봐도 의미 없습니다)
kubectl get cm llm-gateway-config -n llm-gateway -o jsonpath='{.data.BEDROCK_ALLOWED_MODELS}'
kubectl get cm llm-gateway-config -n llm-gateway -o jsonpath='{.data.AWS_REGION}'

# 그 리전·그 모델의 분당 쿼터 (교차리전 추론이면 "Cross-region" 행을 보세요)
aws service-quotas list-service-quotas --service-code bedrock --region <region> \
  --query "Quotas[?contains(QuotaName,'<Model Name>')].[QuotaCode,QuotaName,Value,Adjustable]" --output table

# 일별 토큰 쿼터 (조정 불가 — 별도 확인)
aws service-quotas list-service-quotas --service-code bedrock --region <region> \
  --query "Quotas[?contains(QuotaName,'per day')].[QuotaName,Value,Adjustable]" --output table
```

### 3.2 조치 순서 (1순위가 Helm 이 아닙니다)

| 순위 | 조치 | 근거 |
|---|---|---|
| **1** | **예상 수요를 산정하고 실측 쿼터와 비교** | 분당 요청/토큰 기준. 여기서 부족이 확인되면 아래로 진행 |
| **2** | **Service Quotas 증설 신청**, 또는 **분당 쿼터가 큰 리전/추론 프로파일로 모델 변경** | 분당 TPM/RPM 은 `Adjustable=true`. ⚠️ AWS 는 *"기존 쿼터를 소진하는 트래픽을 이미 발생시키는 고객에게 우선권을 주며, 이 조건을 충족하지 않으면 반려될 수 있다"* 고 명시 — 따라서 **먼저 실사용으로 소비 실적을 만들거나, 프로파일 변경을 병행**하는 것이 현실적입니다 |
| **3** | **`model.rate_limit_configs` 의 GLOBAL 스코프 행** — 우아한 degradation 용 | ⚠️ **쿼터의 70~80% 같은 일괄값을 넣지 마세요.** 그러면 Bedrock 스로틀링을 게이트웨이 스로틀링으로 바꾸는 것에 그칩니다. **예상 수요보다는 위, 쿼터보다는 아래**로 잡으세요 |
| **4** | **`maxReplicas` 축소** | 쿼터 방어가 아니라 아래 두 증폭 경로 때문 |

**GLOBAL 행 관련 필수 확인**: `ScopeLimits.rpm/tpm` 의 기본값은 `None` 이고, **`None` 은 "무제한"이며 검사 자체가 스킵됩니다.** 즉 해당 모델에 GLOBAL 행(`scope='GLOBAL'`, `scope_id IS NULL`, `model_alias` 지정)이 없으면 **함대 전체 상한이 아예 없고**, 이 경우 스케일아웃은 실제로 Bedrock 부하로 직결됩니다. 테이블은 `model.rate_limit_configs`(`gateway-proxy/src/app/models/model.py:177`), 컬럼은 `rpm_limit` / `tpm_limit` / `cpm_limit_usd` / `cph_limit_usd`(`:190-193`)이고 **모든 Pod 이 공유**합니다.

정상 경로(Redis 정상)의 rate limit 은 **Redis Lua 원자 연산 기반 공유 카운터**라 Pod 수와 무관합니다(`services/rate_limit_service.py:134,140`). Pod 이 3→30 이 되어도 카운터는 하나입니다.

### 3.3 Pod 수에 실제로 비례하는 것 2가지 (= `maxReplicas` 를 낮추는 이유)

**① Redis 장애 시 fallback 리미터가 프로세스 로컬**

- 나눗수는 `RL_FALLBACK_REPLICAS × uvicorn_workers` 이고 리미터는 프로세스마다 독립입니다(`rate_limit_service.py:494`, `:502` `adjusted_limit = max(1, limit // self._worker_count)`).
- ⚠️ 이때 곱해지는 "limit" 은 **DB/Bedrock 쿼터가 아니라 하드코딩 상수**입니다(`_FALLBACK_USER_RPM = 60`, `_FALLBACK_TEAM_RPM = 600`, `_FALLBACK_GLOBAL_RPM = 6000`). Redis 가 죽으면 DB 한도도 못 읽기 때문입니다.
- ⚠️ **Redis 장애 시 TPM 은 아예 집행되지 않습니다** — fallback 경로는 **RPM 만** 검사합니다. 이것이 더 큰 노출입니다.
- 산술: 기본값(`minReplicas: 3`, `WORKERS: 2`)이면 나눗수 12, 실제 프로세스 6 → 의도의 **절반**(과도한 429). 반대로 30 replica 면 프로세스 60 → 6,000 RPM 의도 대비 **약 30,000 RPM**.
- **`maxReplicas` 를 5 이하로 두면 합산치가 의도값을 넘지 않습니다**(5 replica → 10/12 ≈ 0.83배).
- ⚠️ `max(1, limit // worker_count)` 는 1에서 바닥을 치므로, 나눗수보다 작은 한도는 프로세스당 1로 퇴화합니다.

**② Bedrock 스레드풀이 프로세스 로컬** (Pod 단위가 아닙니다)

- `providers/bedrock_adapter.py:17` — 모듈 수준 `ThreadPoolExecutor`, `BEDROCK_THREAD_POOL_SIZE` 기본 **128**. uvicorn 워커마다 하나씩 생깁니다 → Pod 당 `128 × WORKERS`.
- 전역/공유 세마포어는 **존재하지 않습니다**(코드 전체에 `Semaphore` 0건).
- `maxReplicas: 30`, `WORKERS: 2` 면 이론상 `128 × 2 × 30 = 7,680` 동시 제출까지 열립니다.
- ⚠️ 이 값을 낮추려면 코드 경로가 필요합니다 — **어떤 values 파일에도 노출돼 있지 않습니다.**

⇒ **CBT 에서 `maxReplicas: 6`(가능하면 3~5)** 은 이 두 경로를 묶는 합리적 선택입니다. 반복하지만 **Bedrock 쿼터 방어 때문이 아닙니다.**

### 3.4 replicas=1 을 반드시 유지해야 하는 컴포넌트

| 워크로드 | 이유 |
|---|---|
| **scheduler** | APScheduler + 인메모리 jobstore, 리더 선출 없음. `templates/scheduler/deployment.yaml:16-20` 에서 `replicas: 1` + `strategy: Recreate` 하드코딩 → 구조적으로 안전. 단 `Recreate` 라 helm upgrade 마다 **cron 공백**이 생깁니다 |
| **notification-worker** | 일반 Redis **Pub/Sub**(전 구독자 브로드캐스트), dedup 키·consumer group 없음 → **replica 마다 같은 알림을 발송**합니다(`listeners/channel_listener.py:36,40-41`). 기본 프로파일은 2 replica + HPA 최대 6 → **최대 6중 발송 가능** → §1.5 에서 `enabled: false` + `replicaCount: 1` 로 고정 |

**>1 로 안전**: gateway-proxy, admin-api, cost-recorder-worker(Streams consumer group + Pod 이름 바인딩).

⚠️ `scheduler.replicaCount`(`values.yaml:738`)는 **어떤 템플릿도 참조하지 않는 죽은 값**입니다. scheduler 는 `resources` 만 유효합니다.

### 3.5 Pod 당 실제 포화 지점

| 자원 | Pod 당 | 성격 |
|---|---|---|
| **CPU / GIL** | 0.5 vCPU ÷ WORKERS = **프로세스당 약 0.25 vCPU** | SSE 청크마다 JSON 재직렬화를 하므로 **가장 먼저 물릴 가능성이 높습니다.** 부하테스트 오버레이가 GIL 경합 때문에 `WORKERS: 1` 을 쓴 것이 그 증거 |
| Bedrock 스레드풀 | `128 × WORKERS` | **하드 상한이 아니라 포화점**입니다. 128개가 모두 바쁠 때 추가 제출은 executor 큐(무제한)에 **쌓이며**, 증상은 에러가 아니라 **TTFT/토큰 간 지연 증가**입니다. 스레드는 스트림 수명 동안이 아니라 **이벤트 단위로** 점유됩니다 |
| DB 커넥션 | 제약 아님 | 세션이 단기 `async with` 블록 + RDS Proxy 가 클라이언트↔백엔드 커넥션을 **분리**합니다(pinning 필터 없음, statement cache 0). 유휴에서 약 3:1 다중화 관측 |
| Redis | 제약 아님 | 노드 `maxclients` 대비 여유 |

⚠️ **`max_pool_connections=50` 을 동시성 상한으로 오해하지 마세요.** 이것은 **커넥션 재사용 캡**입니다. botocore 는 `block` 을 지정하지 않고 urllib3 기본이 `block=False` 이므로, 50개가 모두 사용 중이면 **추가 커넥션을 열고 반환 시 폐기**합니다 — 차단이 아니라 **TLS 핸드셰이크 처닝과 지연**이 증상입니다. 또한 `BotoConfig` 는 in-account(`app/main.py:108-109`)와 cross-account(`:201-202`) **두 곳**에 있고, cross-account 프로바이더는 `(role_arn, region)` 마다 **별도 클라이언트를 캐시**하므로 대상마다 자기 몫의 50-커넥션 풀이 추가됩니다.

⚠️ 반대로 **"클라이언트 커넥션 수 × Pod 수" 를 Aurora `max_connections` 와 직접 비교하면 안 됩니다.** RDS Proxy 의 `MaxConnectionsPercent` 는 **백엔드(프록시→DB)** 커넥션을 제한하고, Pod→프록시 커넥션은 별개입니다. 이 둘을 분리하는 것이 RDS Proxy 의 목적이며 이 배포는 실제로 그렇게 동작합니다. **따라서 "커넥션 기준 안전 Pod 상한"은 이 데이터로 산정할 수 없습니다.**

**소규모 산술**: `minReplicas: 2` 의 근거는 **부하가 아니라 가용성**입니다(`rollingUpdate.maxUnavailable: 0` + 다중 AZ). 그리고 이 규모에서 실제 제약은 Pod 내부 동시성이 아니라 **§3.1 의 계정 분당 쿼터**입니다.

### 3.6 429(스로틀링) 처리 현황

- ✅ **증폭 없음**: `ThrottlingException → 429` 매핑(`bedrock_adapter.py:27`), boto 재시도 `total_max_attempts=1`, fallback 후보 간 재시도 없음. 게이트웨이가 429 폭풍을 키우지 않습니다. **`BEDROCK_MAX_ATTEMPTS` 는 1로 유지하세요.**
- ⚠️ **백프레셔 없음**: 분산 서킷브레이커 실패 집합이 `{502, 503}` 뿐이라(`services/fallback_loop.py:45`) **429 는 CB 를 트립시키지 않습니다.** 지속 스로틀링 시 모든 Pod 이 계속 재시도합니다.
- ⚠️ **관측 구분 불가**: 업스트림 Bedrock 429 와 게이트웨이 자체 rate limit 429 를 구분할 수 없습니다 — `gateway_provider_error_total` 이 선언만 되어 있고 증가 호출부가 없습니다(`observability/metrics.py:64-65`). **§3.1 의 쿼터 부족을 조기에 발견하려면 이 배선이 필요합니다.**
- ⚠️ **fail-open 이 기본**: Redis eval 실패 시 요청이 통과합니다. `gateway_rl_fail_open_total`(`metrics.py:55-56`) 에 알람을 걸고, 비용 민감 구간이면 `RL_FAIL_MODE=closed` 를 검토하세요.

---

## 4. 축소 시 함정 체크리스트

### 4.1 ⚠️ `UVICORN_WORKERS` 미설정 (CBT 전 조치 권장)

fallback rate limiter 의 나눗수는 `RL_FALLBACK_REPLICAS × uvicorn_workers` 이고, `uvicorn_workers` 는 **`UVICORN_WORKERS`** 환경변수를 읽습니다(`app/config.py:16`, 기본값 **4**). 그런데 차트는 이 변수를 **어디서도 설정하지 않습니다** — 실제 프로세스 수를 정하는 변수는 `WORKERS` 입니다.

- 결과: `WORKERS: "2"` 인데 나눗수는 4로 계산 → **Redis 장애 시 한도가 의도의 절반**(과도한 429).
- 조치: `gatewayProxy.env.UVICORN_WORKERS` 를 `WORKERS` 와 동일하게 설정(§1.5 포함).
- 관련: `RL_FALLBACK_REPLICAS` 는 `minReplicas` 를 자동 추종합니다(`templates/common/configmap.yaml:45`). **`minReplicas: 0` 은 쓰지 마세요** — `| default` 체인이 조용히 통과합니다.

### 4.2 `WORKERS` 는 cpu request 와 함께 움직여야 합니다

values 파일에 이미 경고가 있습니다 — *"500m(Fargate ≈0.5 vCPU)에서 4워커면 워커당 0.125 vCPU 기아 → `/v1/messages` p95 저하"*. cpu request 를 250m 로 내리면 `WORKERS: "1"` 로 함께 내리세요.

⚠️ `adminApi.env.WORKERS` 는 **죽은 설정**입니다 — `admin-api/Dockerfile:52` 가 exec 형식으로 `--workers 1` 을 하드코딩해 `${WORKERS}` 가 확장되지 않습니다. admin-api 는 항상 1 프로세스입니다.

### 4.3 기타

| # | 함정 | 근거 | 대응 |
|---|---|---|---|
| 1 | **`replicaCount` 는 `autoscaling.enabled: true` 인 동안 완전히 무시** — Deployment 가 `spec.replicas` 를 생략 | `templates/gateway-proxy/deployment.yaml:16-17` (전 워크로드 동일) | 축소는 `autoscaling.minReplicas` 로. `replicaCount` 만 바꾸면 아무 일도 안 일어납니다 |
| 2 | **PDB 미동반 축소 → ALLOWED DISRUPTIONS 0.** replicas 를 1로 내리는 **모든** 컴포넌트가 차트 기본 `podDisruptionBudget.minAvailable: 1` 을 상속해 0이 됩니다 | 컴포넌트별 base 기본값 (`values.yaml:688`, `:831` 등) | `minReplicas`/`replicaCount` 를 1로 내리는 **같은 커밋에서** `podDisruptionBudget.enabled: false`. ⚠️ **오해 주의: PDB 는 Eviction API(노드 드레인·Fargate 노드 재활용)를 막습니다. Deployment 롤링 업데이트는 Pod 을 직접 삭제하므로 PDB 를 우회합니다** — 즉 롤아웃이 막히는 게 아니라 **노드 재활용이 막힙니다** |
| 3 | **`targetCPUUtilizationPercentage: 0` 은 메트릭 블록을 통째로 삭제** → 절대 스케일 못 하는 HPA (gateway-proxy·admin-api 만 조건부) | `templates/gateway-proxy/hpa.yaml:19` 의 bare `if` | HPA 를 끄려면 `autoscaling.enabled: false`. `0`/`null` 금지 |
| 4 | admin-ui / 두 워커 HPA 는 CPU 를 **무조건** 렌더 → target 이 `null` 이면 빈 `averageUtilization` | `templates/cost-recorder-worker/hpa.yaml:18-25` | 고정하려면 `autoscaling.enabled: false` + `replicaCount: N` |
| 5 | **readiness 가 함대 상관** — degradation/pool 포화 시 503 → Redis·DB 순간 장애로 **모든 Pod 이 동시에 ALB 타깃에서 이탈** | `routers/health.py:88,101-103` | gateway-proxy 를 2 replica 미만으로 내리지 말고, `database.external.poolSize` 를 낮추지 마세요(hard_cap 이 낮아져 더 빨리 트립) |
| 6 | **차트에 `values.schema.json` 이 없어 오타 키가 조용히 무시됩니다.** 예: `guides/deployer-guide.md:306-311` 이 HPA 예시를 **`hpa:`** 로 표기하지만 차트 키는 **`autoscaling:`** | `grep -rn '^\s*hpa:' deployment/charts/llm-gateway/` → 0건 | 오타는 에러 없이 통과하고 HPA 는 기본값으로 남습니다. **항상 `helm template` 렌더로 확인**하세요 |
| 7 | 존재하지 않는 메트릭 이름을 참조하는 커스텀 HPA 를 만들지 말 것 — 차트 HPA 템플릿은 Pods 메트릭을 렌더하지 못합니다 | `hpa.yaml:19-34` 는 cpu/memory 만 | §2.3 의 3단계를 정식으로 밟으세요 |

---

## 5. CBT 전 점검 순서

1. **실제 허용 모델 + 리전 확인 → 그 조합의 분당 쿼터 실측**(§3.1). `AWS_BEARER_TOKEN_BEDROCK` unset 필수. **여기가 이번 건의 1순위입니다.** 일별 토큰 쿼터(조정 불가)도 별도 확인.
2. **예상 수요 vs 실측 쿼터 비교** → 부족하면 증설 신청 또는 분당 쿼터가 큰 리전/추론 프로파일로 전환 검토(§3.2).
3. **`model.rate_limit_configs` 의 GLOBAL 스코프 행 존재 확인** — 없으면 함대 상한이 아예 없습니다(`None` = 검사 스킵).
4. **notification-worker 를 1 replica 로 고정**(§3.4).
5. **`UVICORN_WORKERS` 설정**(§4.1).
6. **오버레이 렌더 검증 → 적용 → PDB / HPA / CapacityProvisioned 확인**(§1.5). gateway-proxy 메모리 축소는 **마지막에 따로**(§1.4 경고).
7. **`gateway_provider_error_total` 배선 + 429 를 서킷브레이커 실패 집계에 포함**(§3.6) — 쿼터 부족을 조기에 발견하기 위한 관측 수단입니다.

## 6. 부하 없이는 확정할 수 없는 것

| 항목 | 확인 방법 |
|---|---|
| gateway-proxy 의 동시 SSE 부하 하 메모리 사용량 | 본 문서 수치는 전부 무부하 기준(측정 24시간 동안 실제 추론 요청 0건). **1GB 단계로 내리기 전 반드시 부하 측정** |
| CBT 규모에서 CPU 가 실제로 어디까지 오르는지 | 부하테스트 오버레이가 GIL 경합을 실측한 만큼, "CPU 는 안 오른다"는 예측은 신뢰할 수 없습니다 |
| 커넥션 기준 안전 Pod 상한 | 클라이언트/백엔드 커넥션이 RDS Proxy 로 분리되어 있어 **현재 데이터로는 산정 불가**. 부하 시 프록시의 `ClientConnections` / `DatabaseConnections` 로 다중화 비율을 측정해야 합니다 |
| GLOBAL rate limit 의 적정 값 | 실측 쿼터와 CBT 초반 실사용 수요 사이 값으로. **쿼터의 고정 비율로 잡지 마세요**(§3.2-3) |
| 신규 계정의 실제 쿼터 | 기성 계정 관측으로 추정 불가 — 해당 계정에서 직접 조회해야 합니다 |

## 참고 문서

| 내용 | 위치 |
|---|---|
| Fargate 최소 단위/반올림 | `deployment/docs/eks-fargate/troubleshooting.md:1067` |
| HPA `<unknown>` 장애 | `deployment/docs/eks-fargate/troubleshooting.md:1299` |
| HPA 헬스 체크 | `deployment/docs/eks-fargate/05-smoke-test.md:35-51` |
| Observability 스택 설치 | `deployment/docs/eks-fargate/04-helm-install.md:106` |
| 리소스·스케일 레버 | `deployment/docs/eks-fargate/01-prerequisites.md:58-93` |
| replica 범위 | `guides/deployer-guide.md:540` |
| Bedrock 쿼터 | AWS 문서 *Bedrock quotas* (`docs.aws.amazon.com/bedrock/latest/userguide/quotas.html`) |
