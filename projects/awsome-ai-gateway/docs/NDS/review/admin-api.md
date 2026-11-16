# admin-api 최종 적대적 리뷰

> 상태: 독립 리뷰 완료 + 확정 결함 수정·회귀 테스트 추가(`test_cli_service` 15 tests).
> R3–R25 라운드로 대부분의 결함 계열이 소거된 상태에서 잔여 3건 발견·수정.

## 개요

범위: `admin-api/src/app` 전역 — 인증/인가, IDOR, 비밀 유출, 주입, SSRF, 예산 경합,
커서, 트랜잭션 경계, 비동기 경합, Redis/DB 장애 fail-open/closed, 재시도 stale 상태.

## 핵심 계약

- STS 로그인은 `sts.{region}.amazonaws.com` 호스트 allowlist + `Action=GetCallerIdentity` 만
  허용, 계정/롤 allowlist 는 **빈 값=전량 거부**(fail-closed), XML 은 defusedxml.
- JWT 는 비대칭 알고리즘만, OIDC JWKS 는 issuer 핀 + aud/client_id + 30s 리프레시 쓰로틀.
- 모든 Redis 쓰기는 커밋 후(`defer_redis_write_until_commit`); `budget:config:user:{uid}` 는
  DEL 만 — 게이트웨이가 미스 시 DB 에서 원자 재도출.
- 유저 단위 예산 불변식(I-1…I-4)은 advisory xact lock + savepoint 직렬화, 확인 필요 변경은
  409 + `*_CONFIRMED` audit action.
- VK 폐기·이관·비활성화의 Redis 부수효과는 전부 commit 후 drain; 복호화 실패분은 `vkrev:{id}`
  실패 행으로 남겨 retry 회수.
- TEAM_LEADER 스코핑은 analytics `_resolve_scope`(기본-거부)와 모든 예산 경로에서 fail-closed.

## 발견·수정된 결함

- **HIGH** — `services/cli_service.py`: STS URL 의 **scheme 미검증**. `http://sts.*.amazonaws.com` 이
  호스트 allowlist 를 통과해 평문 fetch — 경로상 공격자가 위조 `GetCallerIdentityResponse` 로
  허용 계정/롤 ARN 을 주장 → 무인증 VK 발급·자동 프로비저닝(STS 인증 경계 전체 우회).
  → `scheme != "https"` 거부 + 응답 256KiB 상한(거대 본문 파서 압력 차단). 회귀 테스트 2건.
- **MED** — `services/cli_service.py` 자동 프로비저닝: 미검증 `session_name` 을 신뢰 컬럼
  `users.email` 에 적재 → 공격자가 피해자 이메일로 스쿼팅, verified-email rebind 시 행+VK 통째 이관
  (부수: email UNIQUE 충돌 시 미처리 IntegrityError → 500).
  → `sts-{sha256(arn)[:16]}@unknown` 합성 주소로 저장 — 어떤 실제 이메일과도 충돌 불가
  (OIDC `{sub}@unknown` 과 동일 계약). 회귀 테스트 1건.
- **LOW** — `services/budget_service.py:1644`: `/admin/budgets/summary?scope=` 무효값이
  `BudgetScope(...)` ValueError → 500. → `ValidationError`(400) 정규화.

## 검증 완료 — 결함 아님(경화 확인)

- `rl_usage_trend` f-string 컬럼은 검증된 scope 화이트리스트 유래 — 주입 불가
- `ratelimit:config:*`/`rl:config:*` 양 패밀리 대칭 무효화
- `seed_spent` savepoint+FOR UPDATE+delta — 멱등, 증분 유실 없음
- internal 엔드포인트 `compare_digest`+prod 게이트+fail-closed; 422 `input` 반사 마스킹
- VK 발급 `expire_and_create` 원자 CTE + 부분 유니크 인덱스 + savepoint 재시도

## 잔여 리스크

- `/test/issue-key`·`/cli/downloads` 등 무인증/dev 표면은 prod 게이트로 닫힘 — INTERNAL_API_TOKEN
  유출 시 dev 에서 키 발급 가능(수용된 계약).
- JWKS 강제 리프레시 30s 쓰로틀 — IdP 키 롤오버 직후 최대 30s 의 unknown-kid 401 창.
- `resp.text` 전체 버퍼링 + 요청당 httpx 클라이언트 생성 — 부하 시 비용(성능 잔여).
- Cognito reconcile 의 `provider` 필터는 STS 행 제외 — 과거 squat 행은 자동 정리 안 되므로
  운영자 점검 필요(본 수정 이후 신규 생성은 불가).
- `sso_subject` rebind 시 기존 ACTIVE VK 연쇄 폐기 미구현 — 합성 이메일로 squat 경로 자체는
  차단됐으나, 다른 경로의 rebind 가 생기면 VK 이관 검토 필요.
