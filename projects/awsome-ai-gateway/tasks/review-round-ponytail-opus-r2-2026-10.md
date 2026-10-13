# Security Review Round 2 — ponytail × kiro-cli `claude-opus-5.5`

2026-10-13, 브랜치 `nds/dev`. 리뷰 대상: 커밋 `875fee20`(1차 라운드 수정분) — 수정이 새 결함을 만들지 않았는지 재검증.

Reviewers: kiro-cli `claude-opus-5.5`(`~/.kiro/settings/cli.json` `chat.defaultModel` 경로 — `--model` flag 는 2.26.0 빌드에서 무효) + SWE-2 자체 감사. kiro 발견은 전부 코드/실측으로 검증.

Verdict: **kiro 4건 중 3건 수정(F1·F2·F3), 1건은 doc-note 수용(F4). 자체 발견 2건 추가 수정(S1·S2).**

## A. kiro 발견 — 검증·수정

### F1 (High) — 레거시 DB 이메일 폴백 경유 ADMIN 승격

- **Trigger:** 수정 이전에는 `oidc_service._upsert_user` 가 미검증 클레임 이메일을 그대로 저장했다. 그렇게 오염된 행의 소유자가 Cognito 에서 자기 email 속성을 지우면 → 토큰에 email 클레임 없음 → `auth.py` 가 DB 이메일 폴백을 "연결 시 verified 통과분"으로 신뢰(`email_verified=bool(user.email)`) → 저장된 `admin@corp` 가 ADMIN_EMAILS 매칭 → ADMIN.
- **Fix:** DB 이메일을 ADMIN 매칭 폴백으로 **아예 쓰지 않는다** — `derive_role(parsed.email or "", groups, email_verified=claim_verified)`. 매칭은 이번 토큰의 검증된 클레임만. 그룹 기반 ADMIN 은 무관.
- **검증:** 기존 테스트 `test_db_email_fallback_counts_as_trusted_for_admin_emails` 를 반전(`..._does_not_grant_admin`) — 게이트가 뚫리면 즉시 실패한다.
- **Trade-off:** 이메일 클레임이 없는 토큰으로 드는 어드민은 ADMIN_EMAILS 경로로는 승격 안 됨 — 그룹(`ADMIN_GROUPS`)으로 부여하라. 라이브 admin 은 id_token+verified email 이라 무영향.

### F2 (Medium) — `email_verified` 는 `email` **속성**의 검증값

- **Trigger:** `OIDC_EMAIL_CLAIM` 을 `preferred_username`/`upn` 등으로 재매핑한 IdP(Keycloak·Azure 계열)에서는 `email_verified` 클레임이 매핑된 클레임을 증명하지 않는다 — 편집 가능한 username 을 `admin@corp` 로 바꾸면 ADMIN.
- **Fix:** `auth.py`·`oidc_service.py` 양쪽에서 `settings.OIDC_EMAIL_CLAIM == "email"` 일 때만 `email_verified` 승계, 아니면 `False`.

### F3 (Medium) — sync lock 이 postgres 장애에 fail-open

- **Trigger:** `except Exception` 경로는 모킹/비-PG 환경을 위한 것이었는데 **풀 고갈**(`AsyncEngine.connect()` → pool_timeout 후 TimeoutError)도 삼켰다. 풀이 포화된 시점 = 동시 sync 가 몰리는 시점이라 상호배제가 가장 필요한 때에 무너진다.
- **Fix:** `bind.dialect.name == "postgresql"` 이면 `result.errors` 에 남기고 실행을 건너뛴다. 비-PG/모킹만 잠금 없이 진행. `bind` 미바인딩 NameError 경로도 함께 정리.
- **테스트:** `test_lock_failure_on_postgres_refuses_to_run_unlocked`, `test_lock_failure_on_non_postgres_proceeds` — postgres dialect 는 `create_engine("postgresql+asyncpg://")` 실제 Engine(spec mock 은 `dialect` 를 못 단다 — 인스턴스 속성이라 클래스에 없음).

### F4 (Doc) — grace 중 재rotate 거부의 재시도 함정

- 수용. rotate 커밋 후 응답(신규 raw 토큰)이 유실되면 재시도는 422 — grace 안에 관리자가 새 토큰을 발급해야 한다. 연쇄 발급 차단의 비용으로 재열지 않는다.

## B. 자체 발견 — 수정

### S1 (High) — 미검증 풀 이메일의 신규 생성 저장 → DB 폴백 승격 경로

- **Trigger:** `_upsert_one_user` 의 생성 분기는 `email=email` 무조건 저장이었다. 공격자가 ADMIN 주소를 자기 이메일로 등록한 미검증 풀 유저 → sync 가 그대로 저장 → access token(email 클레임 없음) 로그인 시 DB 폴백이 그 주소를 신뢰 → ADMIN. F1 의 폴백 폐기와 짝으로 **저장 측도 닫아야** 완결된다.
- **Fix:** 생성 시 `email if email_verified else f"{sub}@unknown"` — OIDC 경로와 같은 synthetic 계약.
- **Trade-off:** 풀에서 미검증인 유저는 admin-ui 에서 synthetic 이메일로 보인다 — Cognito Username 은 별도로 남고, 검증되면 다음 sync 에서 실주소로 채워진다.
- **테스트:** `test_sync_user_stores_synthetic_email_when_unverified` / `..._when_verified`.

### S2 (Medium) — `_ensure_team_id` 캐시 경로의 무savepoint 생성

- **Trigger:** sync_all/sync_group 이 실제로 타는 캐시 경로의 `create_department`/`create_team` 은 savepoint 없이 flush — 동시 sync 가 같은 (org,dept)/(dept,team) 을 넣으면 패자의 IntegrityError 가 **per-user savepoint 밖**에서 터져 외부 트랜잭션 전체 abort → 잔여 upsert·최종 commit 전부 실패.
- **Fix:** `begin_nested` + `get_department_by_name`/`get_team_by_dept_and_name` 승자 재조회 + 캐시 갱신 — `_ensure_team` 과 동일 패턴.

## C. kiro 가 확인한 정합 항목 (재수정 불요)

- advisory lock 해제 경로 전수(미획득 aclose / except aclose / finally aclose — 커넥션·락 잔류 없음), `AsyncEngine(sync_engine)` 가 같은 풀 참조(dispose 금지 주석과 일치).
- `pending_key_revocations` — 모든 commit 직후 drain, savepoint 성공 후에만 이관, commit 예외 시 미finalize.
- `defer_redis_write_until_commit` — 외부 등록+내부 롤백 생존 / 내부 등록+내부 롤백 폐기 / 형제 savepoint 무관 / 루트 롤백 전량 폐기 — 전부 정합(내 테스트로도 확인).
- `rotate` FOR UPDATE — 같은 세션에서 신선한 행 로드, 두 번째 동시 rotate 는 락 대기 후 `revoked_at` 보고 거부.

## D. Validation

- `pytest tests/` — **923 passed, 50 skipped** (이번 라운드 신규: 락 2, synthetic 저장 2, defer-release 1, DB-폴백 반전 1)
- `py_compile` 변경 모듈 통과

## E. Deployment

admin-api 재빌드(SHA 태그)+ helm upgrade 필요 — 본 라운드 변경은 모두 admin-api 코드.
