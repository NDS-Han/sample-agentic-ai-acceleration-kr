# R31 — docs/NDS/deploy/ (선언적 배포 도구) 리뷰

날짜: 2026-10 · 대상: `docs/NDS/deploy/` ~4.2k LOC (cli/schema/render/capture/doctor/eks_apply/ecs_apply/tiers)
베이스라인: pytest 111 passed → 수정 후 120 passed.

## 범위

`deploy` CLI 전체 — gateway.yaml 스키마/검증, compose·ecs·eks 3개 backend 렌더러,
terraform/helm 실행기, 기존 배포 역생성(capture), 상태·드리프트 점검(doctor),
init/configure/teardown 플로우, subprocess 경계, 시크릿 취급.

## 발견·수정

| # | 심각도 | 파일 | 발견 | 수정 |
|---|---|---|---|---|
| 1 | M | `eks_apply.py` | **notification-worker SES IRSA 미배선** — `dynamic_set_args`가 gatewayProxy/adminApi만 --set-string 주입. provider=ses 이면 install-eks.sh(R24-S7)가 하던 `notification_worker_ses_role_arn` 배선이 빠져 SA 자격 없음 → 발송 전량 실패 | provider=ses 일 때 output 있으면 주입, 없으면 경고 (스크립트와 동일 계약) |
| 2 | M | `render/ecs.py` | **`aws.allowed_roles` 침묵 드롭** — `allowed_iam_roles`/`allowed_sts_regions` 변수가 env에 있는데 tfvars로 출력 안 함 → gateway.yaml에 선언해도 ECS 배포에서 gateway-cli(STS) 로그인이 fail-closed | 두 tfvar 출력 + roles 비어 있으면 feature_notes 경고(compose와 동일 규약) |
| 3 | M | `doctor.py` | **cidr drift 침묵 통과** — `cfg.allowed_cidrs and cidr and …` 조건이라 live에 어노테이션이 없으면(ALB 전체 개방) 선언된 제한과 다른데도 OK 보고 | live 비어있음도 drift로 보고 "(미설정 — 전체 개방)" |
| 4 | M | `doctor.py` | **migration Job 오탐** — chart 기본 `hookDeletePolicy: before-hook-creation,hook-succeeded`는 성공 Job을 지워서 건강한 배포에서도 항상 "Job 못 찾음" WARN | Job 없으면 `helm history` 마지막 revision status로 대체 확인(deployed→OK, failed→HIGH) |
| 5 | N | `ecs_apply.py` | ECR 로그인 `shell=True` — registry_host(terraform output)가 셸 문자열에 삽입 | subprocess 2개로 분리(비밀번호는 stdin만) + timeout/FileNotFoundError 처리 |
| 6 | N | `eks_apply.py` | `--plan`의 `--dry-run`이 client-side — 훅·기존 리소스·라이브 diff를 안 보여줘 미리보기가 부정확 | helm ≥3.13이면 `--dry-run=server` (버전 파싱으로 minor 판별, helm4 포함) |
| 7 | N | `capture.py` | `describe-services`에 하드코딩 `"admin-api"` — service_names 맵이 실제 이름을 들고 있는데 미사용 → 다른 명명 배포에서 조용히 mock 추정 | `name_map.get("admin-api", "admin-api")` |
| 8 | N | `schema.py` | `network.mode=private`이 compose에서 침묵 무시 — Caddy가 호스트 포트 그대로 개방 | warnings에 추가(호스트 방화벽/allowed_cidrs 안내) |
| 9 | N | `capture.py`, `cli.py` | 낡은 docstring — "eks — 미구현"(실제 구현됨), CLI 명령 목록에 apply/capture/client-values 누락 | 갱신 |

## 검증

- `pytest deploy/tests -q` — **120 passed** (+9: SES IRSA 3, dry-run flag 3, allowed_roles 2, compose-private 1)
- eks terraform outputs가 `dynamic_set_args`가 읽는 키와 전부 일치 확인 (`application_db_endpoint`, `elasticache_endpoint`, `*_role_arn`, `cognito_*`, `body_log_*`)
- ecs env `variables.tf`에 render 출력 변수 전부 존재 확인 + `allowed_iam_roles` 변수 존재·fail-closed 설명 문구 확인
- chart migration Job `hookDeletePolicy: before-hook-creation,hook-succeeded` 확인 → Job 부재=정상임을 실 차트로 검증
- `.gitignore`가 `deployment/gen/`·`deployment/gateway.yaml` 커버 — 캡처본(0600)+.env 시크릿 유출 경로 없음
- `write_env_file` O_CREAT 0600+fchmod(TOCTOU fix), `_confirm_destroy` 환경명 타이핑, `--yes`/isatty 가드, `--atomic`/`--rollback-on-failure` 버전 감지 — 기존 경화 확인

## 판정 보류(메모)

- `doctor` 서브파서의 `--force` 플래그는 무인자 dead flag — 제거하면 기존 호출 스크립트가 깨지므로 유지(미사용 명시 안 함)
- eks `apply`는 명시 `--context` 없으면 현재 kubeconfig context — 확인 게이트+plan으로 완충. env↔context 바인딩은 미구현(설계 한계)
- `helm upgrade`는 이전 release의 수동 `--set` 잔여값을 차트 기본값으로 reset — 이 도구가 관리하는 키는 전부 재공급되므로 선언적 모델상 정상이나, 도구 밖 --set 관례를 쓰던 값은 떨어질 수 있음
- `capture_eks`의 ElastiCache members 매칭 방향(`redis_ep in members`)은 느슨한 폴백 — PrimaryEndpoint 매칭이 주 경로라 실해 영향 없음
- eks의 `oidc.client_secret`은 렌더 안 됨(chart가 secretKeyRef 미지원) — notes로 안내됨
- ECS `allowed_aws_accounts`는 모듈이 caller account로 폴백 — env에 변수가 없어 선언 통과 경로 없음(현재 계약상 동일 계정 전제)

---

## 후속 — NDS 배포 마법사 시나리오 커버리지 (같은 날 추가 작업)

`docs/us-llm-gateway` ops 가이드의 운영 시나리오가 `deploy` 경로에서 재현 가능한지 대조해
부족분을 채웠다. 목표: gateway.yaml + configure 만으로 아래 시나리오 전부 커버.

| 시나리오 | 이전 상태 | 변경 |
|---|---|---|
| admin ALB 만 internal | `network.mode=private` 는 3 Ingress 전부 internal → gateway 까지 끊김 | `network.admin_internal` 추가 → `ingress.adminUi/adminApi.annotations` 에만 `scheme=internal` (차트 per-ingress 맵이 공통값을 덮어씀) |
| 도메인 없이 배포 → 도메인 추가 | domain=none 2-phase 만 있고 도메인 도입 시 nextauthUrl 미갱신 → OIDC 콜백 어긋남 | `domain.name` 렌더 시 `adminUi.nextauthUrl=https://admin.<domain>` |
| notification 채널 변경 | 스키마에 `internal_api` 없음, eks SES IRSA 는 수동 terraform | provider `internal_api` 추가(차트 `email.internalApi` 블록, compose `EMAIL_API_URL`, ecs `email_api_url` 변수+taskdefs 배선); `smtp_credentials_secret` 필드. SES 전환 시 `ensure_infra` 가 `module.irsa` targeted apply 로 worker IRSA 자동 생성 |
| 토큰 TTL 변경 | `adminApi.oidc.vkTtlHours` 값 통로 없음 | `oidc.vk_ttl_hours` → `adminApi.oidc.vkTtlHours` (ops 8-Z 와 같은 키 — env 2중생성 함정 회피) |
| 본문 로깅 활성화 | 플래그만 있고 인프라(Firehose/S3/IRSA)는 수동 apply | `eks_apply.ensure_infra`: output 누락 시 `terraform apply -var-file=<gen tfvars> -target=module.body_logging -target=module.irsa` 자동 실행 (ops 8-V 와 같은 계약). plan 은 적용 예고만 하고 실행 안 함 |
| EKS 클러스터 업그레이드 | 범위 밖(설계상 terraform env 소유) | 변경 없음 — doctor 의 tfvars↔live skew 감지로 커버 |
| capture 정합성 | provider=internal_api/smtp 필드·scheme·vkTtlHours 미수집 → 채택본이 실배포를 덜 표현 | live/values scheme 으로 `network.mode`/`admin_internal` 판별, smtp/internal_api 전체 필드, `oidc.vk_ttl_hours` 캡처 |
| wizard(configure) | 신규 필드 미질문 + configure 재실행 시 침묵 삭제 위험 | internal_api 선택지+URL/발신 프롬프트, eks 시 admin_internal confirm, vk_ttl 프롬프트, smtp_credentials_secret/vk_ttl/admin_internal 보존 |
| eks parity | install-eks.sh 는 `database.external.name` 을 --set 하는데 도구가 누락 | `aurora_database_name` output(없으면 차트 기본 'gateway')으로 주입 |

### 검증

- `pytest deploy/tests -q` — **129 passed** (+9: ensure_infra 5, plan 비변경성 1, capture 3; configure 보존 테스트 확장)
- `terraform fmt -check -recursive` clean
- 차트 계약 실 대조: `notificationWorker.email.internalApi.*`, `ingress.adminUi/adminApi.annotations`, `adminApi.oidc.vkTtlHours`, `email.smtp.credentialsSecretName` 전부 템플릿에 존재
- eks env(llm-gateway-dev/prod) `module.body_logging`/`module.irsa`/`enable_notification_ses`·outputs(`body_log_*`, `notification_worker_ses_role_arn`) 일치 확인
- ecs 모듈 `email_api_url` 변수 → notification-worker `EMAIL_API_URL` env merge 확인(worker config.py 계약과 동일)

### 추가 — web_search 자동 배선 (install-guide §5-1/§5-2)

`features.web_search=true` 만으로 `deploy apply` 가 AgentCore WebSearch
Gateway 를 자동 프로비저닝하고 URL 을 배선한다 — 수동 두 단계
(`provision_agentcore_websearch.py deploy` → `set-websearch-url.sh`) 제거.

- 신규 `websearch.py`: status(읽기) → 없으면 deploy(멱등) → `/mcp` 정규화.
  `GW_NAME=llm-gateway-websearch-<env>` 로 set-websearch-url.sh 와 같은
  규칙 — 수동 배포가 만든 gateway 를 재사용(중복 생성 방지). 실패 시 배포를
  중단하지 않고 graceful-off 경고(install-guide 와 같은 의미).
- eks: `gatewayProxy.env.AGENTCORE_GATEWAY_URL/REGION/TARGET_ID` 를 --set.
  `CALLER_ROLE_ARN` 은 terraform output 의 gateway_proxy_role_arn.
- ecs: URL 을 `extra-vars.json` 에 기록 → `agentcore_gateway_url` tfvars
  (taskdef 의 `url!=""→WEB_SEARCH_ENABLED` 계약). 선언을 끄면 잔여 URL 도 제거.
- compose: gen `.env` 에 AGENTCORE_* upsert (merge_env 가 비관리 키를
  보존하므로 다음 render 도 유지).
- teardown: 3 backend 전부 `features.web_search` 선언 시 env 규칙명의
  AgentCore Gateway 를 멱등 teardown (helm/terraform 관리 밖의 과금 리소스).
- plan 은 status 만 조회(비변경) — gateway 부재 시 "apply 시 프로비저닝" 예고.
- requirements.txt 에 boto3 추가 (프로비저너를 같은 인터프리터로 실행).

검증: pytest **141 passed** (+12 — websearch 단위 7 + eks 배선 3 + ecs 배선 2).
