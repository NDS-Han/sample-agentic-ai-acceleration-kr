# 부하테스트 — mock-bedrock SSE 동시성 · executor-HPA 검증

claude-code형 인터랙티브 트래픽(SSE 스트림 장기 점유)에서 게이트웨이의
**실제 용량 한계** — executor 스레드 포화 지점·큐잉·HPA 스케일 루프 — 을
**실 AWS 호출 없이(비용 $0)** 검증하는 절차. 도구 자체의 설명은
[loadtest/README.md](../../loadtest/README.md) — 이 문서는 NDS 배포에 맞춘
운영 절차와 함정을 다룬다.

## 1. 원리 — 무엇을 재현하고 무엇은 못 보는가

- 게이트웨이 병목은 모델 응답 품질이 아니라 **연결 유지 시간**의 함수다.
  SSE 1건 = executor 스레드 슬롯 1개(`BEDROCK_THREAD_POOL_SIZE`, 기본
  워커당 128). 블로킹 대기라 **CPU가 안 오른다** — CPU-HPA가 못 보는
  사각지대 자체가 검증 대상.
- `mock-bedrock`은 실제 `vnd.amazon.eventstream` 바이너리 프레임을 emit —
  boto3 파싱·스레드·소켓 경로가 실제와 동일하게 돌고 SigV4 서명도 수행된다.
  시나리오는 URL의 model_id로 선택된다(게이트웨이 바디 무수정).

| 재현됨 | 미커버 |
|---|---|
| executor 포화 지점·큐잉 깊이 | 실제 Bedrock 쿼터/throttling |
| inflight-메트릭 HPA 스케일 루프 | TLS 비용(mock은 HTTP) |
| DB/RDS Proxy·Redis 핫패스 | 모델별 실제 토큰 분포 |

실 AWS로의 소량 sanity는 §7 참조.

## 2. 인프라 — 상시 유지, 유휴 시 절전

dev 클러스터 기준 이미 배포·시드된 상태(2026-11 실증):

| 리소스 | 평상시 상태 | 비고 |
|---|---|---|
| `mock-bedrock` deploy + svc (llm-gateway ns) | 상주, **`--replicas=0` 절전** | 이미지: `<account>.dkr.ecr.<region>.amazonaws.com/llm-gateway/mock-bedrock:latest` (ECR 상주) |
| 시나리오 모델 별칭 | DB 상주 | `mock-normal` / `mock-think-120` / `mock-hold-300` / `mock-error` / `mock-429` — admin-api에서 provider=BEDROCK 별칭으로 시드됨 |
| endpoint override | **미설정** | 켜져 있는 동안 해당 배포의 **모든** Bedrock 호출이 mock으로 간다 |

> ⚠ override 적용 중 실제 추론은 불가능하다 — **실 사용자가 있는 배포와
> 같은 릴리스에서 켜지 말 것** (env는 프로세스 전역, in-account +
> cross-account 라우팅 양쪽에 적용).

신규 클러스터에 처음 넣을 때:

```bash
# mock 이미지가 없으면 빌드·푸시
docker build -t <ECR>/mock-bedrock:latest loadtest/mock-bedrock
docker push <ECR>/mock-bedrock:latest

# 배포 — Fargate profile 이 커버하는 namespace 여야 함 (dev: llm-gateway)
sed 's|MOCK_BEDROCK_IMAGE|<ECR>/mock-bedrock:latest|' \
  loadtest/k8s/mock-bedrock.yaml | kubectl -n <ns> apply -f -
```

모델 별칭도 처음이면 admin-ui ▸ 모델에서 provider_model_id `mock-*` 5개를
BEDROCK 별칭으로 시드한다.

## 3. 켜기 — mock 활성화

```bash
# 1) mock 기동 (절전 해제 — 서비스 DNS 는 상시 유효)
kubectl -n <ns> scale deploy/mock-bedrock --replicas=2

# 2) 게이트웨이를 mock 으로 리다이렉트 — --set 으로 stored values 에 주입
#    (rebuild-image.sh 와 동일한 --reset-then-reuse-values 경로를 타게 해
#     이후 rebuild 시에도 선언 상태가 일관되게 유지되도록 한다)
helm upgrade llm-gateway deployment/charts/llm-gateway -n <ns> \
  --reset-then-reuse-values \
  --set gatewayProxy.env.AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://mock-bedrock.<ns>.svc.cluster.local:8080
```

파드가 재기동되면(`rollout status`로 확인) 게이트웨이의 모든 Bedrock
호출이 mock으로 향한다.

## 4. 부하 실행

부하 클라이언트용 VK가 필요하다(service_keys 활성 env에서 admin-ui로 발급).

```bash
# k6 — VU = SSE 연결 1개, MODEL 은 콤마로 시나리오 혼합 가능
k6 run -e BASE_URL=https://<gateway-fqdn> \
       -e K6_API_KEY=<vk> \
       -e MODEL=mock-normal,mock-think-120 \
       -e VUS=200 -e HOLD=180s \
       loadtest/k6/sse-hold.js

# 또는 파이썬 로드젠 (간단 버전)
python3 loadtest/sse_loadgen.py --url https://<gateway-fqdn> \
       --key <vk> --conns 200 --model mock-hold-300 --hold 180 --ramp 10
```

## 5. 관측 — executor-inflight HPA 체인 (이 테스트의 주목적)

메트릭 파이프라인:

```
워커별 inflight (service.instance.id={pod}-{pid})
  → OTLP → otel-collector(k8sattributes → k8s.pod.name)
  → Prometheus → prometheus-adapter(max by pod + last_over_time[2m])
  → custom.metrics.k8s.io → HPA type: Pods averageValue
```

관측 명령:

```bash
# HPA — 두 번째 타깃이 pods 메트릭. executorInflightTarget(기본 90 = 워커 풀 128의 ~70%)
kubectl -n <ns> get hpa llm-gateway-gateway-proxy -w
#   TARGETS: cpu: 3%/70%, 140/90  →  "pods metric ... above target" → 스케일아웃

# 커스텀 메트릭 API 직접 확인
kubectl get --raw \
  '/apis/custom.metrics.k8s.io/v1beta1/namespaces/<ns>/pods/*/gateway_bedrock_executor_inflight' \
  | python3 -m json.tool

# Prometheus 에서 adapter 와 동일 쿼리로 가장 포화된 워커 관측
#   max by (k8s_pod_name) (last_over_time(gateway_bedrock_executor_inflight[2m]))
```

기대 동작(검증 포인트):

- **슬롯 ~70%** (워커당 ~90 inflight) 부근에서 스케일아웃 — CPU는 한 자릿수
  그대로. 이게 CPU-HPA 사각지대 실증 + 커스텀 메트릭 HPA의 실제 구동 확인.
- 부하 해제 후 inflight 0 → **300s 안정화 창** 뒤 스케일다운.
- 유휴 상태에서도 `0/90`(not `<unknown>`) — `init_executor_metrics`가 시작
  시점에 시리즈를 만들므로 미보고 파드가 없어야 한다.
- 2차 병목: RDS Proxy/Aurora 커넥션, ElastiCache EngineCPU·`SLOWLOG GET`.

## 6. 끄기 — 원복의 **정확한** 절차

> ⚠ 함정(2026-11 dev 실증): override 키는 한 번 적용되면 **release stored
> values에 남는다**. 오버레이 없이 `helm upgrade` 재실행이나
> `--reuse-values`/`--reset-then-reuse-values`로는 **제거되지 않는다** —
> mock이 계속 살아있어 실 추론이 조용히 mock으로 가는 상태가 된다.

```bash
# 1) stored values export → 해당 키만 삭제 → -f 로 재적용
helm -n <ns> get values llm-gateway -a -o yaml > /tmp/v.yaml
#   gatewayProxy.env.AWS_ENDPOINT_URL_BEDROCK_RUNTIME 라인을 삭제
helm upgrade llm-gateway deployment/charts/llm-gateway -n <ns> -f /tmp/v.yaml

# 2) mock 절전 — deploy/svc·모델 별칭은 다음 테스트를 위해 유지
kubectl -n <ns> scale deploy/mock-bedrock --replicas=0
```

검증: 파드 env에 `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`이 없고
`helm get values -a`에도 없어야 한다(`grep -c` 0).

## 7. 실 AWS sanity (mock 미커버 영역)

mock 종료 후 소량 실호출로 마무리:

- 실제 모델 1~2개로 `/v1/messages` + `/v1/chat/completions` 소량 호출 —
  쿼터·throttling·TLS 경로 확인
- 동시에는 admin-ui 사용량/비용 집계가 실 호출로 쌓이는지 확인(회귀 감시)

## 8. NDS 용량 설정과의 연결

부하테스트 결과가 스케일 동작까지 보이면 그대로 용량 결정에 쓴다:

- 레플리카 범위 조정: `gateway.yaml`의 `deploy.capacity_tier`(밴드) 또는
  `deploy.sizing.gateway-proxy.min_replicas`/`max_replicas` — 세부는
  [install-eks.md](install-eks.md) 용량 절·[gateway-yaml.md](gateway-yaml.md).
- 파드당 수용력: `deploy.sizing.gateway-proxy.bedrock_thread_pool_size` —
  변경 시 `executorInflightTarget`이 `pool×0.7`로 자동 파생(NDS render).
  env values에 명시하면 그 값이 우선.
- 변경 전 확인: `./deploy apply --plan`으로 HPA/env diff → `./deploy apply`,
  `./deploy doctor`로 사후 검증.
