# gateway.yaml 설정 설명서

`deployment/gateway.yaml` 은 배포의 **유일한 설정 원본**입니다. 이 파일을 고치고
`./deploy apply`(plan 미리보기 → 확인 → 적용)를 실행하는 것이 모든 변경의
표준 경로입니다. 직접 편집 대신 `./deploy configure` 로 현재 값을 기본값으로
항목을 하나씩 확인하며 바꿀 수도 있습니다.

스키마는 **엄격**합니다 — 목록에 없는 키(오타 포함)는 오류이며, bool 플래그는
`true`/`false` 만 받습니다(인용된 `"false"` 문자열은 거부). 오타가 기본값으로
조용히 적용되는 사고(예: `allowed_cidrs` 오타로 방화벽이 열림)를 막기 위해서입니다.

## 전체 구조

```yaml
version: 1                    # 스키마 버전 — 1 고정
env: my-gw                    # 환경 이름 (소문자·숫자·하이픈)

aws:
  region: ap-northeast-2      # Bedrock·배포 리전
  account_id: "123456789012"  # ecs/eks 에서 필요할 수 있음

deploy:
  target: compose             # compose | ecs | eks
  size_tier: t0               # t0 | t1 | t2 | t3
  sizing: {}                  # 티어 기본값 세부 override (선택)
  tfstate_bucket: ""          # ecs/eks: terraform state S3 bucket
  tfstate_table: ""           # ecs/eks: lock DynamoDB table (권장)
  release: llm-gateway        # eks 전용 — helm release 이름
  namespace: llm-gateway      # eks 전용 — k8s namespace
  tf_env_dir: ""              # eks 전용 — 이 배포의 인프라를 소유한 terraform env
                              # 디렉토리 (비우면 deployment/terraform/environments/
                              # llm-gateway-<env>)

network:
  mode: public                # public | private
  allowed_cidrs: ["1.2.3.0/24"]
  admin_internal: false       # eks 전용 — admin(admin-ui/api) Ingress만 internal ALB로

domain:
  mode: none                  # none | cloudfront-temp | route53-acm
  name: example.com           # route53-acm 일 때 필수
  zone_id: Z123...            # Route53 hosted zone — ecs+route53-acm 이면 필수
                              # (eks 는 env values 의 기존 인증서/레코드를 쓰므로 미사용)

features:
  notifications:
    provider: mock            # mock | smtp | ses | internal_api
    smtp_host: ""
    smtp_port: 587
    smtp_from: ""
    ses_from: ""
    sender_name: ""           # 발신자 표시명 — 비우면 "LLM Gateway"
    internal_api_url: ""      # provider=internal_api 일 때 필수 — 사내 메일 API URL
    internal_api_from: ""     # internal_api 발신 주소 — 비우면 차트 기본
    smtp_credentials_secret: ""  # eks 전용 — SMTP 인증 Secret 이름(credentialsSecretName)
  bi_insight: false
  pricing_lambda: false
  web_search: false
  body_logging: false
  observability: false        # OTel/Prometheus/Grafana (compose 전용 스위치)
  dev_login: false            # oidc 와 공존하는 개발자 로그인 — true 면 adminUi/adminApi 양쪽에 DEV_LOGIN_ENABLED=true 유지

images:
  registry: ""                # 외부 레지스트리 — 비우면 ecs 가 ECR repo 자동 생성 (eks 는 필수)
  tag: ""                     # 기본 이미지 태그 — ecs/eks 는 tag 또는 tags 중 하나 필수 (명시적 핀)
  # tags:                     # eks 전용 — 서비스별 핀 (tag 위에 개별 키를 덮는다)
  #   gatewayProxy: "1.0.87-us19"   # 허용 키: gatewayProxy adminApi adminUi scheduler
  #   adminApi: "1.0.71-us19"       #            notificationWorker costRecorderWorker migration
  #   migration: "1.0.54-us19"      # migration 은 단일 tag 로는 안 바뀜 — 여기서만 핀

clients:
  models_profile: global      # global (권장) | regional

oidc:                          # 비워두면 OIDC 비활성(dev-login)
  issuer_url: ""
  audience: ""
  client_id: ""
  authorize_url: ""
  token_url: ""
  provider_name: oidc:cognito
  required_group: ""
  client_secret: ""           # Cognito confidential client — 있으면 HTTP Basic 토큰 교환
  vk_ttl_hours: 0            # VK 수명(시간) — 0이면 기본 1시간

infra:                         # 신규 설치 인프라 입력 — `deploy bootstrap` wizard 가 기록
                               # (ecs/eks 전용, compose 무시, 기존 배포 온보딩엔 미사용)
  azs: []                      # 배포할 가용영역 — 2개 이상 (DB/Cache subnet group 요건)
  vpc_cidr: ""                 # 비우면 backend 기본 (eks 10.30/16, ecs 10.40/16)
  cognito_domain_suffix: ""    # 비우면 <region-code>-auth-<account> (예: apne2-auth-123…)
  cognito_groups: []           # 'Claude_<team>' / 'Claude_<dept>_<team>' — ClaudeAdmin 자동 포함
  bedrock_allowed_model_arns: []  # 비우면 backend 기본 — 명시 시 models_profile 매핑을 덮어씀
  db: {}                       # ecs 전용 — Aurora override (아래 'infra.*' 표)
  cache: {}                    # ecs 전용 — ElastiCache override (아래 'infra.*' 표)
```

## 항목별 상세

### `deploy.target` / `deploy.size_tier`

허용 조합만 됩니다:

| target | 가능한 tier |
|---|---|
| compose | t0 만 (단일 호스트) |
| ecs | t1, t2 |
| eks | t2, t3 |

### `deploy.sizing`

ecs 경로에서 서비스별 cpu/memory/desired_count(+ 오토스케일 경계)를 티어
기본값에서 override:

```yaml
deploy:
  sizing:
    gateway-proxy: { cpu: 2048, memory: 4096, desired_count: 4, max_count: 16 }
```

지정한 키만 바뀌고 나머지는 티어 값이 유지됩니다.

- `max_count` — ECS service autoscaling 상한. 기본은 트래픽을 받는 두 서비스만:
  `gateway-proxy` desired×4, `admin-api` desired×2 (CPU 60%·Mem 70%
  target-tracking). 나머지 서비스는 min=max=desired 로 핀됩니다.
  `desired_count` 만 override 하면 상한도 같은 배수로 재계산됩니다 —
  프리셋 기준의 stale 상한이 남지 않습니다.
- `min_count` — 오토스케일 하한(기본=desired_count). desired_count 변경은
  target 의 min/max 경계로 전파됩니다.
- `cost-recorder-worker` 는 스트림 consumer 이름 고정이라 desired_count 를
  늘리면 안 됩니다 (렌더러가 항상 1 로 고정).
- **DB 커넥션 예산**: `validate` 가 worst-case pool 수요
  (pool 10+overflow 20 × 프로세스 × max_count)가 Aurora `max_acu`의
  ~225conn/ACU 대비 80% 를 넘으면 에러로 막습니다 — 스케일아웃이 DB 를
  고갈시키는 경로를 선언 단계에서 차단. 정상 수요(pool_size)가 `min_acu`
  바닥을 넘으면 경고. 더 큰 탄력이 필요하면 RDS Proxy 도입을 검토하세요
  (eks prod 가 이미 사용).

### `infra.*` — 신규 설치 인프라 입력

`deploy bootstrap` 실행 시 대화형 wizard 가 같은 질문을 묻고 답을 여기에 기록합니다
(파일로 직접 써도 됩니다 — CI/`--yes` 경로는 이 파일 값만 읽습니다).

| 키 | 의미 |
|---|---|
| `azs` | 배포할 가용영역 목록. **2개 이상 필수**(Aurora/ElastiCache subnet group 요건). 선언하면 private/public/database/elasticache 서브넷 CIDR 리스트가 개수에 맞춰 자동 생성됩니다 (`{a}.{b}.{i}/24`, `{100+i}`, `{200+i}`, `{210+i}` — 두 env 의 variables.tf 규약) |
| `vpc_cidr` | VPC 대역. 바꾸면 위 서브넷 CIDR 도 이 대역에서 생성됩니다 |
| `cognito_domain_suffix` | Hosted UI suffix. 비우면 `<region-code>-auth-<account>` 자동 (us-east-1→use1, ap-northeast-2→apne2) |
| `cognito_groups` | OIDC 팀 그룹. `Claude_` 접두 필수(없는 그룹은 로그인 거부 — 스키마 검증). `ClaudeAdmin`은 자동 포함 |
| `bedrock_allowed_model_arns` | IRSA 정책 허용 ARN 패턴. 비우면 backend 기본(eks=variables.tf 최신 목록, ecs=`clients.models_profile` 매핑) — 명시 시 그 목록을 덮어씁니다 |
| `db.*` | **ecs 전용** — tier 프리셋 위에 얹는 DB 세부 override: `mode`(serverless\|provisioned), `min_acu`/`max_acu`(0.5 단위, 0=일시정지), `instance_class`(provisioned 시). 매 `apply`마다 tfvars로 내려가므로 **업데이트 경로에서도 동작** — `configure`의 고급 분기나 직접 편집 후 apply. mode 전환은 재생성급 변경(노트 출력) |
| `cache.*` | **ecs 전용** — ElastiCache override: `mode`(single\|replicated\|cluster), `node_type`(non-cluster·cluster 모두에 적용), `num_clusters`(non-cluster 노드 수 1~6 — replicated면 ≥2: primary+replica로 failover/Multi-AZ 자동), `num_node_groups`(cluster 샤드 수 1~90), `replicas_per_node_group`(cluster 샤드당 replica 1~5). `db.*`와 같은 override 관계. 토폴로지 키는 effective mode와 맞아야 하며 어긋나면 스키마가 거부합니다(terraform 무시 방지). node_type/mode 변경은 캐시 재기동·failover를 동반 |

`infra.*` 는 bootstrap 의 managed overlay/generated tfvars 로만 내려갑니다 —
단 `infra.db`/`infra.cache`(ecs)는 `deploy.sizing`처럼 매 apply의 tfvars에
포함되므로 설치 후 크기 변경에도 씁니다. `validate`가 worst-case DB 커넥션
예산을 이 override를 반영한 실효값으로 계산합니다.

### `deploy.tfstate_bucket` / `deploy.tfstate_table`

ecs/eks 경로의 terraform state 위치. 비우면 `llm-gateway-tfstate-<account_id>`
규칙으로 추론하고, account_id 도 없으면 placeholder 가 생성됩니다
(그 경우 apply 전에 채워야 합니다).

### `network`

- `mode: private` — eks는 3개 Ingress 전부 internal ALB로, ecs는 ALB를
  internal 로 전환합니다(전환 절차·사내망 전제는 [update.md](update.md)
  'public → private 전환'). compose는 적용되지 않습니다(validate가 경고)
- `admin_internal` — eks 전용. `mode: public`을 유지하면서 **admin-ui·admin-api
  Ingress만** internal ALB로 분리합니다. `mode: private`이면 이미 전부 internal이라
  중복 선언입니다(스키마가 경고). compose/ecs에서는 무시됩니다(경고 출력)
- `allowed_cidrs` — 인입 허용 CIDR. 비우면 전체 개방이 되므로 운영 환경에서는
  반드시 채우세요 ([operations.md](operations.md#2-직원-오픈-전-하드닝-체크리스트) §2)

### `domain.mode`

| 값 | 의미 | TLS |
|---|---|---|
| `none` | 도메인 없음 — 포트별 접속(8000/8080/3000) | HTTP 만 |
| `cloudfront-temp` | CloudFront 기본 도메인으로 임시 https | 있음 (EKS 전용) |
| `route53-acm` | 실제 도메인 + 인증서 | 있음 |

도메인은 **나중에 바꿀 수 있습니다** — 다만 이미 설치된 클라이언트의 게이트웨이
주소가 바뀌므로 재안내가 필요합니다.

`route53-acm` + `name: example.com` 이면 `gateway.` `admin-api.` `admin.` 세
서브도메인이 자동으로 쓰입니다.

### `features`

| 키 | 켜면 생기는 것 | 주의 |
|---|---|---|
| `notifications.provider` | mock=발송안함, ses=SES 발송, smtp=외부 SMTP, internal_api=사내 메일 API | ses는 `ses_from`, smtp는 `smtp_host`, internal_api는 `internal_api_url` 필수(없으면 worker crashloop). eks의 smtp 자격증명은 `smtp_credentials_secret` |
| `bi_insight` | BI 어시스턴트 (AgentCore Runtime) | compose 미지원. 별도 이미지 배포 필요 |
| `pricing_lambda` | LiteLLM 단가 조회 Lambda | **ecs/eks는 apply가 자동 보장** (role+함수+invoke 권한, 멱등) — compose는 ops/NDS-01 수동. 단가 데이터는 DB 시드가 따로 필요 |
| `web_search` | AgentCore 기반 웹검색 | **us-east-1 전용 인프라**, apply가 자동 프로비저닝 (compose는 .env 주입) |
| `body_logging` | 요청 본문 S3 로깅 | 인프라 + 관리자 런타임 토글 이중 게이트 |
| `observability` | OTel/Prometheus/Loki/Tempo/Grafana | **compose 전용** — ecs/eks에서는 무시(validate가 경고) |
| `dev_login` | 인증 없는 개발자 로그인 (oidc 와 공존) | 기본 false: oidc 설정 시 admin-ui `DEV_LOGIN_ENABLED` 를 끈다. true: adminUi+adminApi 양쪽에 켠다 — 인터넷 노출 시 `allowed_cidrs` 제한 필수 |

### `oidc`

admin-api(JWT 검증)와 admin-ui(브라우저 SSO)가 공유하는 설정입니다.

- 비워두면: OIDC 비활성 → admin-ui는 dev-login으로만 진입 (**운영 전환 시 반드시 설정**)
- 채우면: `DEV_LOGIN_ENABLED=false` 자동 적용
- `authorize_url`/`token_url` 은 issuer가 **아닙니다** — Cognito hosted-ui 도메인
  (`https://<domain>.auth.<region>.amazoncognito.com/oauth2/...`)입니다
- `client_secret` — Cognito **confidential** client를 쓸 때만. 있으면 토큰 교환을
  HTTP Basic으로 합니다. public client면 비워두세요
- `required_group` — 비우면 인증된 모든 사용자 통과. 특정 그룹 소속만 허용하려면 지정
- `vk_ttl_hours` — 발급된 Virtual Key의 수명. 세 backend 모두 반영됩니다
  (eks=차트 `adminApi.oidc.vkTtlHours`, ecs/compose=`OIDC_VK_TTL_HOURS`)

### `clients.models_profile`

- `global` (기본·권장): `global.anthropic.*` inference profile — 어느 리전에서든 동작
- `regional`: 리전별 프로필 — 특정 리전 정책이 필요한 경우만
- **ecs/compose 에서만 적용** — ecs는 IRSA 허용 ARN 매핑에, compose는 시드 안내에
  반영됩니다. eks는 env tfvars(`bedrock_allowed_model_arns`)와 차트 env values
  (`modelsProfile`)가 소유하므로 이 키는 무시됩니다(validate 경고)

## 변경 후 절차

```bash
vi deployment/gateway.yaml
./deploy validate          # 스키마 검증 (실수 조기 발견)
./deploy render            # compose 산출물 재생성 (시크릿 보존)
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d
./deploy doctor            # 결과 확인
```

## 수동으로 바꾸면 안 되는 것

`deployment/gen/<env>/` 안의 파일은 전부 생성물입니다 — 직접 고치면 다음
`render` 때 덮어씌워지고 `doctor` 가 drift로 보고합니다. 바꾸고 싶은 값이
gateway.yaml에 없으면 스키마에 노브를 추가하세요.

예외: `.env` 에 `AWS_ACCESS_KEY_ID` 같이 **스키마에 없는 키를 추가하는 것**은
허용됩니다 — render는 있는 키를 보존하고 없는 키만 채웁니다.
