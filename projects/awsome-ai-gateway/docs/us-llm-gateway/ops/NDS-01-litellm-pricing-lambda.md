# NDS-01. LiteLLM 단가 조회 Lambda 프록시

> **NDS 전용 절차** (upstream 대상 아님). 모델 단가 동기화의 LiteLLM 소스를
> admin-api 인라인 호출이 아니라 Lambda(`llm-gateway-<env>-litellm-pricing`)
> 경유로 바꿉니다.

## 왜

- admin-api 요청 경로에서 외부(비 AWS) 인터넷 호출을 제거 — 카탈로그 조회를
  Lambda로 격리하고 파드는 `lambda:InvokeFunction`만 호출합니다.
- 정규화(per-token → per-1k)·region-aware lookup·diff/apply 로직은 **admin-api에
  그대로** 둡니다. Lambda는 카탈로그 원본만 반환합니다
  (로직 이중화 없음, 동기 invoke 6MB 제한 내).

## 동작

```
admin-api ──invoke──▶ Lambda(llm-gateway-<env>-litellm-pricing)
                          │  LITELLM_COST_MAP_URL (GitHub
                          │  model_prices_and_context_window.json) 조회
                          │  → litellm_provider 필터 → catalog rows 변환
                          └─▶ {"data": [...]} 반환 (VPC 밖 — 공개 egress)
admin-api ◀── FetchResult 정규화는 기존 LiteLLMPricingSyncService
```

`adminApi.env.LITELLM_PRICING_LAMBDA`가 함수 이름을 가리킵니다.
**비어 있으면 litellm 소스는 비활성** — sync-preview/apply가 503을 반환하고
admin-ui의 "3rd-party catalog" 선택지가 자동으로 비활성화됩니다
(`GET /admin/models/pricing/sources`). admin-api가 api.litellm.ai를 직접 호출하는
폴백은 없습니다.

> **cost-map 모드** (2026-11~): `api.litellm.ai/model_catalog` API가 종료돼
> `LITELLM_COST_MAP_URL`(GitHub `model_prices_and_context_window.json`)로
> 동작합니다. 이 env가 없으면 람다는 죽은 API를 호출해 `errors`를 반환합니다.
> 두 배포 경로 모두 이 env를 자동 주입합니다 — 수동으로 지우면 안 됩니다.

## 배포

**정석 — `features.pricing_lambda` (eks/ecs)**: `gateway.yaml`에 켜 두면
`./deploy apply`가 role+함수+invoke 정책+**코드+env**를 매번 멱등 보장합니다
(`deploy/pricinglambda.ensure`, 실패해도 배포는 계속 — graceful-off).
람다 코드를 수정한 뒤에는 `apply`만 돌리면 됩니다.

```yaml
features:
  pricing_lambda: true
```

**수동 경로** (compose·스크립트만 쓰는 환경):

```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash NDS-01-deploy-litellm-pricing-lambda.sh           # 상태 확인 (읽기 전용)
bash NDS-01-deploy-litellm-pricing-lambda.sh --apply   # role + 함수 + env + invoke 정책 + values
bash deployment/scripts/install-eks.sh dev           # adminApi.env 반영
```

`--apply`/`ensure`가 만드는 것:

| 리소스 | 내용 |
|---|---|
| IAM role `llm-gateway-<env>-litellm-pricing` | `AWSLambdaBasicExecutionRole`만 — DB/Secret 접근 없음 |
| Lambda `llm-gateway-<env>-litellm-pricing` | python3.12, 256MB, 60s, **VPC 없음**(공개 egress) |
| 함수 env | `LITELLM_COST_MAP_URL`=GitHub cost map (배포가 관리 — update-config 전체 교체) |
| 인라인 정책 `LitellmPricingInvoke` | admin-api IRSA role에 `lambda:InvokeFunction` 한 건 부여 |
| values / helm --set | `adminApi.env.LITELLM_PRICING_LAMBDA=<함수이름>` |

## 검증

```bash
# Lambda 직접 invoke — cost-map 모드에서는 bedrock_converse 행만 온다
aws lambda invoke --function-name llm-gateway-dev-litellm-pricing \
    --payload '{}' /tmp/o.json && python3 -c \
    "import json;d=json.load(open('/tmp/o.json'));print(len(d.get('data',[])),'models')"
# → 233 models  (2026-11 기준, errors 없어야 함)

# 함수 env 확인 — LITELLM_COST_MAP_URL 이 있어야 cost-map 모드
aws lambda get-function-configuration \
    --function-name llm-gateway-dev-litellm-pricing \
    --query 'Environment.Variables'

# admin-api 경유 (UI 모델 페이지 → 단가 동기화 → 소스 "3rd-party catalog")
# preview 응답의 source 필드가 lambda:<함수이름> 으로 표시됨
```

## 제거

```bash
# pricing_lambda 를 켠 채 배포했다면 deploy 가 소유한 리소스 — teardown이 정리
./deploy teardown                       # eks/ecs: 정책→함수→role 역순 삭제

# 수동 경로로 만든 경우
bash NDS-01-deploy-litellm-pricing-lambda.sh --delete   # + values 키 제거
bash deployment/scripts/install-eks.sh dev
```

삭제 후에는 `LITELLM_PRICING_LAMBDA`가 비어 litellm 소스가 비활성화됩니다
(AWS Price List 소스는 영향 없음).

## 주의

- 동기 invoke 응답 한도 6MB — cost-map은 ~3MB 단일 파일이라 안전하지만,
  `LITELLM_PROVIDER_FILTER`를 비우면 필터 없는 전체 행이 반환될 수 있습니다.
  기본 `bedrock_converse`를 유지하십시오.
- 함수 env는 배포 경로(`pricinglambda.FN_ENV` / 스크립트 `COST_MAP_URL`)가
  소스 오브 트루스 — 콘솔에서 수동 추가한 env는 다음 apply/갱신 때 지워집니다.
- `LITELLM_BASE_URL`/`LITELLM_PROVIDER_FILTER`/`LITELLM_PAGE_SIZE`는
  model_catalog 모드 전용이라 cost-map 모드에서는 사용되지 않습니다
  (`LITELLM_PROVIDER_FILTER`만 cost-map의 `litellm_provider` 필드에 재사용).
