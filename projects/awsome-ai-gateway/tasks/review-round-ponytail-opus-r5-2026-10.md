# Review Round — ponytail × kiro-cli `claude-opus-5.5`, R5

Reviewer: kiro-cli `claude-opus-5.5`, 검증·수정: Devin (SWE-2).
대상: R4 커밋 `76ddff96` diff + 주변 호출 코드.
판정: **NOT CONVERGED** — Medium 1 + Low 2, 전부 수정 완료.

## A. Findings & fixes

### F1 (Medium) — issuer≠Cognito 풀인 배치에서 경고가 억제되고 보강이 무의미 호출

- **What:** 비-Cognito IdP(Okta/Entra) + `COGNITO_USER_POOL_ID`(동기화용) + `OIDC_EMAIL_CLAIM=preferred_username` + `ADMIN_EMAILS` 조합이면 R4의 `oidc_cognito_client is None` 조건이 클라이언트 존재만 보고 경고를 건너뛴다 — 그런데 `admin_get_user(Username=<외부 sub>)` 는 그 풀에 그 유저가 없어 항상 미스 → ADMIN_EMAILS 영구 실패 + 경고도 사라짐.
- **Fix:** issuer 정합으로 판정 — `OIDC_ISSUER_URL.rstrip("/").endswith("/" + COGNITO_USER_POOL_ID)` 일 때만 Cognito 보강이 유효. `main.py` 경고 조건과 `auth._verified_email_for_subject` 진입 조건 둘 다 같은 기준으로(무의미 호출+ "!" 캐시 폐기물도 차단).

### F2 (Low) — synthetic 폴백이 STS CLI 유저의 user_id 를 누출

- **What:** STS CLI 유저도 `<session_name>@unknown` synthetic 을 저장(`cli_service.py:120`). `sync_user("bob")` → Cognito miss → email miss → `get_by_email("bob@unknown")` 이 STS 행을 찾음 → 비활성화는 provider 가드로 막히지만 `result.user_id` 가 STS 계정 id 로 채워져 호출자의 후속 allowed_clients/models 정리가 엉뚱한 계정에 적용.
- **Fix:** 폴백 매칭은 `provider == OIDC_PROVIDER_NAME` 일 때만 인정 — 비-OIDC 는 확인 호출 없이 즉시 no-op.
- **Test:** `test_sync_user_synthetic_fallback_ignores_sts_user`.

### F3 (Low) — "풀 잔존 스킵"과 "DB 부재 no-op"이 호출자 입장에서 구분 불가

- **Fix:** still-in-pool 스킵 시 `result.errors` 에 명시 문구 추가 — 거부된 offboarding 식별 가능.

## B. kiro 정합 확인(수정 불요)

- `list_users` Filter 문법(`sub = "…"`) 정확, 경로 파라미터 인젝션은 fail-closed 방향.
- synthetic `<sub>@unknown` 과 sub 폴백은 항상 같은 사람을 가리킴(유일한 다른 `@unknown` 생산자가 STS — F2로 차단).
- 인프로세스 캐시: 프로세스당 1개 dict, 키는 서명 토큰의 활성 DB 유저 sub 라 증가 bounded, clear() 후 부하는 Redis 수준. 동시 miss 시 Cognito 중복호출 최대 1회.
- `import asyncio` 모듈화로 재배치 — 모든 `asyncio.to_thread` 호출 정합.
- R1–R3 계약(defer/drain/lock/enrichment 조건) 전부 무훼손.

## Validation

- admin-api `pytest tests/` — **928 passed, 50 skipped** (신규 회귀 1건, 기존 테스트 errors 단정 추가)

## Deployment

admin-api 이미지 `git SHA` 태그 → helm upgrade `migration.enabled=true` → rollout + smoke.
