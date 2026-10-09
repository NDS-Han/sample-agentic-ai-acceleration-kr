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

---

# 라운드 3 (재검증 — Devin + kiro-cli claude-opus-5.5)

kiro: 4개 서브리뷰 리포트 완결(`/tmp/r3-partA1/A2/B1/B2.md`, 로컬 실험 증거 `/tmp/r3scratch/`).
주 에이전트가 자체 교차검증 수행 중 세션 종료 — 최종 합성 파일 미작성이나 서브리포트는 완결,
미검증 잔여분(chat-proxy traversal end-to-end, /internal 노출)은 Devin이 **라이브 dev 배포로
직접 확정**.

## 신규 발견 (R3)

| # | 심각도 | 출처 | 요약 |
|---|--------|------|------|
| R3-1 | **HIGH** | Devin+kiro | **`/internal/*` 무인증 + 공개 노출.** `POST /internal/cache/retry` 가 모든 환경에서 무인증·무 prod 게이트(dev 라이브 200 실측). `POST /internal/test/issue-key` 는 `is_production` 게이트뿐이라 **dev 공개 admin-api 에서 무인증 VK 발급**(라이브로 `probe@example.com` VK 실제 발급·정리 완료). admin-api ingress 가 전 경로 통과 |
| R3-2 | **HIGH** | kiro→Devin | **chat-proxy 경로 traversal end-to-end 확정.** `POST /api/chat-proxy/admin/chat/%2e%2e/%2e%2e/internal/cache/retry` → admin-api `/internal/cache/retry` 도달(200). 인증 경로도 도달(`/admin/users/search` → admin-api 401 봉투). 로그인 사용자의 `admin_jwt` 가 임의 admin-api 경로로 전달됨 — 무인증 `/internal/*` 와 결합 시 dev 에서 무인증 VK 발급 경로가 admin-ui origin 으로도 열림 |
| R3-3 | MED | kiro | **Cognito 비활성화가 VK를 폐기하지 않음.** `deactivate_missing_oidc_users`/`_deactivate_missing_user` 는 `is_active=False` 만 — `revoke_key`/`key:vk` DEL/`key_revoked` 이벤트 없음. 차단은 게이트웨이 per-request `is_active` 재확인에 전적으로 의존(DB 열화 시 캐시 경로로 ≤TTL 통과). 재활성화 시 옛 VK 그대로 부활 |
| R3-4 | MED | Devin | **Cognito 부분 실패 → 대량 비활성화.** `sync_all` 의 그룹 멤버 조회 실패는 `result.errors` + `continue` 만 — 해당 그룹 멤버가 `seen_subjects` 에 빠진 채 4단계 `deactivate_missing_oidc_users` 실행 → Cognito 에 살아있는 유저 대량 비활성. 극단적으로 멤버 조회가 전부 실패하면 **OIDC 유저 전원 비활성화**. 다음 성공 sync 에서 `is_active` 복원되지만 그 사이 인증 전면 중단 |
| R3-5 | MED | kiro | **codex web_search 경로 보수 예약 미적용 (R2-9 불완전).** `/v1/responses`·`/v1/chat/*` 는 `enforce_rate_limits` 가 web_search 분기 **전에** 미부풀린 body 로 실행 → 최대 `web_search_max_iterations`(기본 2) 배 초과 지출 창이 codex 경로에 그대로 |
| R3-6 | MED | kiro | **OIDC unknown-kid JWKS 강제 리페치 증폭.** `/v1/auth/exchange` 무인증 + 공격자가 임의 kid 제어 → 요청마다 IdP JWKS GET(negative cache 없음) — admin-api outbound + IdP 양쪽 DoS 벡터 |
| R3-7 | MED | kiro | **client 헤더 스푸핑 → 크로스어카운트 라우팅 + per-app 예산 우회.** `anthropic-client-platform: desktop_app` 하나로 cowork 프로파일(Rule A → cowork AWS 어카운트 역할) 강제 + `budget:user:{u}:{client}` 버킷 회피. `allowed_clients=None` 기본 ACL에서 유효 |
| R3-8 | MED | Devin | **team allowed-models 캐시 무효화가 커밋 전.** `set_for_team`/`clear_for_team` 이 서비스 내부에서 즉시 DEL — CommittingRoute 커밋 전 윈도에 게이트웨이가 구 정책으로 `key:cache:vk` 재생성 → ≤300s 정책 롤백. user 경로는 post-commit 으로 수정됐는데 team 경로는 누락(MF3 회귀) |
| R3-9 | LOW/MED | kiro | **Cognito sync 상호배제 부재 + 빈 email 충돌.** advisory lock 없음 — 동시 sync 가 `teams` 유니크에서 충돌/전체 롤백 가능. `sync_user`/`sync_group` 은 per-user savepoint 없어 email UNIQUE 충돌(빈 email)이 트랜잭션 오염 |
| R3-10 | LOW/MED | kiro | **Redis-down 인메모리 RPM 폴백이 fleet 한도를 pod 수만큼 증폭.** `rl_fallback_replicas` 기본 1 → pods×workers 각자 `limit/workers` 허용 → 합계 `limit×pods` (HPA 10~30배 초과) |
| R3-11 | LOW/MED | kiro→Devin | **poison-only 배치 PEL 누수.** `_consume_live` — `batch_entries` 비면 `xack` 없이 continue → 파싱 실패 ID 가 이 consumer PEL 에 잔류(`>` 신규만 읽으므로 재독 없음). 단일 레플리카 dev 에서 재시작까지 누적, MAXLEN 압박 |
| R3-12 | LOW | 양쪽 | **A2-2** 고아 IDOR(TEAM_LEADER `team_id=None` ↔ 대상 user `team_id=None` → `None!=None` False 로 통과) / **A2-3** `/model/*` converse 경로 `cache_ttl_1h` 미설정 → 1h 캐시 5m 단가 저과금(네이티브 경로만, 현 트래픽 미사용) / **A2-4** 분해 응답이 1h=0 을 명시해도 요청측 ttl=1h 폴백이 전부 1h 과금(삼값 상태 필요) / **A1-1** `::127.0.0.1`·NAT64·6to4 형태 validator 통과(심화 방어) / **A3-1** attacker kid → `key:cache:jwt:{kid}` 무제한 증가(서명 통과 후라 실질 낮음) / **A2-7** `APP_ENV=="development"` 비교는 배포값 `dev` 와 불일치로 CORS `*` 미발동(fail-safe) / **A2-8** prod values 에 `SECURE_COOKIES` 미배선 / **B1-4** `_refund_committed_cost` 시그니처 2-tuple 오표기 / **B1-5** RPM phantom +1 미환불(비대칭, 자기교정) / **B2-5** pub/sub fan-out + send-anyway 폴백의 레플리카 증폭(명시 트레이드오프) / **A4-1** dedup 테스트가 mock session 이라 실 ORM 상태 미커버 / **Devin** 복수 팀 리더가 analytics 에서 자기 `team_id` 팀만 조회(리더십 그래프 미반영) / **A2-5/A2-6** VK 매핑 user_id 미대조·폐기가 Redis DEL 성공에 전적 의존(INFO) |

## 기각 확인 (R3, 양쪽 교차검증)

- **R2 수정 전부 정상**: SSRF 리졸브 검증(숫자형/mapped/nip.io/multi-IP 전부 차단, create·update 양쪽 적용), daily_aggregator UPSERT(40×3-way 동시실행 데드락·이중계상 없음, GROUP BY=충돌키 구조적 일치), JWT iss/aud/alg(gateway·admin-api 동일 계약, aud 리스트·레거시 캐시·키 로테이션 모두 정상), notification 슬롯 재시도(실 SQLAlchemy+asyncpg로 transient/persistent/lost-ack 전 시나리오 정합), ContextVar 순수 ASGI 확인(스트림 완료 후 reset, drain 태스크는 생성 시점 컨텍스트 복사), admin-ui nonce/CSRF(84 테스트 통과, Host 비교 방식 ALB 안전), `is_production` 정규화(internal 가드 실효 확인)
- TPM/cost 예약-정산: 롤오버 환불이 reserved 버킷에 적중(음수 없음), 부분 거절 시 커밋 scope 만 환불, Lua 단일슬롯 원자성
- VK 무차별대입 불가(256bit·sha256 조회·캐시미스 시 DB 미개방), 한 유저 ACTIVE VK 1개(유니크 인덱스+CTE), 폐기 체인(R2-8 + is_active) 정합
- body 로깅: 헤더 미수집(VK 유출 없음), 런타임 플래그 기본 OFF + admin-only 토글 + 감사행
- analytics 격리: TEAM_LEADER 자기 팀 강제 + 캐시가 ADMIN+all 에만, rate-limits 조회는 admin-only
- scheduler: Recreate + replicas:1 — 이중 실행 리스크 없음. ROI upsert 멱등
- XAUTOCLAIM: min_idle 5m + 재처리 필터 이중방어, BUSYGROUP 정상, consumer 이름=pod 이름

## R3 수정 제안 (우선순위)

1. **R3-1/R3-2** — `/internal/*` 에 인증 또는 prod/dev 모두 명시 게이트 + chat-proxy 세그먼트 화이트리스트(`admin`/`chat` prefix 고정, `..`·`%`·`\` 거부)
2. **R3-3/R3-4/R3-9** — Cognito offboarding 시 VK 폐기 + 부분 실패 시 reconcile 스킵 + advisory lock/개별 savepoint
3. **R3-5** — openai_compat 에도 보수 예약 헬퍼 공용화 적용
4. **R3-6** — unknown-kid negative cache(30s) 또는 force-refresh 쓰로틀
5. **R3-7** — 크로스어카운트/예산 경계에서 client 를 신뢰 신호로 교체(VK 바인딩) — 최소 문서화
6. **R3-8** — team allowed-models 무효화를 커밋 후로 이동(user 경로 MF3와 동일)
7. **R3-10/R3-11 + LOW** — fallback replicas 현실값/문서화, poison 배치 xack, 잔여 LOW 일괄

## R3 구현 상태

| # | 상태 | 수정 내용 |
|---|------|-----------|
| R3-1 | ✅ | `/internal/cache/retry` → `require_admin`; `scheduler/run`·`test/issue-key` → `X-Internal-Token` 공유시크릿(`INTERNAL_API_TOKEN`, 미설정 시 fail-closed, hmac.compare_digest). Helm `-app` Secret `internal_api_token` optional 참조. 통합테스트/smoke-test/문서 갱신. `test_internal_token_gate` 3건 |
| R3-2 | ✅ | chat-proxy 세그먼트 화이트리스트 — `admin/chat/<sub>` 구조 + `..`/`.`/`%`·`\`·빈 세그먼트 거부(traversal·이중인코딩 차단). `chatProxyPathWhitelist` 3건 |
| R3-3 | ✅ | `revoke_keys_for_users` 벌크 폐기(VK+hash 쌍 수집으로 decrypt 실패 정렬오류 방지) + cognito 비활성화 경로 연결. `test_key_service_offboard` 2건 |
| R3-4 | ✅ | `reconcile_incomplete` 플래그 — 멤버/전체 유저 조회 실패 시 4단계 비활성화 스킵 |
| R3-5 | ✅ | `web_search_loop.reserve_admission_output` 공용화 — `/v1/responses`·`/v1/chat`·Anthropic 모두 `max_output × max_iterations` 예약, 정산 시 차액 환불 |
| R3-6 | ✅ | unknown-kid 강제 리페치 최소 30s 간격(asyncio.Lock + in-lock double-check 유지). `test_oidc_unknown_kid_throttle` |
| R3-7 | 📝 문서화 | `client_identifier.py` 위협모델 주석 — client 헤더는 스푸핑 가능 힌트이며 신뢰 신호가 아님. VK 바인딩 전환은 설계 변경으로 별도 과제 |
| R3-8 | ✅ | `set_for_team`/`clear_for_team` 무효화를 서비스에서 제거 → 라우터가 commit 후 `invalidate_for_team` 호출(CommittingRoute 패턴, allowed-clients와 동일) |
| R3-9 | ✅ | `pg_try_advisory_lock` 상호배제(커넥션 고정 + finally unlock), reconcile savepoint, `deactivate_missing_oidc_users` RETURNING 으로 대상 유저 회수 → VK 폐기 연결 |
| R3-10 | ✅ | `rl_fallback_replicas` 주석 정확화 — Helm 이 minReplicas 주입 중임을 반영 + HPA 스케일아웃 잔여 한계 문서화 |
| R3-11 | ✅ | poison-only 배치 xack — PEL 잔류/무한 재실패 루프 해소. `test_poison_ack` |
| A2-2 | ✅ | TEAM_LEADER `team_id=None` fail-closed — budget_service 7곳 + analytics/downgrade 등 `actor.team_id is None` 가드 추가 |
| A2-3 | ✅ | `/model/*` converse 경로 `cache_ttl_1h` 요청 신호 전파 |
| A2-4 | ✅ | `cache_creation_1h_input_tokens` 삼값(None=미보고/0=보고됨) — 분해 보고된 응답에 요청 플래그 폴백 적용 안 함 |
| A1-1 | ✅ | IPv4-compatible/mapped/NAT64/6to4 언래핑 후 임베디드 IPv4 차단 |
| A3-1 | ✅ | JWT kid 문자열 검증(길이·문자셋) + sha256 캐시키 바인딩 |
| A2-7 | ✅ | `is_development` 정규화 + CORS 는 명시 `CORS_ALLOW_ORIGINS` 목록만(기본 빈 목록) |
| A2-8 | ✅ | prod values `SECURE_COOKIES` 배선 |
| B1-4 | ✅ | `_refund_committed_cost` 시그니처 주석 정정 |

검증: gateway-proxy **1128** / admin-api **780** / cost-recorder **24** / notification-worker **48** / admin-ui **391** passed, `tsc --noEmit` 클린.

### 구현 중 발견·수정된 자체 버그

- `revoke_keys_for_users` 의 `zip(vks, hashes)` 정렬 오류 — decrypt 실패 시 쌍 어긋남 → (key,hash) 쌍 수집으로 교정
- `_sync_all_locked` 의 `user_pool_id` F821 — 리팩터 시 정의 누락 → 재추가
- advisory lock 누수 경로 — `pg_try_advisory_lock`(세션-레벨)이 내부 commit 시 커넥션 풀 반환으로 락 잔류 → `session.connection()` 고정 + finally `rollback()` 후 unlock
- NAT64 프리픽스 상수 64비트 시프트 누락 → `0x64FF9B00000000 << 64` 정정 후 형태별 차단/허용 재검증

### R3 잔여 보류

- **R3-7 실수정**(client → VK 바인딩): 정책 결정 필요, 문서화로 마무리
- **B1-5** RPM phantom +1 / **B2-5** pub-sub 증폭 / **A4-1** mock-session 커버리지 / **A2-5·A2-6** INFO 항목 — 명시 트레이드오프 또는 저위험으로 보류

## 라운드-4 (2026-10): kiro 5개 서브리뷰 + 내 감사 — R3 수정분의 재검증 포함

kiro(claude-opus-5.5) 서브리포트 5건: `/tmp/r4-partA1.md`(auth/프록시·내부표면), `/tmp/r4-partA2.md`(OIDC·Cognito·세션), `/tmp/r4-partA3.md`(스키마·검증), `/tmp/r4-partB1.md`(게이트웨이·스트리밍·회계), `/tmp/r4-partB2.md`(예산·레이트리밋·집계·캐시). 실험 증거 `/tmp/r4scratch/partA*/`. 최종 합성 파일은 미작성(세션 종료)이나 5개 리포트는 완결.

### R4 신규 발견 → 수정 상태

| # | 심각도 | 내용 | 상태 |
|---|--------|------|------|
| R4A1-1 | HIGH | **chat-proxy R3-2 우회**: Next.js 가 `%2f` 를 세그먼트 내부 `/` 로 디코딩(`..%2finternal`→`../internal`) → 세그먼트 검사 통과 → `join('/')`+URL 정규화로 탈출 | ✅ 세그먼트 내 `/` 거부 + `buildTargetUrl` 심층방어(정규화 경로가 `/admin/chat/` 밖이면 400). 회귀 테스트 3케이스 추가 |
| R4A1-2 | HIGH | `/internal/productivity`·`/webhooks/git` 이 별도 라우터라 R3-1 게이트 누락 → 무인증 DB 쓰기 | ✅ productivity → `_require_internal_token`, git webhook → `X-Hub-Signature-256` HMAC(`compare_digest`, 미설정 403). `GITHUB_WEBHOOK_SECRET` 설정+Helm optional 참조+ESO 조건부 매핑(`adminApi.githubWebhook.enabled`). 테스트 5건 |
| R4-A2-1 | HIGH | Cognito "성공+0건"(IAM 드리프트·잘못된 pool id·페이지네이션 결함)은 `reconcile_incomplete` 가 안 잡음 → 빈 seen 으로 OIDC 유저 전원 비활성화+VK 폐기 | ✅ `user_map` 공집합이면 reconcile 불완전 취급·비활성화 스킵+errors 기록 |
| R4-A2-2 | HIGH | R3-9 의 advisory lock 실측 결함 — `session.connection()` 은 commit 간 고정이 아니고, 첫 commit 시 커넥션이 풀에 checkin(세션락 잔류·상호배제 무력화·무관 요청이 락 상속) | ✅ 엔진 바인딩 세션은 **전용 AsyncConnection + `pg_try_advisory_xact_lock`**(tx 종료 시 자동 해제, 풀 잔류 경로 없음), 커넥션 바인딩 세션(테스트)은 세션락 유지, 비-PG/mock 은 잠금 없이 진행 |
| R4-A2-3/B2-3/B2-4 | MED | model/budget/rate-limit/downgrade/transfer/revoke 경로에 pre-commit 캐시 무효화 잔여 — DEL→commit 창에 구 정책이 ≤TTL 재캐시 | ✅ `invalidate_after_commit` 헬퍼 신설(별도 세션 실패기록 유지) + budget 13곳·rate-limit(SET/DELETE/pattern)·model 6곳·user_team swap·key revoke 전부 전환. `cli_service` warm-SET 도 지연 |
| — | MED(자체 발견) | **`after_commit` 이 SAVEPOINT release/rollback 에도 발화** — nested commit 에서 지연 쓰기가 조기 실행되면 같은 레이스 재도입 | ✅ `in_nested_transaction()` 가드(outer commit 시점엔 False, nested 이벤트엔 True — 실측). `in_transaction()` 은 outer after_commit 에도 True 라 구분 불가. 회귀 테스트 1건 |
| R4-A2-4 | MED | unknown-kid 리페치: 마지막 **성공** 시각 기준 쓰로틀이라 IdP 장애 시 30s 경과 후 모든 임의 kid 가 JWKS GET 증폭 | ✅ 시도 시각(`_jwks_force_attempt_at`) 기준 쓰로틀로 negative-cache 효과 — 실패해도 30s 내 재시도 안 함, fetch 실패는 stale JWKS + 401 로 수렴(OIDCConfigError 삼킴). 테스트 확장 |
| R4-B2-1 | MED | ROI `upsert_aggregation` SELECT→INSERT 레이스(UniqueViolation → 배치 롤백). **scope_id NULL(GLOBAL)은 유니크 인덱스가 못 잡아 ON CONFLICT 도 무력** | ✅ 키별 `pg_advisory_xact_lock(hashtextextended)` 직렬화 — 락 대기 후 SELECT 라 커밋된 행을 봄(READ COMMITTED). NULL 커버·마이그레이션 불필요. 테스트 3건 |
| R4 MED | MED | downgrade `to_model_alias` 가 팀 allowed-models 밖이면 강등 요청이 모델 게이트 403 | ✅ 저장 시점 검증(허용 목록 있는 팀만 적용 — 빈 목록=제한없음). 회귀 테스트 1건 |
| R4 LOW | LOW | SSRF 잔여: NAT64 local-use `64:ff9b:1::/48`(RFC 8215), Teredo `2001::/32` 임베디드 IPv4 | ✅ local-use 블록 전체 차단, Teredo client IPv4(low32 bit-flip) 언래핑 차단. 실측 검증 |
| R4 LOW | LOW | CORS CSV 크래시 위험, README migration head 불일치(`0028`→`0040`), srem 실패 미기록 | ✅ `CORS_ALLOW_ORIGINS` `_split_csv`+`NoDecode`, README 정정, transfer swap 실패기록은 전용 세션으로 durability 복원 |

### R4 검증

- admin-api **791** passed(57 skip) / admin-ui **391** + tsc 클린 / 신규 테스트: defer savepoint 1, webhook/internal 게이트 5, ROI lock 3, downgrade allowed-models 1, chat-proxy %2f 케이스
- SQLAlchemy 이벤트 실측: `after_commit`/`after_rollback` 이 savepoint 에도 발화 + `in_nested_transaction()` 구분자 확인(sqlite), advisory lock 커넥션 반환 실측(checkin 이벤트)

### R4 잔여 보류

- `/model/*` raw Bedrock 스트림 drain 비대칭(조기 단절 시 usage 과소기록) — 스트리밍 재설계급, 별도 과제
- ESO 시크릿 로테이션 시 pod 재시작 미전파 — 운영 절차(rollout restart)로 커버
- daily 집계의 늦은 도착 행 영구 누락(yesterday 창만 처리) — 백필 잡 설계 필요
- `sync_user` 단건 savepoint — 단건 upsert 직후 commit 이라 이점 미미
- R3-7 client→VK 바인딩 실수정 — 정책 결정 필요(문서화 유지)
