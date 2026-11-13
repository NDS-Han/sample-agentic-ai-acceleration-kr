# admin-chat-agent (BI 챗) 배포 — NDS 옵트인 경로

**기본 상태: 미배포.** `features.bi_insight` 는 기본 false 이고 tfvars scaffold
도 `enable_chat_agent = false`(`deploy/bootstrap.py`)라 인프라가 만들어지지
않으며, admin-api 에도 `AGENTCORE_RUNTIME_ARN` 이 없어 `/admin/chat/*` 는
503 으로 닫힌다(fail-closed). 나중에 켜고 싶을 때의 절차를 정리한다.

## 구성 요소가 어디에 있는가

| 요소 | 위치 | 상태 |
|---|---|---|
| terraform 모듈 | `deployment/terraform/modules/agentcore-runtime/` | 존재 — `count = var.enable_chat_agent` 게이트 |
| gateway.yaml 플래그 | `features.bi_insight` | 선언됨, 기본 false — managed overlay 로 `enable_chat_agent`/`enable_chat_db_tools` 발산 |
| admin-api 호출부 | `routers/chat_agent.py` — `AGENTCORE_RUNTIME_ARN`·`AGENTCORE_REGION`·`LAMBDA_QUERY_DB`·`CHAT_STAGING_BUCKET` env | 존재, env 없으면 503 |
| admin-api IRSA | `module.irsa` 의 `ChatAgentRuntimeInvoke`·`ChatAgentToolLambdaInvoke`·`ChatAgentReportPresign` statement | `enable_chat_agent` 게이트 |
| admin-ui 페이지 | `/chat` (ADMIN 전용) | 존재 |
| DB 준비물 | `db/init/08_create_chat_reader.sql` (chat_reader 롤) + `chat_agent` 스키마 + pgvector 임베딩 | init/migration 경로로 이미 적용 |
| AgentCore Runtime 리소스 | terraform **외** — `deploy apply` 가 `provision_agentcore_chatagent.py` 로 멱등 프로비저닝 (web_search 와 동일 패턴) | 자동화됨 |
| 에이전트 이미지 | ECR `chat_agent_ecr_url` repo — 프로비저너가 없으면 docker/finch 로 자동 빌드·푸시 | apply 시 보장 |

## 활성화 절차

### 1. `gateway.yaml` 에 플래그 켜기

```yaml
features:
  bi_insight: true
```

`deploy render` 가 managed overlay tfvars 에 `enable_chat_agent = true` +
`enable_chat_db_tools = true` 를 발산하고, `deploy apply` 는 범위 검증을 거쳐
`module.agentcore_runtime` + `module.irsa` 만 `-target` 적용한다 — ECR repo,
KMS key, agent execution role, staging bucket(`<env>-staging-<account>`,
lifecycle 1d), query_db/get_schema Lambda, Lambda SG, admin-api IRSA statement.

끄기는 managed overlay 계약(켜기만 발산)과 동일하게 **env terraform.tfvars 에서
수동**으로 `enable_chat_agent = false` 후 env 수준 apply 이다 — overlay 는
false 를 쓰지 않는다.

### 2. `deploy apply` — Runtime 자동 프로비저닝

apply 가 `chatagent.ensure_runtime_arn()` 을 호출해:

1. `status` — 같은 이름(`chat_agent_name` 의 하이픈→밑줄 정규화)의 Runtime
   이 있으면 재사용
2. 없으면 `deploy`:
   - ECR 에 `<ecr_url>:latest`(IMAGE_TAG env 로 변경 가능)가 있는지 확인 —
     없으면 docker/finch 로 `linux/arm64` 빌드·푸시를 시도하고, 빌더가
     없으면 수동 빌드 명령을 출력하고 graceful-off
   - `create_agent_runtime`(containerUri, exec role, `networkMode: PUBLIC`,
     LAMBDA_QUERY_DB·LAMBDA_GET_SCHEMA·CHAT_STAGING_BUCKET·AWS_REGION env)
   - READY 까지 폴링
3. ARN 을 `--set adminApi.env.AGENTCORE_RUNTIME_ARN=…` + `AGENTCORE_REGION` +
   `LAMBDA_QUERY_DB` + `CHAT_STAGING_BUCKET` 으로 주입

실패해도 배포는 중단하지 않는다 — env 미주입 상태로 진행돼 `/chat` 은 503
(graceful-off, web_search 와 같은 의미). Runtime 은 PUBLIC 네트워크 —
agent 가 부르는 것은 Lambda invoke·Bedrock·S3 같은 공개 AWS API 뿐이고,
DB 접속은 VPC 부착 query_db Lambda 가 대신한다.

> Runtime 리전은 `CHAT_AGENT_REGION` env(기본 `ap-northeast-2` — admin-api
> 코드 기본값과 동일). tfvars 의 `chat_agent_region` 변수(IRSA 스코프용)와
> 반드시 같은 값이어야 한다 — 다르게 쓰려면 둘 다 바꿀 것.

### 3. chat_reader DB 비밀번호 설정 (apply 후 수동, 유일한 수동 단계)

terraform 은 secret **셸**만 만든다 — `gateway_chat_reader` Postgres 롤의
비밀번호는 out-of-band 로 설정한다:

```bash
# DB 에서 롤 비밀번호 설정 (chat_reader 롤은 db/init/08_* 로 이미 존재)
ALTER ROLE gateway_chat_reader WITH PASSWORD '<생성한 비밀번호>';

# terraform output 의 secret ARN 에 저장 — query_db Lambda 가 여기서 읽는다
aws secretsmanager put-secret-value \
  --secret-id $(terraform -chdir=deployment/terraform/environments/<env> output -raw chat_agent_reader_secret_arn) \
  --secret-string '<생성한 비밀번호>'
```

### 4. 스키마 임베딩 시드

`sql_specialist` 가 쓰는 `chat_agent.schema_embeddings`(pgvector)를 채운다:

```bash
python admin-chat-agent/scripts/build_schema_embeddings.py   # DB 접속 필요
```

`config/schema_whitelist.yaml` 을 바꾼 뒤엔 재시드. (R30: whitelist 의
email `sample_values` 는 합성 값 — 실 PII 넣지 말 것.)

### 5. 검증

- `deploy doctor` + capture 의 `bi_insight` 플래그가 `AGENTCORE_RUNTIME_ARN`
  존재로 켜지는지 확인
- admin-ui `/chat` (ADMIN) 에서 질의 → `chat_agent.sessions/messages` 에 기록
- query_db Lambda 가 Aurora **cluster endpoint** 로 접속함을 확인 —
  RDS Proxy 가 아니다(chat_reader 는 proxy 미등록, 의도된 직결)

## 수동 폴백 (프로비저너 없이)

프로비저너를 쓰지 않고 직접 만들려면:

```bash
# 이미지
ECR=$(terraform -chdir=<env_dir> output -raw chat_agent_ecr_url)
aws ecr get-login-password | docker login --username AWS --password-stdin ${ECR%%/*}
docker build --platform linux/arm64 -t $ECR:latest admin-chat-agent/
docker push $ECR:latest

# Runtime — provisioner 를 직접 실행(멱등, NDS 가 쓰는 것과 같은 스크립트)
REGION=ap-northeast-2 \
AGENT_NAME=$(terraform -chdir=<env_dir> output -raw chat_agent_name | tr - _) \
ECR_REPO=$ECR \
ROLE_ARN=$(terraform -chdir=<env_dir> output -raw chat_agent_execution_role_arn) \
LAMBDA_QUERY_DB=$(terraform -chdir=<env_dir> output -raw chat_agent_query_db_function) \
LAMBDA_GET_SCHEMA=$(terraform -chdir=<env_dir> output -raw chat_agent_get_schema_function) \
CHAT_STAGING_BUCKET=$(terraform -chdir=<env_dir> output -raw chat_agent_staging_bucket) \
python deployment/scripts/provision_agentcore_chatagent.py deploy
# → AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:… 출력
```

ARN 을 `gateway.yaml` env 주입 채널이 아니라 다음 apply 에 자동 주입된다
(status 조회가 재사용하므로). 완전 수동이라면 `adminApi.env.AGENTCORE_*` 를
values 에 직접 세팅.

## 배선 상 참고

- **query_db 는 cluster endpoint 직결** — RDS Proxy 우회(모듈 주석: 저빈도 BI,
  chat_reader 가 proxy 통과 불가). Lambda SG → cluster SG 5432 ingress 로 허용.
- **chat_reader 권한 경계** — `key_value_encrypted`·`sso_subject`·`chat_agent.*`
  제외/REVOKE — `db/tests/test_chat_reader_grants_match_whitelist.py` 가
  whitelist↔grant 드리프트를 핀한다.
- **staging bucket** — 쿼리 결과 staging(S3, KMS, 1일 lifecycle). R30 부터
  `force_destroy` 는 `environment == "dev"` 게이트.
- **seed_dev_data 는 dev 전용** — `ENVIRONMENT != "dev"` 이면 핸들러가 거부
  (쓰기 롤로 합성 usage 주입 방지, R30 F11).
- **비활성 상태는 fail-closed** — `AGENTCORE_RUNTIME_ARN` 없으면 503.
- **이름 규칙** — Runtime 이름은 `[a-zA-Z][a-zA-Z0-9_]{0,47}`(하이픈 불가)라
  terraform output(`llm-gateway-dev-admin-chat-agent`)을 밑줄로 정규화해
  쓴다. 이름을 바꾸면 기존 Runtime 을 재사용하지 않고 중복이 생긴다.
- **admin-api IRSA 스코프** — `InvokeAgentRuntime` 은 생성 시 붙는 runtime id
  를 특정할 수 없어 `<region>:<acct>:runtime/*` 로 열어둔다 (InvokeGateway 의
  `gateway/*` 와 동일 패턴).
