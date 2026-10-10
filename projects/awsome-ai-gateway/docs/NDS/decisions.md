# 설계 배경 — 왜 이 구조인가

이 배포 도구가 지금 모양인 이유를 기록합니다. 독자: 이 도구를 고치거나 확장하는 사람.

## 문제 정의

기존 배포(`docs/us-llm-gateway` + `update-scripts/` ~30개)의 근본 결함:

- **변경이 절차로 축적** — 매 업데이트가 `NN-do-something.sh`라는 일회성
  스크립트였고, 설정 파일(values.yaml/tfvars)을 regex로 누적 수정했다.
- **유일본을 수작업으로 고침** — 계정 값 때문에 git에 못 넣는 values 파일을
  스크립트들이 각자 다른 방식으로 편집 → 순서·누락 사고.
- **US 전용 가정** — 리전·계정 구조·도메인이 문서 전체에 고정.
- **dev/prod 간극** — `is_prod` 이진 분기만 있어 중간 규모가 없었다.

## 핵심 결정

### 1. gateway.yaml 이 유일한 source of truth

환경당 설정 파일 하나. 변경 = 파일 편집 + render + apply. 스키마가 잘못된
조합(compose + t1, ecs + 이미지 태그 없음 등)을 render 전에 거부한다.

### 2. "generate, don't mutate"

render는 베이스 `docker-compose.yml`을 **읽어서** 산출물을 만든다 — 원본이나
기존 산출물을 편집하지 않는다. 산출물(`deployment/gen/<env>/`)은 생성물이며
git에 들어가지 않는다(`.env` 시크릿 포함). 손으로 고친 산출물은 doctor가
drift로 보고한다.

### 3. .env는 "없는 키만 채운다"

시크릿을 재렌더 때마다 바꾸면 VK 암호화키(DEK)가 바뀌어 발급된 키가 전부
무효화된다. `common.merge_env`는 기존 값을 보존하고 비어 있는 키만 채운다 —
다른 값이면 덮지 않고 보고한다.

### 4. size_tier = deploy_target과 독립된 축

| | 결정 |
|---|---|
| deploy.target | 컴퓨트 기판 (compose/ecs/eks) — 서로 다른 제품 경로 |
| size_tier | DB/캐시 토폴로지+HA (t0~t3) — `is_prod` 이진값 대체 |

`deploy_target`을 하나의 플래그로 통합된 파이프라인에 넣지 않는 이유: IRSA·
ESO·ALB annotation 등이 EKS에 load-bearing이라 같은 키가 세 backend에서
완전히 다른 구현으로 컴파일된다 — leaky abstraction. 대신 **공통 스키마 +
backend별 렌더러**.

### 5. 기능 플래그는 3버킷

- **infra만** (body_logging의 S3/Firehose 등): backend별로 만들 수 있는 것이 다름
- **app-env** (notification provider, web_search 스위치): `.env`/task env로 전달
- **DB-seed** (routing_profiles, pricing): 마이그레이션/시드가 필요 — env만으로 부족

켠 플래그가 침묵 속에 아무것도 안 하면 안 된다 → render가 `feature_notes`로
각 플래그의 실제 효과와 누락 전제조건을 출력한다.

### 6. 모델은 `global.*` inference profile 기본

시드 마이그레이션이 이미 `global.anthropic.*`을 기본으로 둔다 — 특정 리전에
고정되지 않아 어디서 배포해도 동일. `us.`/`apac.` 프리픽스는 모델 가용성
제약일 뿐 배포 리전과 무관하다.

### 7. Caddy를 인입점으로 (compose)

- 도메인 없음: 포트별 HTTP 라우팅(8000/8080/3000)
- 도메인 있음: `gateway./admin-api./admin.` 자동 서브도메인 + Let's Encrypt 자동 TLS
- `allowed_cidrs` → `remote_ip` 매처로 403 강제 — SG가 없는 환경에서도 앱 레벨 차단

### 8. doctor = 항상 검증 가능한 진단

업데이트가 깨지는 이유는 "바뀐 것"을 아무도 안 보기 때문. doctor는 매 실행마다
산출물/시크릿/서비스 헬스/마이그레이션 head/`.env` drift를 검증한다.

### 9. ECS 경로는 helm 계약을 이식한다 — 다시 설계하지 않는다

ecs-gateway 모듈의 task-def env 는 `deployment/charts/llm-gateway` 의
commonEnv/configmap/admin-ui/deployment 계약을 1:1 로 옮긴다 (DB_URL
`ssl=` 파라미터, `rediss://:` 형식, Q50 의 Redis/RL 복원력 값,
INTERNAL_API_TOKEN, SECURE_COOKIES, CLI_DIST_DIR 등). env 가 달라지면
EKS 와 ECS 가 다른 앱이 된다 — 계약의 진원지는 helm 차트다.

### 10. 비밀번호가 들어간 URL 은 task-def 가 아니라 컨테이너가 조립한다

helm 의 `$(DB_PASSWORD)` 치환과 같은 방식: `secrets:` 가 env 로 주입하고
`sh -c` 래퍼가 DB_URL/REDIS_URL 을 만들어 exec 한다. task def JSON 과
terraform state 에 평문이 남지 않고, SM 값 회전 시 재시작만으로 반영된다.

### 11. migration 은 helm hook 과 같은 게이트 — terraform_data + run-task

서비스가 스키마 없이 먼저 뜨면 크래시루프 + circuit breaker 오작동.
`terraform_data.migration`(trigger=image_tag)이 `aws ecs run-task` 를 apply
중에 실행하고 exitCode!=0 이면 apply 가 멈춘다. 서비스는 depends_on 으로
그 완료를 기다린다 — EKS 의 pre-install/pre-upgrade hook 과 같은 순서 보장.

### 12. is_prod 이진 분기를 명시 토폴로지 변수로 대체

vpc(`nat_ha`)/aurora(`db_mode`+`safeguards`)/elasticache(`cache_mode`)에
명시 변수를 넣었다 — 기본값은 기존 environment 추론이므로 기존 EKS 환경의
plan 은 그대로다. 토폴로지(provisioned 여부)와 데이터 안전장치
(deletion protection)는 다른 축이라 분리했다 — t1 도 운영이면 safeguards 가
켜져야 한다.

### 13. domain=none 의 외부 URL 은 2-phase apply

NEXTAUTH_URL 은 task-def 생성 시점에 ALB DNS 를 모른다. 첫 apply 가
DNS 를 발견하면 `extra-vars.json` 에 기록하고 tfvars 를 다시 렌더해 두 번째
apply — 발견값이 gateway.yaml 을 오염시키지 않으면서 plan drift 에도
잡히지 않는다 (helm 의 `--set adminUi.nextauthUrl=<alb-dns>` 와 같은 결).

## 의도적으로 하지 않은 것

- **dual-render** (yaml → helm values): TF output `--set` 브릿지가 이미 단일
  사슬로 존재 — 두 시스템이 같은 키를 쓰면 drift 재발
- **`deploy_target` 플래그식 통합**: 위 4번 참조
- **site-config를 별도 레포/SM으로 분리**: tfvars가 이미 gitignore라 시급하지 않음 — 나중에
- **terragrunt/cdktf**: 기존 손글씨 HCL과 코멘트 자산을 버리는 비용 대비 이득 없음
