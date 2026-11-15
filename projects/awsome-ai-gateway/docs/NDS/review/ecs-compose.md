# 리뷰: ECS(배포 보류)·compose 경로

**범위**: `deploy/render/ecs.py`·`deploy/ecs_apply.py`,
`deploy/render/compose.py`·`deploy/render/common.py`, cli 의 compose/
ecs apply·teardown 경로.

**판정: 조건부 수렴** — ECS 는 배포 보류(코드 검토만, 라이브 미검증),
compose 는 로컬 평가 경로로 수렴.

## ECS (배포 보류 — 공유 코드 수정분만 활성)

사용자 지시로 ECS 롤아웃은 현재 제외. 다만 공유 캡처/apply 코드가
ECS 경로를 타므로 아래는 코드 검증으로 수정됐다:

- **internal ALB → private 매핑** (18차): capture 가 `network.mode` 를
  하드코딩 public 으로 두어 채택 apply 가 internal ALB 를 공개로
  재생성하던 destructive 경로.
- **사이드카 오염** (18차): `containerDefinitions[0]` 고정 → 서비스명
  컨테이너 우선 — 이미지·env 가 otel/firelens 에 오염.
- **gateway-proxy 대표 태그** (18차): 단일 `images.tag` 스키마에서
  서비스별 태그 불일치는 노트로 표면.
- **scalable-targets min/max 캡처** (18차): 수동 상한 유실 방지.
- **describe-services.failures 검사** (18차): 서비스 조회 실패를
  조용히 넘기지 않음.
- **terraform init -reconfigure** (19차): 공유 env dir 의 캐시 backend
  가 다른 env 에 붙는 문제 — doctor/teardown 과 같은 보호.
- **backend key/env 불일치 경고** (20차): `ecs/<env>/terraform.tfstate`.
- **기본 lock table 미참조** (21차): `ensure_tfstate(ecs)`가 만드는 기본
  DynamoDB table 을 backend.hcl 이 미선언 시 생략해 state unlock →
  `render_backend_hcl` 이 기본값(`llm-gateway-vanilla-tflock`)을 방출.
- **teardown**: destroy 전 outputs 실측 chat_agent_name 전달,
  `-reconfigure` init, `tf_outputs` SystemExit 가드.

미검증: 실제 ECS 배포·서비스 안정 대기·2-phase apply 는 라이브 필요.

## compose (로컬 평가 경로)

- **dev_login fail-closed** (19차): eks 와 동일하게 `dev_login and not
  oidc → true else false` — oidc 없는 평가 호스트가 서명 없는 dev.ADMIN
  세션을 발급하던 구멍 + 로그인 경로 없음 경고(feature_notes/schema).
- **ALLOWED_AWS_ACCOUNTS/IAM_ROLES 주입** (R24 계약): fail-closed —
  비면 CLI 로그인 전량 403 + 노트.
- **`merge_env`/secret 보존**: 기존 `.env` 값은 보존, 빈 키만 생성,
  0600 쓰기 — 재렌더 시 랜덤 시크릿이 회전하지 않는다.
- **observability**: OTel/Prometheus/Loki/Tempo/Grafana 스택,
  포트 전부 127.0.0.1 바인드.
- **body_logging/pricing_lambda 미지원 명시**: feature_notes 로 침묵
  드롭 금지 — 지원하지 않는 플래그는 "미지원"으로 명시한다.

## 회귀 테스트

## 23차 적용 (독립 리뷰 — 추가 수정)

- **.env declared-off 수렴**(HIGH): merge_env 가 desired 에 없는 키를
  조용히 보존해 oidc→dev_login 전환 시 stale OIDC_* 가 dev 토큰을
  거부(401 잠금), web_search off 후 기능 계속 켜짐+AgentCore Gateway
  과금 리소스 잔여. 관리 키 드롭+보고, GENERATED_KEYS·운영자 키 보존,
  compose apply 에 web_search off-수렴 teardown 연결.
- **NEXTAUTH_URL mode 게이트**: compose mode=none 은 caddy 포트 라우팅만
  열어 `http(s)://admin.<name>` 이 죽은 주소 — mode!=none 일 때만 기록.
- **scheduler migration 게이트**: docker-compose.yml 의 scheduler 가
  migration 완료를 기다리지 않아 crash-loop 가능 — admin-api 와 동일
  게이트 추가.
- **litellm_pricing_lambda extra-vars 영속화**(선행 커밋): ensure 성공
  값이 in-memory 에만 있어 다음 apply 가 var 를 잃음 — `_save_extra_vars`
  호출 추가.

## 잔여 리스크

- ECS 경로는 배포 보류라 코드 검토만 — 실 서비스 안정 대기/롤링은
  재개 시 라이브 검증 필수.
- compose 는 프로덕션 배포 대상이 아님(평가용) — 운영 모니터링/
  백업 계약은 문서 영역.

## 29차 적용 (추적키 생명주기·module 실측 스냅샷)

- **추적키 조기 저장**(HIGH): ws/bi/pricing 추적키를 teardown 전에
  pop+저장 — 그 사이 abort(SystemExit/ctrl-C)가 살아 있는 유료 리소스의
  마지막 추적 증거를 영구 소실 → render 는 메모리 pop 상태로 하고 저장은
  off-teardown 수렴(실패 복원 포함) 후 단 한 번.
- **human-declared ARN 유실**(HIGH): 사람이 enable_chat_agent 를 tfvars 로
  소유한 env 에서 ARN 을 지우면 사람 소유 Runtime 이 살아 있는 채 /chat 이
  영구 503 — `_human_declared_chat_agent` 를 pop 전에 평가해 주입 유지.
- **`module_alive` 사멸**(MEDIUM): bootstrap targeted apply 가 outputs 를
  `enable_chat_agent=false` 로 재저장해 apply 후 읽은 module_alive 가 항상
  false — ARN 미기록 Runtime 의 teardown 신호가 죽음. init(백엔드 정렬) 후
  apply 전 `pre_module_name` 스냅샷으로 대체.

