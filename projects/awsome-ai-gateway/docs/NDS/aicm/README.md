# AICM `/aicm/v1` 통합 가이드

이 디렉터리의 `aicm-v1-openapi.json`이 기계 판독용 계약서다. 아래는 통합 담당자용 운영/연동 요약이다.

> **환경 결정(2026-11-26):** AICM 통합 시험은 내부(prod) 환경에서만 한다. dev 는 공개 환경이므로
> `admin-api: AICM_API_ENABLED=false` 를 영구적으로 유지한다. dev 에서는 플래그가 꺼진 상태(404/비마운트)만
> 검증하고, 어떤 검증 목적으로도 dev 에서 이 플래그를 켜지 않는다.

## 경로 가정(Q1)

phase 0a2 경로는 AICM 질의응답 Q1 의 가정대로 기존 `/admin/*` 표면을 `/aicm/v1` 아래에 mirror 한 형태다.
예: `/admin/users?email=` → `/aicm/v1/users?email=`, `/admin/budgets/user/{id}` →
`/aicm/v1/budgets/user/{id}`. 이 가정은 최종 AICM 경로 설계가 바뀌면 재검토한다.

## 인증과 scope

`POST /aicm/v1/service-tokens`만 사람 admin JWT(`Bearer` JWT)를 쓴다. 그 외 모든 호출은
`Authorization: Bearer itg-...` 통합 토큰만 허용한다. 기존 `svc-` 토큰과 사람 JWT는 401이다.
발급 시 `allowed_cidrs`를 지정하면 그 원천 대역만 허용된다. 토큰 계보별로 read/write/lifecycle 레이트리밋이 적용된다.

| scope | 경로 |
|---|---|
| `tokens:read` | `GET /service-tokens` |
| `tokens:rotate` | `POST /service-tokens/rotate` |
| `users:provision` | `POST /users/provision` |
| `users:lifecycle` | `POST /users/{user_id}/deactivate` |
| `users:sync` | `POST /users/sync-cognito/user/{username}` |
| `users:read` | `GET /users?email=` |
| `policy:read` | `GET /users/{id}/allowed-clients` |
| `policy:write` | `PUT/DELETE /users/{id}/allowed-clients` |
| `budgets:read` | `GET /budgets/user/{id}`, `GET /budgets/team/{id}` |
| `budgets:limits:write` | `PUT/DELETE /budgets/user/{id}`, `PUT/DELETE /budgets/team/{id}`, `PUT /budgets/team/{id}/default-cap` |

`users:sync`는 Cognito 사용자를 게이트웨이 DB와 동기화한다. `GET /users?email=`는 exact 매칭이며 대소문자는 무시한다.
예산 API는 총액 한도와 team default cap만 다룬다. spent seeding, per-app budget, reactivate, managed lock,
팀 리더십/역할 변경은 이 계약에 없다.

## 멱등성

모든 mutation(POST/PUT/DELETE)은 `Idempotency-Key` 헤더가 필수다(1–128자 printable ASCII). 같은 키와 같은 요청을
다시 보내면 저장된 응답이 `Idempotent-Replayed: true`와 함께 반환된다. 같은 키의 다른 요청은
`422 idempotency_key_reused`, 진행 중이면 `409 idempotency_in_progress`다. 실패한 요청은 저장하지 않으므로 같은 키로
재시도할 수 있다. rotate 응답의 새 토큰 원문은 저장하지 않으므로 재전송은
`409 idempotency_secret_not_replayable`를 반환한다.

예:

```bash
curl -X PUT "$BASE/aicm/v1/budgets/team/$TEAM_ID" \
  -H "Authorization: Bearer $ITG_TOKEN" \
  -H "Idempotency-Key: aicm-budget-2026-11-001" \
  -H 'content-type: application/json' \
  -d '{"max_budget_usd":"100.00","confirm":true}'
```

## 보호 대상과 확인(confirm)

사용자 대상 경로와 user budget 경로는 대상이 ADMIN, 시스템 사용자, 서비스 계정이면 403으로 거부하고 감사 로그에 남긴다.
오류 code는 `target_is_admin`, `target_is_system_user`, `target_is_service_account`다.

즉시 차단/연쇄 삭제 위험이 있는 예산 변경은 기존 `/admin/*`와 같은 `confirmation_required` 409를 반환할 수 있다.
`details.warnings`를 검토한 뒤 같은 본문 또는 query에 `confirm=true`를 넣어 재제출한다.

## 오류 봉투

통합 오류는 다음 형태다.

```json
{"error":{"type":"forbidden","code":"insufficient_scope","message":"missing scope: budgets:limits:write"}}
```

대표 코드: `missing_token`/`invalid_token`(401), `source_ip_forbidden`/`insufficient_scope`(403),
`rate_limited`(429, `Retry-After` 있음), `idempotency_key_required`/`idempotency_key_reused`/
`idempotency_in_progress`, `cognito_busy`/`cognito_throttled`(503), `cognito_timeout`(504).
비즈니스 검증 오류는 기존 admin-api의 표준 오류 코드(`budget_rule_violation`, `not_found` 등)를 유지한다.

## 캐시와 감사

allowed-clients 변경은 기존 admin 경로와 같은 VK 인증 캐시 키(`key:cache:vk:{hash}`)를 커밋 후 삭제한다.
예산 변경은 기존 admin 경로와 같은 gateway 예산 캐시 키(`budget:config:team:{team}`,
`budget:config:user:{user}`)와 지연 Redis 쓰기 경로를 사용한다. mutation 감사는 비즈니스 변경 및 멱등 완료와 같은
DB 트랜잭션에 기록되고 `_integration`, `_idempotency_key`, `_service_token_id`, IP, request id, before/after를 포함한다.
