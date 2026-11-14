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

지원 타깃: **eks, ecs**. compose 는 스키마가 에러로 차단한다 (로컬 개발용
타깃이라 AgentCore 인프라를 만들지 않는다).

**eks**: `deploy render` 가 managed overlay tfvars 에 `enable_chat_agent = true` +
`enable_chat_db_tools = true` 를 발산하고, `deploy apply` 는 범위 검증을 거쳐
`module.agentcore_runtime` + `module.irsa` 만 `-target` 적용한다 — ECR repo,
KMS key, agent execution role, staging bucket(`<env>-staging-<account>`,
lifecycle 1d), query_db/get_schema Lambda, Lambda SG, admin-api IRSA statement.

끄기는 managed overlay 계약(켜기만 발산)과 동일하게 **env terraform.tfvars 에서
수동**으로 `enable_chat_agent = false` 후 env 수준 apply 이다 — overlay 는
false 를 쓰지 않는다 (NDS render 경로로 켰다면 gen tfvars 재생성으로 자동
수렴). `deploy apply` 는 `bi_insight` 가 꺼져 있으면 Runtime 도 자동으로
teardown 한다 — 아래 "파기" 참조.

**ecs**: render 가 env tfvars 에 같은 플래그를 발산하고 apply 가
`module.agentcore_runtime` + `chat-reader-pw` task def + admin-api task role
statement(InvokeAgentRuntime·lambda invoke·staging s3:GetObject)를 만든다 —
ECS 는 managed overlay 가 없어 env tfvars 로 바로 간다.

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
3. ARN 주입 — eks: `--set adminApi.env.AGENTCORE_RUNTIME_ARN=…` +
   `AGENTCORE_REGION` + `LAMBDA_QUERY_DB` + `CHAT_STAGING_BUCKET`.
   ecs: `agentcore_runtime_arn` 을 extra-vars 로 기록 → 재렌더+재적용으로
   admin-api task env 에 주입 (나머지 LAMBDA/버킷 env 는 모듈 출력이라
   첫 apply 때 이미 배선됨)

실패해도 배포는 중단하지 않는다 — env 미주입 상태로 진행돼 `/chat` 은 503
(graceful-off, web_search 와 같은 의미). Runtime 은 PUBLIC 네트워크 —
agent 가 부르는 것은 Lambda invoke·Bedrock·S3 같은 공개 AWS API 뿐이고,
DB 접속은 VPC 부착 query_db Lambda 가 대신한다.

> Runtime 리전은 `CHAT_AGENT_REGION` env(기본 `ap-northeast-2` — admin-api
> 코드 기본값과 동일). **Runtime 은 ECR 이미지와 같은 리전이어야 한다** —
> `create_agent_runtime` 이 ECR URI 의 리전과 다르면 ValidationException 으로
> 거부한다. 즉 `CHAT_AGENT_REGION` 은 배포 리전(ECR)과 같게 두는 것이 기본
> 안전값이며, tfvars 의 `chat_agent_region` 변수(IRSA 스코프용)도 같은
> `CHAT_AGENT_REGION` env 에서 자동 발산되므로 별도 손댈 필요는 없다.
> Runtime 을 ECR 과 다른 리전에 두려면 이미지를 해당 리전의 ECR 로
> 복제(replication 또는 재푸시)해야 한다.

### 3. chat_reader DB 비밀번호 (프로비저너 자동)

terraform 은 secret **셸**만 만들고, 프로비저너가 apply 마다 비밀번호를
동기화한다. SM 에는 `{"username":"gateway_chat_reader","password":...}` JSON
형식으로 기록된다 — `query_db`·`seed_dev_data` Lambda 가
`json.loads(SecretString)` 하는 계약이고, ecs `chat-reader-pw` task 도
`secretArn:password::` JSON 키 추출로 읽는다. 평문 형식의 기존 값은
프로비저너가 감지해 비밀번호는 재사용하면서 JSON 으로 재기록한다(자가치유).

**eks** (ALTER → SM 순서 — SM 선기록 불일치 창 없음):
1. SM 값이 비어 있거나 형식이 맞지 않으면 `secrets.token_urlsafe(24)` 로 생성
   (평문 레거시는 값 재사용 + JSON 재기록)
2. kubectl 일회성 pod(migration 이미지, `llm-gateway-db` 의 master 자격증명)
   이 클러스터 안에서 `ALTER ROLE gateway_chat_reader` 실행 — deploy 호스트는
   게이트웨이 VPC 밖이라 psql 직접 접속이 안 되므로 인클러스터 경로를 쓴다
3. ALTER 성공 후에만 `put-secret-value`(JSON) — SM 과 DB 가 불일치 상태로
   끝나지 않는다. 매 apply 재실행돼 수동 변경 드리프트가 자가치유된다.

**ecs** (SM → ALTER 순서 — task def `secrets` 주입이 태스크 시작 시 SM 을
해석하므로 SM 기록이 선행돼야 함):
1. SM 값이 비어 있으면 생성·기록
2. `aws ecs run-task` — `chat-reader-pw` task def(migration 이미지)가
   PGPASSWORD(master secret)+CHAT_PW(reader secret 의 `:password::` 키
   추출)를 `secrets` 주입받아 `psql \password` 실행 → exitCode 0 확인
3. ALTER 실패 시 SM≠DB 불일치가 남지만 다음 apply 가 재시도로 수렴한다 —
   평문은 어디에도 남지 않는다(task spec 도 SM ARN 만 담음)

비밀번호는 argv·SQL·pod/task spec 에 평문으로 남지 않는다 (eks: SM → 임시
k8s secret → pod env → `psql \password` stdin — 실행 후 임시 secret·pod
삭제; ecs: task def `secrets` 주입 → 같은 stdin 경로). kubectl/run-task 가
없거나 실패하면 경고만 내고 넘어간다 — 그 경우에만 아래 수동 폴백으로
설정한다:

```bash
# DB 에서 롤 비밀번호 설정 (chat_reader 롤은 db/init/08_* 로 이미 존재)
ALTER ROLE gateway_chat_reader WITH PASSWORD '<생성한 비밀번호>';

# terraform output 의 secret ARN 에 JSON 형태로 저장 — query_db Lambda 가
# json.loads(SecretString)["password"] 로 읽으므로 평문을 넣으면 전부
# JSONDecodeError 로 실패한다 (76c825dc 로 확인된 계약)
aws secretsmanager put-secret-value \
  --secret-id $(terraform -chdir=deployment/terraform/environments/<env> output -raw chat_agent_reader_secret_arn) \
  --secret-string '{"username":"gateway_chat_reader","password":"<생성한 비밀번호>"}'
```

### 4. 스키마 임베딩 시드 (Phase 2 — 현재 미연결)

`chat_agent.schema_embeddings`(pgvector) 테이블은 마이그레이션 0005 에
생성돼 있지만, **현재 런타임은 이 테이블을 읽지 않는다** — sql_specialist 의
스키마 인식은 `get_schema` Lambda(정적 whitelist 메타데이터)와
`fewshot.py` 의 Jaccard few-shot 으로 동작한다. 이 시드는 Phase 2 RAG
schema-linking 이 코드에 연결되기 전까지 **실행 불필요**다:

```bash
python admin-chat-agent/scripts/build_schema_embeddings.py   # DB 접속 필요
```

실행해도 무해하나(멱등) 읽는 코드가 없어 효과는 없다. (R30: whitelist 의
email `sample_values` 는 합성 값 — 실 PII 넣지 말 것.)

### 5. 검증

- `deploy doctor` + capture 의 `bi_insight` 플래그가 `AGENTCORE_RUNTIME_ARN`
  존재로 켜지는지 확인
- admin-ui `/chat` (ADMIN) 에서 질의 → `chat_agent.sessions/messages` 에 기록
- query_db Lambda 가 Aurora **cluster endpoint** 로 접속함을 확인 —
  RDS Proxy 가 아니다(chat_reader 는 proxy 미등록, 의도된 직결)

## 파기

두 경로 모두 선언 수렴이다 — 수동으로 지울 것은 없다.

- **기능만 끄기**: `features.bi_insight: false` 후 `deploy apply` →
  `chatagent.teardown()` 이 Runtime 을 멱등 삭제하고(없으면 조회 한 번으로
  종료), managed overlay/env tfvars 가 `module.agentcore_runtime` 을
  destroy, admin-api env 도 비워진다 (eks: helm values 리셋, ecs:
  extra-vars 의 `agentcore_runtime_arn` 자동 제거).
- **환경 통째 파기**: `deploy teardown` — helm uninstall + terraform
  destroy 에 이어 web_search Gateway·pricing Lambda 와 같은 자리에서
  Runtime 도 함께 정리한다 (삭제 확인 패널에 표시됨).

남는 것: `gateway_chat_reader` DB role·스키마 임베딩 데이터(Aurora 잔여,
재배포 시 재시드로 수렴), KMS 7일 예약 삭제, chat_reader secret 30일 복구창
— 빠른 재배포가 같은 이름 생성에 막히면 `aws secretsmanager restore-secret`.

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
