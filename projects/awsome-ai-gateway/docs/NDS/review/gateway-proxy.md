# gateway-proxy 보안·신뢰성 리뷰

> 상태: 최종 독립 리뷰 완료. 결함 #1~#2(MEDIUM)는 수정 후보로 확정, LOW는 정책 판단 필요.
> 라이브 Bedrock/Redis 장애 주입은 미검증 — 정적·코드 추적 기반.

## 개요

가상키(VK)/JWT 인증, 모델·앱 인가, 레이트리밋, 월예산, 사용량 회계, 프로바이더 폴백, Bedrock 패스스루를 담당하는 ~66k LOC FastAPI/ASGI 게이트웨이. Pure ASGI 미들웨어 체인(스트리밍 보존 목적) 아래에서 Redis(ZSET·예약·스트림)와 Postgres를 이중 백엔드로 쓰며, degradation manager가 백엔드별 열화를 마킹한다. 전반적으로 방어 주석과 멱등 정산이 촘촘하고, 인증·예산의 인프라 장애는 대부분 503 fail-closed다.

미들웨어 체인(`main.py`): StateInjection → OTel → MaxBodySize → ClientIdentification → Auth → ClientAuthorization → Budget → Downgrade → Rate-limit → 라우터. 인증 실패는 `middleware/auth.py:112-148`에서 401(자격 거부) vs 503(인프라 장애)로 분기.

## 핵심 계약

- 인증 전략은 경로로 결정: `/model/*`→VK, 추론 `/v1/*`→DUAL(토큰 형태로 VK/JWT 판별, `auth_service.py:608-641`), 나머지 `/v1/*`→JWT. VK는 `sha256(token)` Redis 매핑 + 300s AuthContext 캐시(`key:cache:vk:{hash}`). JWT는 비대칭 alg만 허용(alg-confusion 방어)+issuer/aud 강제. **마스터키 없음** — VK는 USER/TEAM_LEADER 역할로 상한(`:287-296`).
- 인가 3축 AND: `check_key_scope`(allowed_models, `router_service.py:586-613`) × `check_client_scope`(키 allowed_clients) × `check_client_model_scope`(모델 allowed_clients; `[]`=전면거부, `None`=무제한).
- 레이트리밋은 Lua(`redis_scripts/rate_limit_check.lua`)로 USER/TEAM/GLOBAL 원자 검사 후 TPM/비용/월예산을 입장 시 예약(`reserve_budget`, `rate_limit_enforcement.py:525-535`), `state["rate_limit_state"]` pop으로 정산 소유권 이전(`fallback_loop.py:237-285`). 정산은 마커 기반 멱등.
- 열화 정책: 인증 BOTH_DEGRADED→503(`middleware/auth.py:68-70`), `allowed_models` 조회 실패→503(fail-closed), `allowed_clients` 조회 실패→허용(fail-open, `auth_service.py:93-99`), 예산 Redis→DB 폴백 후 동시 실패→`BudgetBackendUnavailableError`→503(`budget_service.py:912-923,1344`).
- 프로바이더 자격증명은 어댑터가 자체 생성(Mantle 단기 bearer, SigV4 자체 서명, 크로스계정 STS) — **클라이언트 `Authorization`은 업스트림에 전달되지 않는다**.
- 회계: `cost_recorder.finalize`가 Redis 예산 카운터 정산 + usage stream XADD(→usage_logs). 비-SUCCESS는 0토큰 행도 기록(`:427-448`).

## 발견·수정 후보 결함

| # | 심각도 | 위치 | 내용 |
|---|--------|------|------|
| 1 | MEDIUM | `services/router_service.py:208-219`, `services/client_identifier.py:14-41`, `services/budget_service.py:538-539` | 모델×앱 바인딩이 키 `allowed_clients=None`(발급 기본값)에서 스푸핑 가능한 헤더(`User-Agent`/`anthropic-client-platform`/`originator`) 주장만으로 통과 — 동일 주장이 per-app 예산 버킷·라우팅 프로파일·크로스계정 역할 선택도 결정해 앱별 예산 회피·타 앱 버킷 오염·의도치 않은 라우팅 가능. → 무제한 키엔 fail-closed 전환 또는 발급 시 allowed_clients 강제. |
| 2 | MEDIUM | `services/rate_limit_enforcement.py:536-562` + `services/budget_service.py:28` | `BudgetBackendUnavailableError`(PermissionError 서브클래스)가 `except PermissionError`에 잡혀 백엔드 장애가 "예산 초과" 429로 오보고 — BudgetMiddleware의 503 계약과 모순, 클라이언트 rate-limit 백오프 유발·`budget_exceeded` 메트릭 오염. → 전용 except로 503 매핑. |
| 3 | LOW | `rate_limit_enforcement.py:563-567` | 예약 경로의 비-Permission 예외를 `budget_reservation=[]` fail-open 통과 — 월 하드캡이 check-only로 회귀해 동시성 초과 허용. |
| 4 | LOW | `auth_service.py:239-254` | DB_DEGRADED 시 캐시 적중 VK의 `is_active` 재확인 생략 — 비활성화된 계정이 최대 300s 인증 유지(캐시 미스는 503 fail-closed라 비대칭). |
| 5 | LOW | `middleware/auth.py:169` | DUAL 경로(`/v1/messages` 등)의 VK 인증 실패가 JWT로 오기록 — 브루트포스 탐지 버킷 왜곡. 토큰 형태 기반 분류 필요. |
| 6 | LOW | `cost_recorder.py:427-449` + `routers/openai_compat.py:601-619`, `routers/messages.py:1433-1437` | 성공 2xx + usage 파싱 실패 시 예약만 해제, usage_logs 행 미생성 — 프로바이더 청구분 감사 공백. `unparsed_usage` 플래그 행 또는 추정 과금. |
| 7 | INFO | `auth_service.py:311-321` | 엔타이틀먼트 축소가 캐시 TTL(≤300s)/admin DEL 계약에 의존 — 정책 변경 무효화 계약 명문화 필요. |
| 8 | INFO | `auth_service.py:611-615` | `/v1/chat/completions`·`/v1/completions`의 VK 수용은 문서화된 임시 조치 — JWT 전환 이슈로 추적. |

## 검증 완료 — 결함 아님(양호 확인)

- 마스터키/우회 없음; ADMIN은 JWT 경로만
- 클라이언트 `Authorization` 업스트림 미전달(Mantle/SigV4 자체 자격증명)
- `allowed_models` 조회 실패 fail-closed 503; Lua 다중 스코프 원자 검사
- `usage_fired`+pop 정산 소유권; raw Bedrock 스트림 await 우선순위 수정 확인(`routers/bedrock.py:441-459`)
- 보안이벤트 source IP는 XFF 최우측 홉 사용; BOTH_DEGRADED fail-closed

## 잔여 리스크

- **의도된 fail-open**: `allowed_clients` 조회 실패 허용(soft gating), 예약 경로 장애 통과(#3), 성공 0토큰 무기록(#6) — 로그는 남으나 정책 문서에 "허용"으로 명시 필요.
- **크로스서비스 계약**: VK revoke의 `key:vk:{hash}`/`key:cache:vk:{hash}` 이중 DEL 순서는 admin-api 책임 — 매핑 우선/원자 삭제 계약 확인 필요.
- **감사 공백**: web-search 멀티턴의 단일 request-id 부재(조인 한계); upstream 4xx는 body 로그에만.
- **SSRF 표면**: `model_aliases.endpoint_url`은 admin 설정값 — admin-api 침해 시 SSRF 경로. 엔드포인트 allowlist 권장.
- 스트리밍 중도 단절 시 `estimated_input` 추정 과금은 유실 방지이나 과다 청구 방향 오차 — reconcile 대조 필요.
