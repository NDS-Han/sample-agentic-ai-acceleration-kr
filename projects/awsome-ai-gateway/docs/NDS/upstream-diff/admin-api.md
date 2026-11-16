# admin-api — upstream 대비 변경 전체

upstream 마지막 마이그레이션은 `0023_add_usage_logs_ttft`. NDS는
**34개 신규 마이그레이션(0024–0057)** + **신규 라우터 5종** + 기존
엔드포인트 대폭 확장을 추가했다.

## 1. 신규 라우터

| 라우터 | 기능 |
|---|---|
| `apps.py` | 앱 레지스트리 — per-app 기본 모델·모델 접근 허용(`allowed_clients`)·앱 정책 조회 |
| `audit_reconcile.py` | 감사 로그 대사(reconcile) — 누락·드리프트 점검 |
| `body_logs.py` | 요청/응답 본문 로깅 조회 — 관리자 토글로 런타임 on/off, 위험 방향 가드 |
| `service_accounts.py` | 서비스 계정 CRUD + 장기 VK 발급(사람 로그인 경로와 분리) |
| `settings.py` | `system_settings`(migration 0036) 읽기/쓰기 — 알림 임계값 등 운영자 설정 |

## 2. 기존 라우터 확장

- **`auth_oidc.py`** — `/exchange` + `/provision`(PR #4), `provision_user`
  추출, 레이트리밋 공통화(`rl:auth_exchange:{ip}` 10/min)
- **`users.py`** — `sync-cognito`(전체 열거·`custom:groups`·per-user/group),
  팀 리더 해제·이관 강등·동기화 보존, 멤버 검색, `team_source` 수동 보호
- **`cli.py`** — STS presigned URL 로그인(`ALLOWED_STS_REGIONS/ACCOUNTS/
  IAM_ROLES` 화이트리스트, 서버측 https fetch 검증)
- **`budgets.py`** — 알림 임계값 저장/조회, 자동 다운그레이드,
  멤버 검색·bulk 편집
- **`analytics.py`** — 임의 기간(custom range) + 캐시 키 client·기간 분리,
  rate-limit 사용량 시계열
- **`models.py`** — 모델 라우팅 필드(x-account ARN), context_window/
  max_output_tokens, provider_model_id, hard delete, LiteLLM 단가 동기화
- **`rate_limits.py`** — scope별 사용량 추이, `/users` 패널 편입
- **`monitoring.py`** — 채팅 drain/크로스파드, 대시보드 분모 정합
- **`internal.py`** — 내부 API 토큰(`INTERNAL_API_TOKEN`) 채널
- **`my.py`** — `/my/profile` 유효 역할 조회
- **`keys.py`** — VK 수명주기(dedup·만료·폐기 알림·purge 기본 off),
  key_prefix(0051/0052), sha256 해시(0056)
- **`productivity.py`**, **`dashboard.py`**, **`chat_agent.py`** —
  생산성 집계·대시보드·chat-agent 리포트(0057)
- **`routing.py`** — 클라이언트 라우팅 프로필 + DR
- **`service_tokens.py`** — 내부 서비스 토큰 발급 경로

## 3. 마이그레이션 0024–0057 주제 분류

| 주제 | 마이그레이션 |
|---|---|
| 모델 카탈로그/별칭 | 0025(gpt56), 0027(claude_5), 0028(codex default), 0029(llama_3_70b), 0031(openai enum), 0032(runtime plane 별칭) |
| 가격 정정 | 0030(sonnet_5), 0033(gpt56 프로모션) — 단가 NUMERIC(12,8) 정밀도 |
| 무결성 인덱스 | 0024(active config 유일), 0026(requested_at), 0034(누락 인덱스·유일성), 0044(daily_aggregates bigint), 0045(team dept 유일), 0053(audit ts_id) |
| 허용 계층 | 0035(model.allowed_clients) → 0038(team/org allowed_clients — user>team>org) |
| CAP/예산 | 0039(team 기본 user cap D), 0041(알림 임계값), 0047(haiku55 장문 티어) |
| 요금 티어 | 0042(long_context_pricing), 0043(usage_log context_tier) |
| 운영 설정 | 0036(system_settings) |
| 알림·채팅 | 0040(notification dedup), 0046(chat_reader grants), 0057(chat_agent reports) |
| 팀/계정 라우팅 | 0048(team_source), 0049(model-level account routing) |
| 키 수명주기 | 0050(service account), 0051/0052(key_prefix+backfill), 0055(xaccount 샘플 정리), 0056(vk sha256), 0054(session_id) |

## 4. 서비스 계층

- **`oidc_service.py`** — `provision_user`/`exchange_jwt_for_vk` 분리,
  Cognito 이메일 enrich, `_resolve_team`(인덱스 단건 조회 + savepoint
  레이스 — 10k boot-storm 부하테스트로 튜닝), `_upsert_user` UNIQUE
  충돌 처리, `transfer_user` 롤백 보호
- **`cli_service.py`** — gateway 캐시 하이드레이트(`_cache_for_gateway`)
- **`cognito_sync`** — 락·per-entity 동기화·full enumeration
- **감사** — `audit_catalog.py`, request_id 전파(`x-request-id` 헤더 폴백),
  commit 후 Redis 무효화(`cache_invalidation.py`)
- **`db.py`** — pool_size 10 + overflow 20, pool_timeout·connect·command
  타임아웃 설정화(`DB_*`)
- **스케줄러** — key_expirer·key_purger·roi_aggregator 등 백그라운드 잡
