# loadtest — Bedrock-native SSE 동시성 부하테스트

claude-code(Bedrock invoke-with-response-stream) 경로의 동시성 한도를
**실 AWS 호출 없이(비용 $0)** 측정하기 위한 도구 모음.

## 구성

| 경로 | 역할 |
|---|---|
| `mock-bedrock/` | Bedrock Runtime mock — 실제 eventstream 바이너리 emit, 시나리오는 URL 의 model_id 로 선택 |
| `k8s/mock-bedrock.yaml` | `loadtest-mock` ns 에 Deployment+Service |
| `../deployment/charts/llm-gateway/values-eks-fargate-prod-loadtest-sse.yaml` | `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` 오버레이 — boto3 를 mock 으로 리다이렉트 |
| `k6/sse-hold.js` | VU=SSE 연결 1개 hold 시나리오 |

## 왜 mock 인가

병목(executor 스레드 점유/큐잉/HPA 사각지대)은 **연결이 열려있는 시간**의
함수이지 모델 품질의 함수가 아니다. mock 은 실제 `vnd.amazon.eventstream`
바이너리 프레임을 emit 하므로 boto3 파싱·스레드·소켓 경로가 실제와
동일하게 돌고, SigV4 서명도 그대로 수행된다. 실측 검증: botocore 클라이언트가
mock 바이트를 정상 파싱(chunk.bytes, throttlingException, mid-stream
exception, think pause 전부).

커버 안 되는 것: Bedrock 실제 쿼터/throttling 동작, TLS 비용 → 별도 소량
실호출 sanity + 쿼터 증설 요청으로 보완.

## 절차

```bash
# 1) mock 이미지 빌드·푸시 (ECR)
docker build -t <ECR>/mock-bedrock:latest loadtest/mock-bedrock
docker push <ECR>/mock-bedrock:latest

# 2) mock 배포 (manifest 의 MOCK_BEDROCK_IMAGE 치환)
kubectl apply -f loadtest/k8s/mock-bedrock.yaml

# 3) 게이트웨이를 mock 으로 리다이렉트
helm upgrade llm-gateway deployment/charts/llm-gateway -n <ns> \
  -f deployment/charts/llm-gateway/values-eks-fargate-prod.yaml \
  -f deployment/charts/llm-gateway/values-eks-fargate-prod-loadtest-sse.yaml

# 4) 시나리오 모델 별칭 시드 — admin-api 에서 BEDROCK provider 별칭 생성:
#    provider_model_id: mock-normal / mock-think-120 / mock-hold-300 /
#                       mock-error / mock-429
#    (mock 이 URL 의 model_id 로 시나리오를 고른다 — 게이트웨이 바디 무수정)

# 5) VK 발급 후 부하 실행
k6 run -e BASE_URL=https://<gateway> -e K6_API_KEY=<vk> \
       -e MODEL=mock-normal,mock-think-120 \
       -e VUS=200 -e HOLD=180s loadtest/k6/sse-hold.js
```

## 관측

- `gateway_bedrock_executor_inflight` / `gateway_bedrock_executor_queue_depth`
  (bedrock_adapter 계측) — 128 초과 구간부터 큐잉 = starvation 시작
- TTFB(`sse_ttfb_ms`) — 큐 대기와 함께 선형 상승
- HPA 반응 — CPU 낮은 상태로 큐가 쌓이면 CPU-사각지대 실증
- DB/RDS Proxy 커넥션, Redis `SLOWLOG`/EngineCPU — 2차 병목 관측

## 롤백

```bash
helm upgrade llm-gateway deployment/charts/llm-gateway -n <ns> \
  -f deployment/charts/llm-gateway/values-eks-fargate-prod.yaml   # 오버레이 제거
kubectl delete -f loadtest/k8s/mock-bedrock.yaml
```

⚠ 오버레이 적용 중에는 해당 배포의 **모든** Bedrock 호출이 mock 으로 간다.
실 추론 사용자와 같은 배포에서 켜지 말 것.
