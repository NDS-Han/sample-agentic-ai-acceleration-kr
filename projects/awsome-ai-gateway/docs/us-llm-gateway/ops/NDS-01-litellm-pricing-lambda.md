# NDS-01. LiteLLM 단가 조회 Lambda 프록시

> **NDS 전용 절차** (upstream 대상 아님). 모델 단가 동기화의 LiteLLM 소스를
> admin-api 인라인 호출이 아니라 Lambda(`llm-gateway-<env>-litellm-pricing`)
> 경유로 바꿉니다.

## 왜

- admin-api 요청 경로에서 외부(비 AWS) 인터넷 호출을 제거 — 카탈로그 조회를
  Lambda로 격리하고 파드는 `lambda:InvokeFunction`만 호출합니다.
- 정규화(per-token → per-1k)·region-aware lookup·diff/apply 로직은 **admin-api에
  그대로** 둡니다. Lambda는 `/model_catalog` 페이지네이션 원본만 반환합니다
  (로직 이중화 없음, 동기 invoke 6MB 제한 내).

## 동작

```
admin-api ──invoke──▶ Lambda(llm-gateway-<env>-litellm-pricing)
                          │  api.litellm.ai/model_catalog 페이지 조회
                          └─▶ {"data": [...]} 반환 (VPC 밖 — 공개 egress)
admin-api ◀── FetchResult 정규화는 기존 LiteLLMPricingSyncService
```

`adminApi.env.LITELLM_PRICING_LAMBDA`가 함수 이름을 가리킵니다.
**비어 있으면 litellm 소스는 비활성** — sync-preview/apply가 503을 반환하고
admin-ui의 "3rd-party catalog" 선택지가 자동으로 비활성화됩니다
(`GET /admin/models/pricing/sources`). admin-api가 api.litellm.ai를 직접 호출하는
폴백은 없습니다.

## 배포

```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash NDS-01-deploy-litellm-pricing-lambda.sh           # 상태 확인 (읽기 전용)
bash NDS-01-deploy-litellm-pricing-lambda.sh --apply   # role + 함수 + invoke 정책 + values
bash deployment/scripts/install-eks.sh dev           # adminApi.env 반영
```

`--apply`가 만드는 것:

| 리소스 | 내용 |
|---|---|
| IAM role `llm-gateway-<env>-litellm-pricing` | `AWSLambdaBasicExecutionRole`만 — DB/Secret 접근 없음 |
| Lambda `llm-gateway-<env>-litellm-pricing` | python3.12, 256MB, 60s, **VPC 없음**(공개 egress) |
| 인라인 정책 `LitellmPricingInvoke` | admin-api IRSA role에 `lambda:InvokeFunction` 한 건 부여 |
| values | `adminApi.env.LITELLM_PRICING_LAMBDA=<함수이름>` (yq, 백업 생성) |

## 검증

```bash
# Lambda 직접 invoke
aws lambda invoke --function-name llm-gateway-dev-litellm-pricing \
    --payload '{}' /tmp/o.json && cat /tmp/o.json | head -c 300
# → {"data": [{"id": "..."}, ...], "pages": N}

# admin-api 경유 (UI 모델 페이지 → 단가 동기화 → 소스 "3rd-party catalog")
# preview 응답의 source 필드가 lambda:<함수이름> 으로 표시됨
```

## 제거

```bash
bash NDS-01-deploy-litellm-pricing-lambda.sh --delete   # 정책→함수→role 역순 삭제 + values 키 제거
bash deployment/scripts/install-eks.sh dev
```

삭제 후에는 `LITELLM_PRICING_LAMBDA`가 비어 litellm 소스가 비활성화됩니다
(AWS Price List 소스는 영향 없음).

## 주의

- 동기 invoke 응답 한도 6MB — provider 필터(`bedrock_converse`) 적용 시
  현재 카탈로그는 수백 KB 수준. 필터를 비우면 전체 카탈로그가 오므로
  `LITELLM_PROVIDER_FILTER`를 비우지 마십시오.
- Lambda는 `LITELLM_BASE_URL`/`LITELLM_PROVIDER_FILTER` 환경변수로 동일한
  catalog 설정을 씁니다(기본값이 admin-api 설정과 동일).
