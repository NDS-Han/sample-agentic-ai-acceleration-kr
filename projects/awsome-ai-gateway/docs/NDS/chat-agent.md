# admin-chat-agent (BI 챗) 배포 — NDS 옵트인 경로

**기본 상태: 미배포.** NDS bootstrap 이 생성하는 tfvars 는 `enable_chat_agent = false`
(`deploy/bootstrap.py`) 라 인프라가 만들어지지 않고, admin-api 에도
`AGENTCORE_RUNTIME_ARN` 이 없어 `/admin/chat/*` 는 503 으로 닫힌다
(fail-closed). 나중에 켜고 싶을 때의 절차를 정리한다.

## 구성 요소가 어디에 있는가

| 요소 | 위치 | 상태 |
|---|---|---|
| terraform 모듈 | `deployment/terraform/modules/agentcore-runtime/` | 존재 — `count = var.enable_chat_agent` 게이트 |
| env 플래그 | `enable_chat_agent`, `enable_chat_db_tools` (dev/prod variables.tf) | 선언됨, 기본 false |
| admin-api 호출부 | `routers/chat_agent.py` — `AGENTCORE_RUNTIME_ARN`·`AGENTCORE_REGION`·`LAMBDA_QUERY_DB` env | 존재, env 없으면 503 |
| admin-ui 페이지 | `/chat` (ADMIN 전용) | 존재 |
| DB 준비물 | `db/init/08_create_chat_reader.sql` (chat_reader 롤) + `chat_agent` 스키마 + pgvector 임베딩 | init/migration 경로로 이미 적용 |
| AgentCore Runtime 리소스 | terraform **외** — web_search 의 AgentCore Gateway 와 같은 "apply 시 프로비저닝" 패턴 | 수동/boto3 생성 필요 |

## 활성화 절차

### 1. tfvars 플래그 켜기

```hcl
enable_chat_agent    = true
enable_chat_db_tools = true   # query_db / get_schema Lambda — enable_chat_agent 선행
```

`deploy apply` 가 managed overlay(-var-file)로 계획을 범위 검증한 뒤 모듈을
만든다 — ECR repo, KMS key, agent execution role, staging bucket
(`<env>-staging-<account>`, lifecycle 1d), query_db/get_schema Lambda,
Lambda SG. **AgentCore Runtime 리소스 자체는 만들지 않는다** (아래 4번).

### 2. chat_reader DB 비밀번호 설정 (apply 후 수동)

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

### 3. 에이전트 이미지 빌드·푸시

```bash
ECR=$(terraform -chdir=deployment/terraform/environments/<env> output -raw chat_agent_ecr_url)
aws ecr get-login-password | docker login --username AWS --password-stdin ${ECR%%/*}
docker build -t $ECR:<tag> admin-chat-agent/
docker push $ECR:<tag>
```

### 4. AgentCore Runtime 생성 (terraform 외, web_search 와 동일 패턴)

terraform 은 런타임 리소스를 관리하지 않는다 — `web_search` 의 AgentCore
Gateway 처럼 apply 경로 밖에서 boto3/콘솔로 생성한다:

- runtime 에 3번의 ECR 이미지 + `chat_agent_execution_role_arn` output 부여
- agent name 은 `chat_agent_name` output 사용
- 생성 후 ARN 을 확보 — 다음 단계의 env 주입에 쓴다

> 참고: web_search 는 `deploy apply` 가 boto3 로 멱등 프로비저닝한다. chat-agent 도
> 나중에 같은 프로비저너를 deploy 도구에 넣을 수 있지만, **지금은 수동 단계**다.

### 5. admin-api env 주입

`deployment/gateway.yaml` 의 env 주입 경로(`web_search` 의
`AGENTCORE_GATEWAY_URL` 과 같은 `--set` 채널)로 admin-api 에 넣는다:

| env | 값 출처 |
|---|---|
| `AGENTCORE_RUNTIME_ARN` | 4번에서 만든 runtime ARN |
| `AGENTCORE_REGION` | 런타임을 만든 리전 (기본 ap-northeast-2) |
| `LAMBDA_QUERY_DB` | terraform output `chat_agent_query_db_function` |

`deploy render` → `deploy apply` 로 롤아웃. env 가 들어가면 503 이 풀린다.

### 6. 스키마 임베딩 시드

`sql_specialist` 가 쓰는 `chat_agent.schema_embeddings`(pgvector)를 채운다:

```bash
python admin-chat-agent/scripts/build_schema_embeddings.py   # DB 접속 필요
```

`config/schema_whitelist.yaml` 을 바꾼 뒤엔 재시드. (R30: whitelist 의
email `sample_values` 는 합성 값 — 실 PII 넣지 말 것.)

### 7. 검증

- `deploy doctor` + capture 의 `bi_insight` 플래그가 `AGENTCORE_RUNTIME_ARN`
  존재로 켜지는지 확인
- admin-ui `/chat` (ADMIN) 에서 질의 → `chat_agent.sessions/messages` 에 기록
- query_db Lambda 가 Aurora **cluster endpoint** 로 접속함을 확인 —
  RDS Proxy 가 아니다(chat_reader 는 proxy 미등록, 의도된 직결)

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
- **비활성 상태는 fail-closed** — `AGENTCORE_RUNTIME_ARN` 없으면 503. 플래그를
  다시 false 로 돌리면 모듈 리소스는 destroy 되지만 env 주입도 함께 제거할 것.
