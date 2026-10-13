# Review Round — ponytail × kiro-cli `claude-opus-5.5`, R3

Reviewer: kiro-cli `claude-opus-5.5` (settings 기반 기본 모델 지정, `--trust-tools=fs_read,grep` 비대화), 검증·수정: Devin (SWE-2).
대상: R2 커밋 `e8525bd5` diff + 그 수정이 **호출하는/호출되는 주변 코드**까지.
판정: **Not converged → Ship** — 5건 중 5건 실물 확인·수정.

## A. Findings & fixes

### F1 (High→config-conditional) — email-less 토큰에서 ADMIN_EMAILS 관리자 강등

- **What:** admin-ui 쿠키가 access_token(`OIDC_COOKIE_TOKEN=access_token` 또는 id_token 부재 폴백, `admin-ui/.../callback/route.ts:328-331`)이면 `email` 클레임이 없다. `auth.py`의 DB-이메일 폴백은 R2에서 폐기됨 → `derive_role("")` = DEVELOPER, `admin_mapping_configured()` 분기로 DB ADMIN 도 강등.
- **Why real:** 라이브는 `ADMIN_EMAILS` 설정됨. `/v1/auth/exchange` 는 Cognito AdminGetUser 로 이메일을 보강해 ADMIN + DB 기록하지만 admin-ui 는 그 DB ADMIN 을 매 요청 무효화 → VK=ADMIN 인데 UI=403.
- **Fix:** `auth._verified_email_for_subject` — 검증된 클레임 이메일이 없을 때만 `request.app.state.oidc_service._email_attrs_from_cognito(sub)` 호출, `oidc:emailver:{sub}` Redis 캐시 5분("!"=미해결). sub 가 신원을 증명하므로 풀 속성 이메일은 신뢰 가능. `ADMIN_EMAILS` 비어 있으면 호출 자체를 안 해 라이브(id_token 경로) 오버헤드 0.
- **Tests:** `test_access_token_admin_email_enriched_via_cognito_pool_attrs`, `test_access_token_unverified_pool_email_still_denied`.

### F2 (Medium) — `OIDC_EMAIL_CLAIM != "email"` 배치에서 정책 조용히 사망

- **What:** `email_verified` 클레임은 `"email"` 속성만 증명한다. 비-Cognito IdP(Keycloak/Entra)에서 `OIDC_EMAIL_CLAIM=preferred_username` 등이면 `_email_attrs_from_cognito` 도 없어 ADMIN_EMAILS 영구 실패 + 신규 유저 전원 synthetic + 이메일 연결 항상 거부 — 로그도 없음.
- **Fix:** startup 에 `ADMIN_EMAILS` ∧ `OIDC_EMAIL_CLAIM != "email"` 조합을 ERROR 로그(`oidc.admin_emails_unusable`). fail-fast 는 과하다 — 그 배치가 의도일 수 있으므로 관측 가능성만.

### F3 (Medium, security) — synthetic 이메일 사용자의 offboarding 미스

- **What:** `sync_user(username)` → `_deactivate_missing_user` 는 `get_by_email(username)` 만 조회. 미검증 사용자는 `<sub>@unknown` synthetic 저장이라 매칭 불가 → 비활성화 no-op + `result.user_id=None` → 후속 allowed_clients/models 정리도 스킵, ACTIVE VK 는 TTL 까지 인증.
- **Fix:** email miss 시 `get_by_sso_subject(username)`(이 풀들에서 Cognito Username=sub) → `get_by_email(f"{username}@unknown")` 순차 폴백.
- **Test:** `test_sync_user_deactivates_synthetic_email_user_by_sub_or_suffix`.

### F4 (Medium) — synthetic 이메일이 SES 수신자로 직행

- **What:** `notification-worker/services/recipient_resolver.py` 의 AFFECTED_USER/TEAM_LEADER/ADMIN 3 경로가 `user.email` 을 그대로 SES `Destination` 으로 — `@unknown` 은 거부/바운스(계정 bounce rate 오염 포함).
- **Fix:** resolver 의 중앙 dedup 루프에서 `@unknown` 접미사 drop + WARN(`recipient_synthetic_email_dropped`). 수신처가 그것뿐이면 빈 목록(기존 "수신자 없음" 경로로 처리됨).

### F5 (Medium, perf) — 미검증 유저가 매 sync 마다 upsert 를 탐

- **What:** `_needs_update` 가 `snap["email"]`(synthetic)과 풀 이메일을 비교 — 절대 일치 안 해 "skip unchanged" 게이트가 미검증 유저 전원에게 무력화(savepoint + get_by_sso_subject 왕복 × 미검증 유저 × 매 sync).
- **Fix:** `_needs_update(..., email_verified)` — update 분기(`email_verified` 게이트)와 1:1 동일 조건.

## B. 자체 발견·검증 (SWE-2 병행)

- **`_ensure_team_id` 캐시 경로** — sync_all/sync_group 이 실제로 타는 쪽이 savepoint 없이 부서/팀 생성 → 동시 sync 의 IntegrityError 가 per-user savepoint 밖에서 외부 트랜잭션을 죽임. savepoint + 승자 재조회 + 캐시 갱신으로 M2 와 같은 패턴.
- **신규 생성 경로의 미검증 이메일 저장** — sync 생성이 `email` 그대로 저장 → "DB 이메일=신뢰" 폴백과 결합 시 ADMIN 승격 회귀. 생성도 `email if verified else <sub>@unknown` (R2 커밋에 포함됨).

## C. kiro 가 확인한 정합 (재수정 불요)

- advisory-lock fail-close 가 postgres 전용 분기로 정확(dialect.name="postgresql", pgbouncer transaction mode OK).
- `_verified_email_for_subject` 없던 시절의 `CurrentUser.email` 우선순위, pending drain 타이밍, FOR UPDATE 회전 — 정합.

## D. Accepted residuals

- Redis 캐시된 검증 이메일이 최대 5분 stale — Cognito 에서 이메일 검증 해제 직후 최대 5분간 ADMIN 매칭 유지 가능. ADMIN_EMAILS 멤버십 자체는 배포가 바꾸는 값이라 수용.
- `CurrentUser.email` 이 synthetic 일 수 있음(표시용, 감사 로그에 `sub@unknown` 노출 — 의도된 가시성).

## Validation

- admin-api `pytest tests/` — **926 passed, 50 skipped** (신규 회귀 4건)
- notification-worker `pytest tests/unit/test_recipient_resolver.py` — 9 passed
- TUI 68 passed (이전 라운드)

## Deployment

커밋 → admin-api 이미지 재빌드(git SHA 태그) → helm upgrade `migration.enabled=true` → rollout + 스모크.
