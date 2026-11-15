# 리뷰: gateway.yaml 스키마·검증·wizard

**범위**: `deploy/schema.py`, `deploy/cli.py` 의 `_collect_doc`/`cmd_init`/
`cmd_configure`/`_write_config`/`_set_path`, `deploy/capture.py` 의
채택 입력 경로.

**판정: 수렴** — 모든 발견이 수정·회귀 테스트로 고정됐다.

## 핵심 계약

- `gateway.yaml` 은 배포의 declarative source of truth — `deploy
  bootstrap`(마법사)가 최초 생성, 이후 `deploy configure` 또는 직접 편집
  후 `render → apply`.
- 스키마 버전 `1`, `from_dict()` → dataclass → `validate()`+`warnings()`.
- 알 수 없는 키는 오류, 잘못된 타입은 침묵 순회 대신 명시 오류.

## 발견·수정된 결함 (라운드별)

### HIGH — configure wizard 의 선언 소실 (19차)

`_collect_doc` 이 doc 을 재구성하면서 질문하지 않는 키를 통째로 버렸다:

- `aws.allowed_roles` — 비면 admin-api 가 로그인 전량 403(fail-closed).
  configure 한 번에 다음 apply 가 잠금 상태를 만들던 경로 → `aws.*` 전체 보존.
- `deploy.capacity_tier` — 사라지면 다음 apply 가 프리셋으로 조용히 축소
  → `deploy.*` 미질문 키 전부 보존 (`sizing` 만 살리던 것을 확장).
- `domain.zone_id` — eks 도 ACM 검증/Route53 레코드 자동화에 필요한데
  ecs 전용 질문이라 eks 경로는 `""` 로 덮임 → 두 target 모두 질문.
- `--config gateway.captured-*.yaml` 지정 시 기본값 소스가 지명 파일이
  아니라 DEFAULT_CONFIG(다른 env)였음 → 소스/출력 경로 분리.

### 크래시·오파싱 (18~20차)

- `from_dict` malformed 입력: `null`/스칼라/리스트-자리-매핑이
  `AttributeError`/`TypeError`/`"None"` 문자열/문자-단위-분해로 나오던 경로
  → `_require_mapping`/`_str_items`/`_safe_bool`/`_safe_int`/`_safe_float`
  로 명시 오류화.
- `validate()` 가 잘못된 `size_tier`/sizing 값으로 `preset()`·예산 계산을
  호출해 2차 예외 → 유효성 가드 우선.
- `_set_path` 중간 경로 비-dict 가드; `existing` 비-mapping·리프 스칼라
  (`_gd`/`_gl` 코어션 헬퍼) 방어.
- 중복 `_REGION_RE` 정리, `version` 타입, `account_id` 12자리·`zone_id`/
  `domain.name` 형식 검증 추가.

### 의미 보존 계약

- `features.body_logging` 은 삼태 — `None`(미선언)은 managed overlay 의
  소유 경계라 보존, 명시 `false` 만 끄기로 의미가 다르다.
- `network.allowed_cidrs` 빈 리스트 ≠ 미선언 — 빈 리스트는 ALB ingress
  규칙 전량 삭제로 해석될 수 있어 명시 구분.
- `oidc.audience` 미기입 시 `client_id` 기본 — JWT aud 검증 비활성화 방지.
- `oidc.admin_emails`/`groups_claim`/`reject_unmatched_groups` 등 신규 키도
  configure 보존 대상(캐치올 패턴으로 미래 키 자동 보존).

## 회귀 테스트

`test_schema.py` (malformed 타입/검증 가드/형식), `test_capture.py`
`test_configure_preserves_unprompted_fields`+`test_collect_doc_survives_
scalar_leaf_sections`+`test_configure_uses_named_captured_file_as_source`.

## 잔여 리스크

- `deploy configure` 가 캡처본 다수 존재 시 `captured[0]` 을 기본값으로
  선택 — 목록을 출력하고 `--config` 안내를 추가했으나 자동 선택 자체는
  사용자 확인에 의존 (LOW).

## 28차 적용

- **`aws.allowed_roles` 콤마 거부**(LOW): 항목 안 콤마가 ALLOWED_IAM_ROLES
  의 ",".join() 에서 두 role 로 갈라져 허가 목록이 조용히 확장 —
  item_re(role 이름/ARN 문자셋 + 와일드카드 *, 콤마 제외)로 거부.
