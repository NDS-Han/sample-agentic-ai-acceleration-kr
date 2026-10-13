# Review Round — ponytail × kiro-cli `claude-opus-5.5`, R4

Reviewer: kiro-cli `claude-opus-5.5`, 검증·수정: Devin (SWE-2).
대상: R3 커밋 `723f3c40` diff + 주변 호출 코드.
판정: **NOT CONVERGED → 수정 후 재검증** — 4건 보고, #1 Medium(보안), #2–#4 Low.

## A. Findings & fixes

### F1 (Medium→High 경로) — 폴백이 살아있는 federated 유저를 비활성화할 수 있었음

- **What:** R3에서 추가한 `_deactivate_missing_user` 의 `get_by_sso_subject(username)` 폴백. Federated Cognito 유저는 Username 이 `Google_xxx` 형태라 `admin_get_user(sub)` 가 UserNotFound → "Cognito 에 없다"고 오인 → 폴백이 DB 행을 sub 로 찾아 **살아있는 유저를 비활성화 + VK 전량 폐기**.
- **Fix:** 폴백(sub/synthetic)으로만 찾은 행은 `list_users(Filter='sub = "…"')` 로 실제 삭제 확인 후에만 비활성화. 확인 불가(예외) 시 fail-closed 스킵. `_user_exists_by_sub` 헬퍼 신설.
- **Tests:** `test_sync_user_fallback_hit_does_not_deactivate_live_federated_user`(풀 잔존 → 스킵), 기존 synthetic 테스트에 `list_users: []` 확인 경로 추가.

### F2 (Low) — Cognito 일시 오류가 5분 "!" 캐시로 관리자 강등

- **What:** `_email_attrs_from_cognito` 는 예외(스로틀링/네트워크)와 미검증을 구분 못 해 `(None, False)` — R3에서 둘 다 300s "!" 캐시.
- **Fix:** 미해결 캐시 TTL 300s → **30s**(`_EMAIL_ENRICH_MISS_TTL`). 양수 결과만 5분 유지.

### F3 (Low) — Redis 다운 시 요청마다 Cognito 호출

- **What:** email-less 토큰 배치에서 Redis 장애 시 전 사용자 요청이 AdminGetUser 로 직행 — 계정 공유 읽기 쿼터 소진 → F2 연쇄.
- **Fix:** 인프로세스 TTL dict(`_EMAIL_ENRICH_CACHE`, 10k 상한 clear)를 1차 캐시로 추가 — Redis miss/다운 양쪽에서 커버.

### F4 (Low, accepted residual) — 양수 캐시가 이메일 변경을 최대 5분 추종

- Cognito 에서 이메일 변경/검증 해제 후 최대 300s ADMIN 매칭 유지. 완화: 사용자 비활성화는 `is_active` 매 요청 검사라 즉시 차단. sync upsert 시 캐시 무효화는 서비스가 Redis 핸들을 안 갖는 구조라 문서화로 종결.

## B. kiro 정합 확인(수정 불요)

- enrichment 실행 순서(user lookup/is_active 이후 — 미프로비저닝 호출 없음), 캐시 키 오염 불가(sub는 서명된 JWT), `Redis decode_responses` 정합, `@unknown` resolver 우회 경로 부재, R1/R2 계약(defer/drain/lock) 미훼손.
- `_ensure_team_id` savepoint 는 R3 diff 가 아니라 기존 코드 — 캐시는 savepoint 성공 후에만 갱신, 정합.

## C. kiro 지적 수용 — startup 경고 조건 정정

Cognito 풀 설정이 있으면 서버측 보강이 `OIDC_EMAIL_CLAIM != "email"` 을 커버하므로, `oidc.admin_emails_unusable` 경고는 보강 불가(풀 미설정)일 때만.

## Validation

- admin-api `pytest tests/` — **927 passed, 50 skipped** (신규 회귀 1건 + 기존 2건 갱신)

## Deployment

admin-api 이미지 `git SHA` 태그 → helm upgrade `migration.enabled=true` → rollout + smoke.
