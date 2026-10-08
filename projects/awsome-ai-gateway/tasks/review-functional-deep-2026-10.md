# 기능 버그 심층 리뷰 (2차) — LLM Gateway

2026-10, 브랜치 `nds/admin-ui-ux-review` 기준.
1차 리뷰(`review-functional-bugs-2026-10.md`)에서 수정 완료된 항목(H1·M1~M7 등)은 재보고하지 않는다.

검토 방식: (a) 수동 코드 감사 + (b) kiro-cli 2차 독립 리뷰 → 양쪽 주장을 코드로 상호 검증.

> ⚠️ kiro-cli 모델 고지: `kiro-cli chat --model claude-opus-5.5` 실행 시
> `[warn] failed to set model 'claude-opus-5.5': Method not found` 가 출력됐다.
> 설치된 CLI 버전에서 해당 모델을 지원하지 않아 **기본 모델로 폴백 실행**됐다.
> 결과물 자체는 유효한 독립 리뷰이지만 "Opus 5.5로 검증됐다"고 주장할 수 없다.

범위: gateway-proxy / admin-api / cost-recorder-worker / notification-worker / scheduler.

## 요약

| # | 심각도 | 한 줄 | 출처 |
|---|--------|-------|------|
| **F1** | **HIGH** | cost-recorder **부분 재처리** 시 일별 Redis 카운터 이중 집계 + threshold 재발행 — DB 경로만 replay 필터, 카운터/알림은 비필터 `entries` 사용 | 양쪽 독립 확인 |
| **F2** | **HIGH (도달 시)** | `/model/*/invoke-with-response-stream`·`/model/*/converse-stream` 패스스루 — usage 파서가 OpenAI SSE 전용이라 **usage_logs 행 자체가 안 남음** (무료 추론) | kiro → 본인 검증·정밀화 |
| **F3** | **MED** | notification-worker `event_id` 멱등 없음 → F1 재발행이 중복 예산 메일로 발송 | 양쪽 독립 확인 |
| **F4** | **MED** | gateway JWT 인증이 `kid` 무시 + 활성 키 다중 시 `.first()` 비결정 → 로테이션 중 정상 토큰 401 + 잘못된 PEM이 1h 캐시 | 양쪽 독립 확인 |
| **F5** | **MED** | `CostStreamEntry.requested_at`=완료시각, `period`=완료 월 — Redis 예산 카운터(요청 시작 월)와 `budget_usages`(완료 월)가 월 경계에서 어긋남 | 본인 |
| **F6** | **MED** | 모델 `endpoint_url` 검증 없음 — 스킴/호스트 무제한 → ADMIN이 등록한 URL로 게이트웨이가 임의 POST (SSRF 경로) | 본인 |
| **F7** | **LOW** | 월 예산은 check-then-deduct(예약 없음) — 동시 요청이 hard_block 한도를 초과 통과 | 본인 |
| **F8** | **LOW** | `cost:stream` MAXLEN ~100k trim — 워커 장애 누적 시 미기록 usage 조용히 유실 (spool은 XADD 실패만 커버) | 본인 |
| **F9** | **LOW** | 무소속 유저의 `budget_usages` TEAM 행이 Default Team UUID로 누적 — Redis 팀 카운터(스킵)와 비대칭 | 본인 |
| **F10** | **LOW** | `daily_aggregates` ON CONFLICT DO NOTHING — 크론 이후 늦게 flush된 어제 행 영구 누락 | kiro → 검증 |
| **F11** | **LOW** | OpenModel `aiter_bytes` + 청크별 독립 라인 파싱 — usage 프레임이 청크 경계에 걸치면 input 토큰 0 | kiro → 검증 |
| **F12** | **LOW** | converse-stream 분기만 connect-retry 없음 (비대칭) | kiro → 검증 |
| **F13** | **LOW** | `notifications:budget` PUBLISH는 영속성 없음 — worker 다운 시 임계 알림 통째 유실 | kiro → 검증 |

---

## F1 (HIGH) — 워커 부분 재처리: 일별 카운터 이중 집계 + 알림 재발행

**파일:** `cost-recorder-worker/src/worker/batch_flusher.py` `BatchFlusher.flush()` (lines 217-269)

`flush()`는 재처리 방어를 DB 경로에만 적용한다:

```python
fresh = await _filter_replays(session, entries)   # usage_logs에 이미 있는 request_id 제외
if not fresh:
    ... return                                    # 전부 replay면 조기 종료 — 안전
await self._insert_usage_logs(session, fresh)     # fresh만 INSERT
await self._upsert_budget_usages(session, fresh)  # fresh만 UPSERT — DB 정확
...
await self._bump_daily_counters(entries)          # ⚠️ 원본 entries — replay 포함
await self._publish_thresholds(entries)           # ⚠️ 원본 entries — replay 포함
```

**트리거:** 배치의 일부만 이미 기록된 경우(`fresh` 부분집합) —

1. **spool 재발행**: gateway XADD가 실패로 보여 스풀에 넣었는데 실제로는 스트림에 기록됨(응답 유실) → 같은 request_id가 나중에 다른 배치로 재소비. `_dedup_in_batch`는 같은 배치 내 중복만 접으므로 통과.
2. **XAUTOCLAIM 겹침**: `_reclaim_orphans`가 처리 중인 형제 replica의 pending을 가져가면 두 워커가 같은 엔트리를 flush — 패자 쪽 배치에서 replay로 필터되지만 카운터/알림은 실행됨.
3. **per-row 폴백 경로**: `_flush_per_row`에서 replay로 스킵된 행도 `_bump_daily_counters(entries)`에 그대로 반영.

**증상:** `usage_logs`/`budget_usages`는 정확(한도 집행 안전)하지만 `usage:daily:*` 카운터가 부풀려져 `/v1/usage/me`의 "오늘 사용량/비용"과 모델별 브레이크다운이 배수로 표시. + F3로 이어지는 중복 알림.

**수정:** `fresh`를 상위 스코프로 끌어올려 `_bump_daily_counters(fresh)` / `_publish_thresholds(fresh)`로 정렬. per-row 폴백은 성공 기록된 행만 반환해 카운터/알림에 반영.

**테스트:** 같은 request_id 혼합 배치를 두 번 flush → 일별 카운터가 1회분만 증가하는지; `_flush_per_row` 경로도 동일.

---

## F2 (HIGH, 도달 시) — `/model/*` 네이티브 스트리밍 패스스루 무방비

**파일:** `gateway-proxy/src/app/routers/bedrock.py` `stream_with_cost()` (lines 228-264)

usage 추출이 `_try_extract_usage(chunk)`(streaming.py:782) 하나뿐인데, 이 함수는 **OpenAI SSE `data: {...usage...}` 형식 전용**:

- `invoke-with-response-stream` → `_bedrock_stream_gen`이 `chunk["bytes"]`(Anthropic 이벤트 JSON, `data:` 접두 없음)를 yield → 매칭 불가
- `converse-stream` → `_converse_stream_gen`이 `json.dumps(event)`(Converse 이벤트)를 yield → 매칭 불가

따라서 `usage=None` → `effective_usage` 전부 0 → `cost_recorder.finalize`의 KI-08 zero-usage 가드(cost_recorder.py:102)가 TPM/비용 예약만 환불하고 **XADD 없이 조기 반환 → usage_logs 행 자체가 생성되지 않는다.** kiro는 "0 토큰 기록"으로 봤지만 실제로는 **기록 자체가 없음** — 더 심각하다.

예산 게이트(`/model/*`는 BudgetMiddleware 대상)와 레이트리밋 예약은 돌지만, 차감이 영원히 오지 않아 **VK만 있으면 이 경로로 무제한 무료 스트리밍**이 가능하다.

**도달성:** `/model/*`는 VK 인증 노출 라우트(resolve_auth_strategy → _VK_STRATEGY). claude-code/cowork/codex는 `/v1/*` 경로라 현 트래픽에서는 안 쓰이지만, 엔드포인트는 열려 있다.

**수정:** 포맷별 usage 파서 — invoke는 `message_start`/`message_delta`/`amazon-bedrock-invocationMetrics`, converse는 `metadata.usage`(camelCase `inputTokens`/`outputTokens`/`cacheReadInputTokens`/`cacheWriteInputTokens`). 최소한 "usage 미검출 스트림 완료" 경고 메트릭을 추가해 무과금 경로를 가시화.

**테스트:** converse-stream SSE 시뮬레이션으로 usage_logs 행 + 토큰이 기록되는지.

---

## F3 (MED) — notification-worker `event_id` 멱등 부재

**파일:** `notification-worker/src/worker/handlers/base.py` `BaseHandler.handle()` (lines ~60-160)

수신자별로 무조건 `NotificationLog` 생성 + 발송 — `(event_id, recipient)` 선점 체크 없음. `notification_logs.event_id`는 인덱스만 있고 가드로 미사용. 생산자의 주석이 "idempotency hint"라고 적은 `event_id`(=`request_id`)를 소비자가 전혀 쓰지 않는다.

**트리거:** F1 재발행 → 동일 "예산 80% 도달" 메일 재발송. SES 실사용 환경에서 사용자 체감.

**수정:** `handle()` 진입 시 `INSERT ... ON CONFLICT (event_id, recipient_email) DO NOTHING` 선점 또는 Redis `SETNX notif:dedup:{event}:{recipient}` 게이트. 근본적으로는 pub/sub → Stream consumer group 전환이 F13도 함께 해결.

---

## F4 (MED) — JWT 인증 `kid` 무시, 로테이션 중 401

**파일:** `gateway-proxy/src/app/services/auth_service.py` `JWTAuthStrategy` (lines 206-243)

- `kid`는 헤더에서 추출하지만 DB 조회는 `select(JwtPublicKey).where(is_active).first()` — **kid 미사용, ORDER BY 없음**. `admin_jwt_configs`에 `kid` 컬럼도 없다.
- 활성 키 2개 이상(로테이션 겹침)이면 `.first()`가 임의의 키 반환 → 다른 키로 서명된 정상 토큰이 InvalidSignatureError → 401.
- 잘못된 PEM이 `key:cache:jwt:{kid}` 아래 1시간 캐시 → 같은 kid의 후속 요청도 계속 401.
- 캐시 히트 시 `algorithm = "RS256"` 하드코딩(1차 리뷰 L3에 기록된 항목)과 함께 수정 필요.

대조: admin-api `JWTVerifier`는 kid 매칭 + 전 키 순회로 올바름.

**수정:** 캐시 미스 시 활성 키 전부 순회 decode(admin-api와 동형). 중기: `kid` 컬럼 추가 + `WHERE kid=:kid`.

---

## F5 (MED) — `CostStreamEntry`의 `requested_at`/`period`가 완료 시각 기준

**파일:** `gateway-proxy/src/app/schemas/cost_stream.py` `make()` (lines 98-130)

`requested_at=completed_at=now`, `period=current_kst_period()`(완료 월)인데:

- Redis 예산 카운터는 `request_period()` — BudgetMiddleware가 **요청 시작** 시각의 월을 ContextVar로 심는다(cost_recorder.py:147).
- `budget_usages.period` 행은 워커가 `e.period` 그대로 씀(완료 월).

→ 월 경계를 넘긴 스트리밍 요청은 **Redis 카운터는 시작 월에서 차감, DB 행은 완료 월에 귀속**. Redis 열화→DB 복원 시 두 소스가 서로 다른 달을 가리켜 복원값이 어긋나고, D-20("요청 시작 월 귀속") 설계 규칙이 DB 측에서 깨진다.

부수: `usage_logs.requested_at`이 실제로는 완료시각이라 `daily_aggregates`(`DATE(requested_at AT TIME ZONE)`) 버킷이 완료일 기준 — 자정을 넘긴 요청이 다음날로 집계. `requested_at` 컬럼명과 실제 의미가 어긋남.

**수정:** finalize 호출부에서 요청 시작 타임스탬프/`request_period()`를 entry에 전달. `requested_at`은 라우터가 보유한 `request_start_time`에서 변환.

---

## F6 (MED) — `endpoint_url` 검증 부재 → ADMIN 설정 가능 SSRF

**파일:** `admin-api/src/app/schemas/models.py:42,96` — `endpoint_url: str | None`에 검증자 없음.
**소비:** `mantle_adapter.py:48`(`POST {endpoint}/v1/messages`), `bedrock_openai_adapter.py`, `openmodel_adapter` 등이 그대로 요청 URL로 사용.

ADMIN이 `http://169.254.169.254/...`나 내부 VPC 서비스를 등록하면 게이트웨이가 사용자 요청 본문을 그대로 POST하고 응답을 되돌린다 — 메타데이터/내부망 도달 경로. ADMIN 전용 입력이라 blast radius는 제한적이지만, (a) 스킴 검증(https 강제), (b) private/loopback/메타데이터 대역 차단, (c) 호스트 화이트리스트(환경별) 중 최소 (a)+(b)는 가치가 있다.

**수정:** 스키마에서 `AnyHttpUrl` + validator로 `https` 강제 + 사설/링크로컬/메타데이터 대역 차단(또는 명시적 allowlist env).

---

## F7 (LOW) — 월 예산은 예약 없이 check-then-deduct

`BudgetMiddleware.check_budget`(budget.py:87)은 요청 전에 카운터만 읽고, 차감은 응답 후 `budget_deduct`(cost_recorder.py:210). CPM/CPH 레이트리밋은 예약 모델인데 월 hard_block 예산은 비예약 — 동시 N개 요청이 각각 한도 미달로 통과해 합계 초과. 배치 추론 클라이언트(동시성 높음)에서 한도 초과분이 그대로 과금·집행됨.

**수정(선택):** 예상 비용 예산 예약(TTL) 또는 "soft 초과 허용" 정책 문서화. hard_block의 의미가 "사후 차단"인지 명확화 필요.

---

## F8 (LOW) — `cost:stream` MAXLEN trim 손실

`COST_STREAM_MAXLEN = 100_000`(cost_recorder.py:21, "~500RPS × 3분 버퍼"). 워커가 장애로 소비를 멈추면 `XADD ... MAXLEN ~`가 가장 오래된 미소비 엔트리를 조용히 잘라낸다 — spool은 XADD **실패**만 커버하고 trim 손실은 감지 불가. 장애 지속 시 usage_logs에 영구 구멍(과금·분석 모두 과소).

**수정:** consumer lag 메트릭/알람(`XINFO GROUPS` pending+lag), trim 임계치 상향 또는 DLQ, 복구 시 lag 초과 경고.

---

## F9 (LOW) — Default Team 팬텀 TEAM 사용량 행

`cost_recorder._publish_to_stream`이 무소속 유저의 `team_id`를 `_DEFAULT_TEAM_ID`(…0003)로 합성(cost_recorder.py:401) → 워커 `_upsert_budget_usages`가 `TEAM/0003` 행에 무소속 사용량을 누적(batch_flusher.py:363). 반면 게이트웨이는 무소속 시 팀 Redis 카운터를 스킵(fc3ea58 수정) → Default Team의 DB 사용량과 Redis 카운터가 비대칭. Default Team에 예산을 걸면 DB 행에는 팬텀 누적이 있으나 Redis 게이트는 다른 값을 본다.

**수정:** 워커도 Default Team 합성 팀의 TEAM 행을 스킵(게이트웨이와 동형), 또는 Default Team을 정식 팀으로 취급해 양쪽 모두 계상(의도 결정 필요).

---

## F10 (LOW) — `daily_aggregates` DO NOTHING 누락

`cost-recorder-worker/src/worker/daily_aggregator.py` `_AGG_SQL`이 `ON CONFLICT (date,user_id,model_alias) DO NOTHING`. 크론(KST 00:10) 이후 스트림 백로그/재처리로 늦게 들어온 어제분 usage_logs는 재실행돼도 합산되지 않아 daily_aggregates에 영구 누락(usage_logs 원본은 정확 → 대시보드 일별 추이가 원장보다 조금 낮게 드리프트).

**수정:** 전체 재집계이므로 `DO UPDATE SET = EXCLUDED.*`로 갱신, 또는 집계 윈도우를 "어제 00:00 ~ 집계 시점"으로 넓히고 재실행 스케줄 추가.

---

## F11 (LOW) — OpenModel 스트림 청크 경계 usage 유실

`openmodel_adapter._sse_stream_gen`이 `response.aiter_bytes()`로 원시 바이트 청크를 넘기고 `streaming.openai_sse_stream._scan_usage`가 청크별 독립 `split("\n")` + `json.loads`. vLLM 최종 usage 프레임이 청크 경계에 걸치면 파싱 실패 → `latest_usage=None` → `_estimate_if_needed`가 output만 tiktoken 추정, **input은 0 기록**(vLLM은 usage를 마지막 프레임에만 실으므로). Mantle은 `aiter_lines`라 안전(비대칭).

**수정:** 청크 간 라인 버퍼 유지(불완전 꼬리를 다음 청크에 이어붙임) — SSE 표준 프레이머로 교체.

---

## F12 (LOW) — converse-stream connect-retry 비대칭

`bedrock_adapter.invoke_stream`: invoke-with-response-stream 분기는 `_call_with_connect_retry`(죽은 풀 연결 재시도)를 쓰는데 converse-stream은 plain `run_in_executor`(bedrock_adapter.py:271). 유휴 후 첫 스트림에서 간헐 ConnectTimeout → 502.

**수정:** converse-stream도 같은 재시도 래퍼 적용.

---

## F13 (LOW) — 알림 pub/sub 영속성 없음

`notifications:budget`은 Redis PUBLISH(fire-and-forget) — notification-worker 다운/재배포 창에 발행된 임계 이벤트는 영구 유실. F3과 짝: 중복 방어도 유실 방어도 없는 양방향 보장 부재.

**수정:** Redis Stream + consumer group으로 전환(재처리·멱등 모두 해결).

---

## 검증 후 기각/양호 판정

- **TEAM_LEADER 교차팀 열람**: `analytics_service._resolve_scope`가 `team:{uuid}≠actor.team_id`→403, `scope=all`+TEAM_LEADER는 본인 팀 강제, 팀 없는 TEAM_LEADER도 403 — 견고.
- **analytics 캐시 유출**: `scope=all && ADMIN`만 캐시, 나머지 `cache_key=None` — 견고.
- **cost-recorder DB 멱등**: `_filter_replays`(txn 내) + `_dedup_in_batch` + `ON CONFLICT(request_id)` + per-row 동일 필터 + XAUTOCLAIM — DB 경로는 견고(카운터/알림이 F1).
- **budget Lua 수치**: soft 경계 곱셈 비교, deduct 최고 교차값, limit=0 차단 — 견고.
- **스트리밍 발화 가드**: `usage_fired`/`complete_fired` 1회 보장, timeout·cancel은 yield 전 확정, `_drain_remaining`, `model_copy` 보존 — 견고.
- **H1 TPM 누수**: `_refund_committed_tpm` + `committed_out` 수정됨(1차 후속).
- **SigV4/cross-account**: (role,region,external_id) 캐시 + Lock + skew 재빌드 — 견고.
- **dev-login prod 게이팅**: base/prod values `false` + Helm NOTES 경고 + 이중 게이트 — fail-closed.
- **scheduler 부분 실패**: 잡별 독립 try/except, ROI upsert 멱등 — 견고.
- **config 기본값**: rl_fail_mode=open(의도), budget/auth fail-closed, trace_mask_pii, body_log 이중 잠금 — 안전 방향.
- **`usage:daily:*` 소비자**: `/v1/usage/me` 오늘분만 Redis, 과거는 DB — 경계 정합.
- **web_search_loop**: 턴별 usage `_merge_usage` 합산 후 `on_usage(merged)` 1회 — 과금 정확(레이트리밋 예약이 검색 다중 호출을 과소추정하는 건 설계 한계, F7과 같은 부류).

## 우선순위 제안

1. **F1** — 재처리 정확성 + F3 완화를 한 곳에서 해결 (worker flush 정렬, 변경 적음)
2. **F2** — 과금 구멍. 미사용이어도 최소 경고 메트릭은 즉시 가치 있음
3. **F5** — 월 경계 불일치 (진입점 1곳 + 스키마 필드)
4. **F3+F13** — 알림 채널 Stream 전환 또는 선점 게이트
5. **F4** — 키 로테이션 운영 절차 전에 수정
6. **F6** — SSRF 하드닝 (배포 전 필터 추가)
7. F7~F12 — 백로그

---

## 수정 상태 (2026-10 구현)

| # | 상태 | 수정 내용 |
|---|------|-----------|
| F1 | ✅ 수정 | `batch_flusher.flush()` — DB/counter/알림 모두 `recorded`(실제 기록된 fresh) 기준으로 정렬; `_flush_per_row` 가 기록된 항목을 반환해 폴백 경로도 동일 적용. 회귀: `test_partial_replay_does_not_double_count_or_republish`, `test_all_replays_skips_counters_and_publish` |
| F2 | ✅ 수정 | `routers/bedrock.py::_scan_bedrock_stream_chunk` — invoke(Anthropic 이벤트: message_start/delta/stop·invocationMetrics)와 converse(`metadata.usage` camelCase) 포맷별 누적 파서. OpenAI-wire 대비 `_try_extract_usage` 폴백 유지. 회귀: `test_bedrock_stream_usage.py` |
| F3 | ✅ 수정 | `notification.notification_logs` 에 `(event_id, event_type, recipient_email)` 유니크 인덱스(마이그레이션 0040, 기존 중복 정리 포함). 핸들러 3d 가 INSERT 로 슬롯 선점 → IntegrityError 시 send 없이 스킵. 회귀: `test_handler_dedup.py` |
| F4 | ✅ 수정 | `auth_service` — 캐시를 `{pem, algorithm}` JSON 으로 저장(레거시 PEM 호환), 캐시 키 불일치·미스 시 **활성 키 전체 순회**해 서명이 맞는 키로 검증 후 그 키를 kid 아래 캐시. 회귀: `test_auth_service` 키 로테이션 케이스 |
| F5 | ✅ 수정 | `periods.py` 에 `period_at`/`date_at` + `request_started_at` ContextVar 추가. BudgetMiddleware 가 시작 시각을 심고 `CostStreamEntry.make(requested_at=…)` 이 시작 시각에서 `requested_at`/`period`/`date` 를 파생 — Redis 카운터(request_period, 시작 월)와 `budget_usages` 행이 같은 버킷. 회귀: `test_make_uses_request_start_for_bucket_labels` |
| F6 | ✅ 수정 | `ModelCreateRequest`/`ModelUpdateRequest` `endpoint_url` 검증 — http(s)만, userinfo/fragment 금지, loopback/link-local(메타데이터 대역)/unspecified/multicast IP 차단. RFC1918·내부 DNS 는 허용(사내 vLLM 정당). 회귀: `TestEndpointUrlValidation` |
| F7 | ⏸️ 보류 | 월 예산 예약-미적용 설계 이슈 — 월 카운터에도 예약 버킷을 도입하는 설계 변경이 필요해 별도 진행 |
| F8 | ⏸️ 보류 | `cost:stream` MAXLEN → 보존 정책/DLQ 설계와 함께 |
| F9 | ⏸️ 보류 | Default Team 팬텀 행 — 팀 귀속 정책 결정 필요 |
| F10 | ✅ 수정 | `daily_aggregator` — 어제 윈도우를 같은 트랜잭션에서 DELETE 후 재집계. GROUP BY 를 충돌키 `(date,user_id,model_alias)` 입도로 정렬(팀 이동 자체충돌 방지), team/dept 는 그날 마지막 요청 소속 귀속. 회귀: `test_daily_aggregator.py` |
| F11 | ✅ 수정 | `openai_sse_stream` — 청크를 `_pending_sse` 바이트 버퍼에 누적해 완결 라인까지만 파싱, 종료/drain/timeout/예외 경로에서 잔여 버퍼 플러시. 회귀: usage 프레임 청크 분할·개행 없는 종단 프레임 테스트 |
| F12 | ✅ 수정 | `converse-stream` 도 `_call_with_connect_retry` 경유 — 연결 수준 오류만 응답 시작 전 1회 재시도. 회귀: real-botocore 스테일 연결 테스트에 converse-stream 추가 |
| F13 | ⏸️ 보류 | pub/sub → Stream/DLQ 전환은 아키텍처 변경 — F3 슬롯 선점이 중복 방지는 해결 |

검증: gateway-proxy 1120 / admin-api 764 / cost-recorder 23 / notification-worker 47 passed.

---

# 라운드 2 (수정 후 재검증 — Devin + kiro-cli claude-opus-5.5)

kiro 리포트: `/tmp/kiro-review-r2.out` (773줄). 5개 병렬 서브리뷰 + 주 리뷰어 교차검증.
**라운드-1 수정사항 검증 결과: F1/F2/F5/F10/F12 구현 모두 정상 확인** (recorded 정렬, 어댑터 이벤트 경계 전달, ContextVar 가시성·오염 없음, DELETE↔SELECT 윈도우 1:1, 재시도 payload 동일 — 전부 로컬 실험으로 확인). 마이그레이션 0040 concurrency-safe.

## 신규 발견 (R2)

| # | 심각도 | 출처 | 요약 |
|---|--------|------|------|
| R2-1 | HIGH | kiro | `endpoint_url` SSRF: 숫자형 호스트(`2130706433`, `0x7f000001`, `127.1`, `localhost.`)가 `ip_address()` ValueError → DNS 취급으로 통과. httpx 실측으로 127.0.0.1 접속 확인 → IMDS 자격증명 절취형 SSRF 성립 |
| R2-2 | HIGH | kiro | IPv6 ULA `fd00:ec2::254`(EC2 IMDS IPv6) 미차단 — `is_private` 미검사라 fc00::/7 통과 |
| R2-3 | MED | kiro | `daily_aggregator` DELETE+재집계가 멀티-replica(prod 3) 크론 동시 실행에서 UniqueViolation — F10 수정의 동시성 회귀. MVCC 롤백으로 데이터 유실은 없으나 매일 N-1 pod 실패 로그 |
| R2-4 | MED | kiro | gateway JWT `jwt.decode`에 `issuer`/`audience` 미전달 — admin-api는 둘 다 검증, 주석의 "같은 전략" 주장 거짓 → 교차 용도 토큰 수용(confused-deputy) 가능 |
| R2-5 | MED | kiro | PyJWT 기본 `verify_aud=True` → `aud` 클레임 있는 정상 IdP 토큰 전부 401 (InvalidAudienceError 로컬 재현). admin-api는 수용 → 서비스 간 상반 판정 |
| R2-6 | MED | kiro | notification 슬롯 INSERT가 IntegrityError 외 예외(일시 DB 장애)면 `continue` 없이 메일 발송 + 로그 0행 → 재배송 시 중복 메일 (F3이 막은 창 재개방) |
| R2-7 | MED | kiro | `GET /admin/budgets/{scope}/{scope_id}/downgrade` — TEAM_LEADER 허용인데 서비스에 `actor` 미전달 → 타 팀/유저 다운그레이드 정책 교차 열람(IDOR). 형제 `get_budget_config`는 스코프 검사 있음 |
| R2-8 | MED | kiro | VK 캐시 히트 경로가 `User.is_active`만 재확인 — 폐기 시 Redis DEL 실패하면 폐기 VK가 TTL(≤300s) 동안 통과 |
| R2-9 | MED | kiro | web_search 루프 입장심사 1회만 — max_iterations 턴 분량 한도 초과 지출 가능(사후 정산, 상한 있음) |
| R2-10 | MED | kiro | ROI 집계가 USER/DEPT 스코프를 쓰지 않음(GLOBAL+TEAM만) — reader 부재로 잠재 결함 |
| R2-11 | MED | kiro | `169.254.169.254.nip.io` 류 DNS→메타데이터 리졸브는 코드로 차단 불가 — egress 네트워크 정책 전제를 문서화 필요 |
| R2-12 | MED | Devin | `_has_1h_cache_control`이 `ttl=="3600"`만 매칭 — Anthropic API는 `"1h"` 리터럴 → **1h 캐시 전부 5m 단가로 저과금**. + 단일 boolean이라 혼합 TTL 시 5m 부분까지 1h 과금(응답의 `cache_creation.ephemeral_5m/1h_input_tokens` 분해 미사용) |
| R2-13 | LOW | kiro | `usage:daily:*:cost` incrbyfloat 누적 → "오늘 비용" 표시값 float 드리프트(예산 enforcement는 Decimal 별도 경로라 무영향) |
| R2-14 | LOW | kiro | dev 토큰(`dev.<b64>.sig`) 무서명 ADMIN 우회 — `DEV_LOGIN_ENABLED=true` 게이트, prod false 확인됨. env 단일 의존 |
| R2-15 | LOW | kiro | JWT algorithm 비대칭 allowlist 미고정 — DB 오설정(HS256+공개PEM) 시 alg-confusion 가능(표준 공격은 키별 단일 alg 고정으로 이미 차단) |
| R2-16~ | LOW | kiro | ContextVar reset 부재(uvicorn 요청별 태스크라 오염 없음, 방어적 reset 권장) / 셧다운 시 drain task GC로 꼬리 usage 유실(best-effort) / 미사용 `stream_response`가 F11 버퍼링 미적용(회귀 함정) / pending 행 영구 잔류(F3 명시 트레이드오프) / `localhost.` trailing-dot(R2-1과 동결함군) / CSRF·Secure 쿠키·OIDC nonce / web_search 무서명 이력 / `team:vk_hashes` EXPIRED 잔존·`rl:config` TTL 의존 / ROI float |

## 기각 확인 (재수정 불요)
- batch_flusher `recorded` 언바운드/빈 insert 경로 없음, per-row 폴백 정합
- Bedrock 스트림: botocore EventStream이 이벤트 단위 전달 → `json.loads` 분할 불가
- ContextVar: 요청별 태스크 격리로 reset 누락 오염 없음, drain task도 컨텍스트 복사로 정상
- `_call_with_connect_retry` 재시도 payload 동일(closure 캡처), ClientError 재시도 안 함
- admin-api text() SQL 24건 전부 바인드 파라미터, analytics 캐시 격리, revoke 무효화 체인 정합
- usage 중복 발화 없음(usage_fired/complete_fired 가드 + 덮어쓰기 카운터)

## R2 수정 상태 (2026-10 구현)

| # | 상태 | 수정 내용 |
|---|------|-----------|
| R2-1/2/11 | ✅ 수정 | `admin-api/schemas/models.py::_validate_endpoint_url` — `socket.getaddrinfo` 리졸브 기반 검증으로 재작성. 숫자형 호스트(2130706433/0x7f000001/127.1/0), trailing-dot `localhost.`, 메타데이터로 리졸브되는 이름(169.254.169.254.nip.io 류) 전부 차단 — 리졸브 결과 IP **전부**에 loopback/link-local/unspecified/multicast + 명시 차단 목록(`fd00:ec2::254` EC2 IMDS IPv6) 적용. 사설 RFC1918(vLLM 내부)은 허용. 잔여 한계: 등록 시점 리졸브와 어댑터 요청 시점이 다르면 DNS rebinding 가능 — 네트워크 egress 정책으로 방어해야 함(코드 주석 문서화). 회귀: `test_model_service.py` 숫자형/DNS 케이스 23건 |
| R2-3 | ✅ 수정 | `daily_aggregator` — DELETE+INSERT 제거, `INSERT … ON CONFLICT (date,user_id,model_alias) DO UPDATE` (어제 윈도우 전체 재집계로 덮어쓰기 — 늦은 행 반영 + replica 안전 둘 다). startup backfill은 `DO NOTHING` 유지(빈 테이블 전용 의도). 회귀: `test_daily_aggregator.py` 4건 |
| R2-4/5/15 | ✅ 수정 | `auth_service._decode_jwt` 헬퍼 신설 — `issuer`/`audience` 를 DB 레코드에서 전달(audience 공란 시 `verify_aud=False` 명시로 PyJWT 기본값 함정 제거). 캐시 레코드 `{pem,algorithm,issuer,audience}` 로 확장, 레거시 PEM/iss-aud-없는 JSON 은 DB 재조회로 갱신. 비대칭 alg allowlist(RS/ES/PS) — admin-api `JWTVerifier.load_configs` 에도 동일 allowlist 적용. 회귀: `test_auth_service` JWT 케이스 |
| R2-6 | ✅ 수정 | notification 슬롯 INSERT — IntegrityError=중복 스킵(기존), **그 외 예외는 1회 재시도 후 슬롯 재확보**, 그래도 실패하면 경고+메트릭 남기고 **발송 우선**(유실 < 중복, 운영 결정). 회귀: `test_handler_dedup` transient/persistent 케이스 |
| R2-7 | ✅ 수정 | `budgets.get_downgrade_config` 에 `actor` 추가 — TEAM_LEADER 는 자기 팀(TEAM) / 자기 팀 멤버(USER, UserRepository 조회)만. `get_budget_config` 와 동일 스코핑. 회귀: `test_budget_service` IDOR 케이스 |
| R2-8 | ✅ 수정 | VK 캐시 히트 시 `key:vk:{hash}` 존재 확인 추가 — 매핑 부재(폐기 DEL 성공·캐시 DEL 실패 케이스)면 캐시 제거 후 거절. `is_active` 재확인 유지. 회귀: 캐시 히트/폐기/비활성 경로 |
| R2-9 | ✅ 수정(보수적 예약) | web_search 입장심사용 body 의 max_tokens 를 `× web_search_max_iterations` 로 선반영(상류 바디와 무관한 심사 복사본) — 정산 시 settle 이 차액 환불. 턴 경계 재심사는 더 침습적인 설계 변경으로 별도 보류 |
| R2-10 | ✅ 문서화 | `roi_aggregator.aggregate_usage` docstring 에 "GLOBAL+TEAM 만 기록, USER/DEPT 는 소비자가 생길 때 함께 추가" 계약 명시 — 지원 주장과 실제 행의 괴리 해소 |
| R2-12 | ✅ 수정 | `TokenUsage.cache_creation_1h_input_tokens` 추가 + `calculate_cost` 가 5m/1h 부분을 분리 과금(분해 없는 프로바이더는 요청 신호 폴백). `_has_1h_cache_control` 이 `"1h"`/`"3600"` 모두 인식. Anthropic streaming/비스트림·Bedrock invoke/converse·web_search 누적 전 경로 배선. 회귀: `test_calculate_cost_1h_*` 3건 |
| R2-13 | ⏸️ 문서화 | `usage:daily:*` incrbyfloat 드리프트 — 표시 전용(예산 집행은 Decimal 경로). 정수 micro-USD 전환은 기존 float 키와 비호환(INCRBY 실패)이라 별도 마이그레이션으로 |
| R2-14 | ✅ 수정 | admin-api `Settings.is_production` 신설(`prod`/`production` 모두 수용 — Helm values 가 `prod` 인데 코드가 `"production"` 과 비교해 internal 디버그 가드가 무력화된 **사전 버그도 함께 수정**) + `_parse_dev_token` 에 prod 이중 가드 |
| LOW | ✅ 수정 | ContextVar 토큰 기반 reset(BudgetMiddleware finally) / 미사용 `stream_response` 삭제(F11 미적용 회귀 함정 제거) / admin-ui OIDC `nonce` 바인딩(login→콜백 id_token 검증) + `/api/` 변형 메서드 Origin/Referer CSRF 차단 + `SECURE_COOKIES` 강제 스위치(lib/cookies.ts) |
| LOW 보류 | ⏸️ | `team:vk_hashes` EXPIRED 잔존 정리 / `rl:config` TTL / 셧다운 drain task GC / pending 알림 행 리퍼 / ROI float → 모두 명시 트레이드오프 또는 별도 작업 |

검증: gateway-proxy **1128** / admin-api **774** / cost-recorder **23** / notification-worker **48** / admin-ui **388** passed (전부 증가분 포함).
