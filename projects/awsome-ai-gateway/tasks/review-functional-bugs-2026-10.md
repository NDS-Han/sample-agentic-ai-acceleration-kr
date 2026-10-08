# 기능 버그 리뷰 — LLM Gateway 전체 (2026-10)

> 리뷰어: Devin 1차 코드 감사 + kiro-cli (Opus 5.5) 2차 독립 감사, 교차 검증 완료.
> 범위: gateway-proxy (라우팅·인증·레이트리밋·예산·사용량기록·스트리밍·폴백), admin-api (모델·예산·키·분석·스케줄러), worker 2종.
> 각 항목: 파일:라인, 재현 시나리오, 심각도(HIGH=과금오류/인가우회/데이터손상, MED=엣지 오동작, LOW=경미).

---

## HIGH — 즉시 수정 권고

### H1. 레이트리밋 단계 간 예약 누수 — 거절된 요청이 이전 스코프에 예약을 남김

**파일:** `gateway-proxy/src/app/services/rate_limit_enforcement.py:163-196`, `rate_limit_service.py:check_multi_scope_tpm(207-273)`, `reserve_cost(367-459)`

**구조:** `enforce_rate_limits`는 RPM → TPM → CPM/CPH 순으로 검사하는데, **TPM/비용 Lua는 "체크+예약 커밋"이 한 eval**이다. 각 스코프 eval이 통과하면 이미 `INCRBY`(TPM) / `INCRBYFLOAT`(비용)가 커밋된다. 그런데 `state["rate_limit_state"]`는 **전 단계 통과 후에야**(line 199) 채워지므로, 중간 거절 시 `release_reservations`/`finalize` 어느 쪽도 예약을 모른다.

**세 가지 누수 지점:**
1. USER TPM eval 통과(+예약 커밋) → TEAM TPM 거절 → **USER TPM 예약 누수**
2. TPM 전 스코프 통과 → `reserve_cost` 거절 → **USER+TEAM+GLOBAL TPM 예약 전부 누수**
3. USER 비용 eval 통과 → TEAM 비용 거절 → **USER CPM/CPH 예약 누수** (주석이 "다음 settle이 환불"이라 주장하지만 **거짓** — settle은 자기 요청의 차액만 조정. 누수분은 키 만료까지 남음: cpm 120s, cph 최대 2시간)

**재현 시나리오:** 팀 TPM이 포화된 상황에서 유저가 재시도를 반복. 매 429마다 `est_input + max_output`(수만 토큰)이 **TEAM TPM `cur` 버킷에 누적** → 거절이 거절을 부르는 양의 피드백 → 팀 전체 TPM 기아 (다른 유저 피해). CPH 누수는 2시간 TTL로 자기 한도를 깎아먹는 자가 DoS.

**검증 상태:** 코드 경로로 확인 — unwind 호출이 어디에도 없음. 테스트 필요.

---

### H2. `identify_client` — 스푸핑 가능한 헤더를 인가·예산 축으로 사용

**파일:** `gateway-proxy/src/app/services/client_identifier.py` (문서화된 스푸핑 가능성 인정), 소비처: `middleware/client_id.py:39` → `state["client"]` → `check_client_model_scope`(`router_service.py:84`), `check_client_scope`(`router_service.py:72`), per-app 예산 키 `budget:user:{uid}:{client}:{period}`(`cost_recorder.py:269`)

**문제:** `identify_client`는 `User-Agent`/`originator`/`anthropic-client-platform` 헤더로 클라이언트를 분류한다 — 자기 문서도 "NOT an authorization signal"이라 명시. 그런데 실제로는:
- **모델×앱 허용목록**(`allowed_clients`)의 권한 축으로 사용 → `anthropic-client-platform: desktop_app` 헤더 하나로 `cowork`로 위장 → 허용목록 우회
- **키의 allowed_clients** 제한도 동일하게 우회
- **per-app 예산 귀속**: `claude-code` 예산 $5를 다 쓴 유저가 헤더만 바꾸면 `codex` 버킷으로 지출 귀속 이동 → per-app 한도 무력화

**재현 시나리오:** VK 보유자가 curl로 게이트웨이 직접 호출 + 헤더 위조. 계정 축(`allowed_models`)은 여전히 걸리지만, 운영자가 설정한 앱 단위 제한은 전부 우회됨.

**검토 필요:** 모델 `allowed_clients`가 "보안 경계" 의도인지 "소프트 분류" 의도인지 — 보안 경계라면 헤더 기반 분류로는 부족 (VK 메타데이터에 클라이언트를 바인딩하거나, 헤더 위조를 인정 문서화 필요).

---

## MED — 수정 권고

### M1. `model:{provider_model_id}` 캐시 키 무효화 누락 — 비활성화/삭제/가격변경이 최대 300s 지연

**파일:** `gateway-proxy/src/app/services/router_service.py:286-288` (resolve가 `model:{alias}` **와** `model:{pmid}` 두 키를 씀), 무효화 누락처: `admin-api/src/app/services/model_service.py`
- `update_model`:168 — `model:{alias}` + `model:list`만
- `set_pricing`:215 — **`model:{alias}`만** (list도 없음)
- `patch_status`:409 — alias + list (pmid 없음)
- `delete_model`:531 — alias + list (pmid 없음) ← 이번에 추가한 기능도 상속
- `apply_price_sync`:382 — **유일하게 pmid 포함**

**재현 시나리오:** 클라이언트가 provider_model_id(예: `us.anthropic.claude-...`)로 요청하는 경로. 비활성화(kill switch) 후에도 최대 5분간 `model:{pmid}` 캐시 히트로 라우팅됨. 삭제된 모델도 동일.

### M2. 윈도우 경계를 넘는 settle — 환불/추가차감이 다른 버킷에 기록 (양쪽 리뷰 독립 발견)

**파일:** `rate_limit_service.py:settle_tpm(275-310)`, `settle_cost(461-491)`

**문제:** settle은 `time.time()` 기준 **현재** 윈도우/버킷 키에 차액을 쓴다. 예약은 요청 시작 시점 버킷에 들어갔다. >60s 스트리밍(LLM의 정상 케이스)이 분 경계를 넘으면:
- TPM: `cur`이 `prev`로 로테이션된 뒤 환불이 새 `cur`에 적용 → 새 버킷 **음수화** → 그 분 동안 한도 초과 허용. 이전 버킷의 예약은 환불 안 된 채 만료.
- CPM/CPH: 동일 구조 — 환불이 새 분/시 카운터를 음수로 → 한도 우회.

**재현 시나리오:** Opus extended-thinking 등 긴 스트리밍이 분 경계를 건너면 매번 발생. `cost_reserved`/`tpm_reserved`에 예약 시점 윈도우를 저장해 settle이 그 버킷을 치게 해야 함.

### M3. Bedrock Converse 경로 캐시 토큰 유실 → 과소청구 (kiro 발견, 확인됨)

**파일:** `gateway-proxy/src/app/providers/bedrock_adapter.py:163-177`

converse 분기가 `inputTokens`/`outputTokens`만 읽고 `cacheReadInputTokens`/`cacheWriteInputTokens`를 버린다. `/model/.../converse` 호출자는 캐시 사용량이 과금·usage_logs 모두에서 0이 된다. (Anthropic invoke_model 경로는 snake_case로 정상 추출됨 — 경로별 비대칭.)

### M4. 다운그레이드 규칙이 "첫 매치" 선택 → 높은 임계값 규칙이 영구 무시 (양쪽 독립 발견)

**파일:** `gateway-proxy/src/app/services/downgrade_loader.py` (`order_by threshold_pct.asc()` + `apply_chain`의 `next(...)` 첫 매치)

`opus→sonnet@80%`, `opus→haiku@100%` 규칙이 있으면 사용률 100%에서도 80% 규칙이 먼저 매치 → sonnet으로만 전환. 단계적 하향 의도(더 싼 모델로 더 내려가야 할 때) 무효. `(scope, scope_id, from_model_alias)` 유니크 제약도 없어 다중 규칙 생성 가능. 설계 의도 확인 필요 — "높은 임계값 규칙 우선"이 자연스러운 해석.

### M5. `budget_deduct.lua` — 첫 교차 임계값만 보고 → 100% 알림 영구 누락

**파일:** `gateway-proxy/src/app/redis_scripts/budget_deduct.lua:44-50`

`for t in thresholds: if crossed then triggered = t; break` — 한 요청이 75%→105%를 넘으면 `80`만 반환. 이후 요청은 `old_pct=105`로 어떤 교차도 아님 → 90%, 100% 알림 **영원히 미발송**. notification-worker는 교차 이벤트당 1통 발송이므로 가장 긴급한 "예산 초과" 통지가 빠진다. 최고 교차값(또는 전체 교차 목록)을 반환해야 함.

### M6. OpenAI chat 스트림 추정기가 input/cache 토큰 유실 → 끊김 시 과소청구 (kiro 발견, 확인됨)

**파일:** `gateway-proxy/src/app/services/streaming.py:377-397` (`openai_sse_stream._estimate_if_needed`)

Responses 방언(628)은 `model_copy`로 input/cache/`web_search_count`를 보존하도록 수정됐는데, chat 방언은 새 `TokenUsage`를 만들어 cache 버킷과 `it`(input)을 버림. usage가 마지막 청크에만 오는 chat 스트림이 중간에 끊기면 출력 추정치만 기록되고 대형 프롬프트 입력은 미청구.

### M7. `settle_cost`가 예약 없던 스코프에도 키 생성 — TTL 없는 음수 팬텀 키

**파일:** `rate_limit_service.py:482-489`

reserve는 `has_user_limit`/`has_team_limit`일 때만 해당 스코프 eval을 돌리지만, settle은 `team_id`가 있으면 팀 cpm/cph 키에, 항상 유저 키에 `incrbyfloat`를 쓴다. 예약이 없던 스코프에는 **음수** 잔액이 생기고 — reserve Lua는 `EXPIRE`를 거는데 settle은 걸지 않아 **영구 키**. 나중에 그 창에 한도를 설정하면 음수 카운터가 한도를 부풀린다.

---

## LOW — 기록/후속

| # | 내용 | 파일 |
|---|---|---|
| L1 | `budget:{user,team}:{id}:{period}` 사용량 키 TTL 없음 — 영구 누적(월별 신규 키라 범위는 제한적). `budget:team:{}:{period}` 팬텀 글로벌 카운터 — team_id 없는 유저의 team deduct eval이 무조건 실행됨 | `cost_recorder.py:180,216-238` |
| L2 | Redis INCRBYFLOAT(double) vs DB Decimal 드리프트 — 경계값 부근에서 check/deduct 판정 불일치 가능. 폭은 미세 (kiro #2) | `budget_deduct.lua:42` |
| L3 | JWT 알고리즘 캐시 경로 하드코딩 — `key:cache:jwt:{kid}` 히트 시 `algorithm="RS256"` 고정. 비-RS256 키는 첫 요청(미스)만 성공하고 이후 전부 401 | `auth_service.py:243` |
| L4 | 어댑터 예외가 `(TimeoutError, ConnectionError, OSError)` 밖이면 `run_fallback_loop` 밖으로 전파 — 예약 해제 안 됨 (`_client_resolver` raise 등) | `fallback_loop.py:370` |
| L5 | 2xx 응답인데 usage=0 → `finalize` 0-가드가 스킵하고 loop의 release도 비-2xx만 → 예약 잔류 (희귀) | `messages.py:797` |
| L6 | `_build_status` 혼합 policy 스택에서 `policy`/`threshold`는 team 기준, 금액은 decisive tier 기준 — 헤더 해석 오류 (kiro #9) | `budget_service.py:230-290` |
| L7 | `message_start`의 `input_tokens` walrus truthy 가드 — 0/absent + metrics 프레임 유실 시 입력 미기록 (kiro #7) | `streaming.py:119-132` |
| L8 | 웹서치 루프는 admission 1회로 N개 provider 호출 — RPM은 게이트웨이 요청 기준이라 의도일 수 있으나 TPM 예약은 첫 호출만 커버 (settle이 실사용 정산) | `messages.py:475-615` |

---

## 기각된 의심 (검증 완료 — 버그 아님)

- **VK 캐시 allowed_models staleness (kiro #6):** `user_allowed_model_service.py:110`, `team_allowed_model_service.py:122`에서 `key:cache:vk:{hash}` 역인덱스로 즉시 무효화됨 — 기각.
- **비-2xx 예약 해제 (kiro 후보 → 자체 기각):** `fallback_loop.py:393`이 모든 비-2xx에 `release_reservations`, openai_compat/bedrock 라우터도 개별 해제 경로 보유.
- **다중 스코프 RPM phantom +1:** 코드에 의도 문서화(보수적 방향, 자가보정).
- **cost-recorder-worker 멱등성:** `_filter_replays` + 배치 내 dedup + `ON CONFLICT` + XAUTOCLAIM — 견고.
- **KST period 일관성:** 읽기/쓰기 모두 KST 월 — 이전 불일치 이미 수정됨.
- **스트림 usage 발화:** `usage_fired`/`complete_fired` 가드로 정확히 1회 — 4개 종료 경로 모두 커버.
- **scheduler:** KST 기간, 예외 로깅, 키 만료/퍼지 — 이상 없음.

---

## 우선순위 제안

| 순서 | 항목 | 이유 |
|---|---|---|
| 1 | H1 예약 누수 | 팀 전체 영향 + 재시도 증폭 — 운영 중 실증 가능성 최고 |
| 2 | H2 client 스푸핑 | 인가 우회 — 의도 확인 후 설계 결정 필요 |
| 3 | M1 pmid 캐시 | kill switch 지연 — 수정이 1줄 수준 |
| 4 | M2 settle 윈도우 | 스트리밍 정상 케이스에서 매일 발생 |
| 5 | M3 converse 캐시 | 조용한 과소청구 |
| 6 | M5 임계값 알림 | 100% 알림 미발송 = 운영 사각지대 |
| 7 | M4 다운그레이드 | 의도 확인 필요 |
| 8 | M6/M7 | 청구/카운터 정확도 |
