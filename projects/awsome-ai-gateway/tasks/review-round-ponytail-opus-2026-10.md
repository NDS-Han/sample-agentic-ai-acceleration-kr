# Security/Consistency Review Round — ponytail × kiro-cli `claude-opus-5.5`

2026-10-13, 브랜치 `nds/dev`. 리뷰 대상: 커밋 `325dfb0e`(kiro 2차 잔여분) + `2db0987a`(migration hook 수정) + 직전 ponytail 라운드(`2a077a2b`) 이후 미커밋 상태.

Reviewers: kiro-cli `claude-opus-5.5`(settings 기본 모델 지정 경로 — `--model` flag 는 이 빌드에서 무효) + SWE-2 자체 감사. 모든 kiro 발견은 코드로 직접 검증 후 확정/기각했다.

Verdict: **9건 중 8건 수정 완료 + 1건 자체 발견 추가 수정, 1건은 accepted residual.**

## A. Findings & fixes

### F1 (High) — `ADMIN_EMAILS` 가 미검증 이메일에도 매칭

- **What:** `derive_role` 가 `email in ADMIN_EMAILS` 만 보고 ADMIN 부여. 이메일 클레임은 IdP 설정에 따라 사용자가 쓸 수 있는 값이다.
- **Problem:** 라이브 `ADMIN_EMAILS=seunghwan1228@nongshim.co.kr` 설정돼 있어 실제 경로 — Cognito 에서 미검증 이메일로 유저 생성(AdminCreateUser 기본값) → 해당 주소가 ADMIN 목록과 일치하면 주장만으로 권한상승.
- **Fix:** `derive_role(email, groups, *, email_verified)` — 매칭 전제로 verified 요구. 클레임 `email_verified` 전달(auth.py), Cognito 풀 속성 전달(cognito_sync·oidc_service), DB 이메일 폴백은 연결 시 verified 게이트 통과분이라 신뢰. `cognito_sync._derive_role` 는 복제본이던 것을 `core.oidc_identity.derive_role` 위임으로 통일.
- **Verified live:** pool 의 admin 유저 `email_verified=true` — lockout 없음 확인.

### F2 (High) — grace 중인 service token 의 재rotate 연쇄

- **What:** `rotate` 는 `revoked_at <= now` 만 거부했다 — 미래 시각(grace 유효)은 통과.
- **Problem:** 토큰 A rotate → B 발급 + A 에 grace 24h → A 로 다시 rotate → C 발급 + … 유출 토큰이 신 토큰을 무한 연쇄 발급. 직전 라운드의 `min(grace)` 상한은 종료 시각만 묶고 연쇄 발급 자체는 못 막았다.
- **Fix:** `get_by_id(for_update=True)` 행 락 + `revoked_at is not None` 이면 무조건 거부(토큰당 회전 1회). 회귀 테스트 2건.

### F3 (High) — `sync_all` advisory lock 이 실제로는 영원히 무효

- **What:** `isinstance(session.get_bind(), AsyncConnection)` 분기 + else 의 `bind.connect()` await.
- **Problem:** `AsyncSession.get_bind()` 는 `sync_session.get_bind` 프록시라 **항상 sync Engine/Connection** 을 돌려준다(SQLAlchemy 2.0.49 소스 확인). sync Engine → `connect()` 는 sync Connection → `await` 가 TypeError → `advisory_lock_unavailable` 예외 경로 → **락 없이 진행**. 동시 sync_all 시 reconcile 단계가 서로의 seen 집합 모른 채 전원 비활성화 가능.
- **Fix:** sync `Connection` 바인딩 → 세션-레벨 `pg_try_advisory_lock`, sync `Engine` 바인딩 → `AsyncEngine(sync_engine)` 래퍼(같은 풀 참조, dispose 금지)의 전용 커넥션에서 `pg_try_advisory_xact_lock`. 신규 테스트 3건(획득/거부/커넥션 바인딩).

### F4 (High) — 지연 Redis 쓰기가 savepoint 롤백을 살아남음

- **What:** `defer_redis_write_until_commit` 의 `after_rollback` 핸들러는 nested 롤백이면 early-return.
- **Problem:** per-user savepoint 안에서 등록된 DEL/SET 이 그 savepoint 롤백 후에도 외부 커밋 때 실행 → **DB ACTIVE·Redis 삭제** 정합 붕괴(VK 폐기 실패한 유저의 키가 지워짐 등).
- **Fix:** 등록 시점의 SessionTransaction(`get_nested_transaction() or get_transaction()`)을 캡처, `after_soft_rollback` 에서 롤백된 트랜잭션의 자신~자손이면 쓰기 폐기. `SessionTransaction.nested` 는 자식 링크가 아니라 flag 라 `get_nested_transaction()` 사용(실측). 테스트 2건(폐기/무관 savepoint 생존).

### F5 (Medium) — 동시 최초 로그인의 사용자 생성 경합

- **What:** `_upsert_user` 의 check-then-insert — 같은 sub 로 두 exchange 가 나란히 오면 패자는 sso_subject UNIQUE 에 IntegrityError → 500.
- **Fix:** 생성을 `begin_nested` savepoint 로 감싸고 IntegrityError 시 `get_by_sso_subject` 재조회 → 승자 행으로 기존사용자 경로. 재조회도 None 이면 전파. 테스트 2건.

### F6 (Medium) — `preferred_username` 이 verified email 로 통과

- **What:** email 클레임 없을 때 `preferred_username` 폴백, 그 뒤 Cognito enrich 의 `pool_verified` 를 무조건 승계.
- **Problem:** `preferred_username` 은 사용자 자유기재 — 다른 계정 주소를 적으면 연결 게이트 통과. `pool_verified` 는 풀의 `email` 속성에 대한 값이라 `preferred_username` 과 무관.
- **Fix:** preferred_username 경로는 `email_verified=False` 고정; enrich 승계는 `pool_email == email` 일치 시에만.

### F7 (Medium) — Cognito disable 된 유저의 VK 미폐기

- **What:** `_upsert_one_user` 는 `is_active=False` 만 쓰고 폐기는 "풀에서 사라진 유저" 경로에만 있었다.
- **Problem:** disable 된 유저의 Redis-생존 VK 가 재활성화 창까지 유효.
- **Fix:** True→False 전환 시 같은 트랜잭션에서 `revoke_keys_for_users`, 해시는 `revoked_out` 에 수집 → **savepoint 성공 후에만** `result.pending_key_revocations` 로 이관 → 각 commit 직후 `_flush_pending_revocations`. sync_all/sync_user/sync_group 3경로 동일.

### F8 (Medium, 자체 발견) — `pending_key_revocations` 가 savepoint 롤백을 살아남음

- **What:** F7 구현 중 발견 — `result.pending_*` 는 트랜잭션을 모르는 파이썬 리스트.
- **Problem:** savepoint 안에서 수집된 폐기 해시가 롤백 후에도 남아 DB ACTIVE 키를 DEL — F4 와 같은 부류를 리스트 수집에서 재현할 뻔했다.
- **Fix:** 수집은 호출부가 준 로컬 `revoked_out` 에만 → savepoint 정상 종료 후 `result.pending` 으로 이관. `revoked_out` 은 필수 인자(None 이면 폐기가 조용히 스킵되는 함정 차단).

## B. Accepted residual

### R1 — user budget 캐시의 잔여 stale 재캐시 창 (F9)

DEL-only 전환 후에도 창이 남는다: 게이트웨이가 miss 로 구 행 조회 → admin 커밋 → 지연 DEL → 게이트웨이가 구 값을 SET(TTL 300s). 완전 봉합엔 generation/version 카운터 + 게이트웨이측 CAS Lua 가 필요해 **gateway-proxy 까지 건드리는 별도 작업** — 이번 범위 밖으로 남긴다. DEL-only 로 창은 "stale SET 이 도착" 에서 "DB 조회 중에만" 로 좁아졌고, app_clients 유실(보안 우회)은 해소됨.

## C. Validation

- `pytest tests/` — **918 passed, 50 skipped** (신규 회귀 12건: 락 3, defer-savepoint 2, 회전 2, 최초로그인 경합 2, disable-VK 1, admin-email 게이트 2)
- `pytest deployment/tui/tests/` — 68 passed, 1 skipped
- `py_compile` 변경 모듈 전부 통과
- 라이브 검증: pool admin `email_verified=true`(F1 게이트 통과 확인), `OIDC_AUDIENCE` 는 rev178 에서 적용됨

## D. Deployment

재빌드 대상: **admin-api**(auth·oidc·cognito_sync·service_token·budget_cache·repo) — scheduler 는 동일 이미지 공유. migration 변경 없음(0045 이미 적용됨). 배포 후 스모크 + 헬름 rev 기록.
