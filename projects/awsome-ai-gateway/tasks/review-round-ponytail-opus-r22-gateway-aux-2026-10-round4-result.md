# R22 라운드 4 재검증 — gateway-proxy 보조 서비스군

범위: 작업 트리(미커밋 32파일 + `credential_backoff.py`, `test_r22_round3_regressions.py`).
r3 수정 8건 + r1~r2 누적 변경(budget lua 4종·pendingidx·sentinel, CB hash tag·probe token,
credential backoff, body_logger 소진·BoundedCapture, 예약 초과 reason) 재검증.
가정 부하: README 기준 HPA 최대 30 pod × 4 worker, 수천 동시 SSE.

## Verdict: **NOT CONVERGED** (M0 + S2 + N5)

r3 수정 8건은 전부 의도대로 반영됐고 새 회귀는 없다. 다만 r1~r3에서 쌓은 probe-close 설계를
무력화하는 기존 Lua 결함(S1)이 남아 있고, settle 4/5키 분기가 두 군데에 복붙돼 있다(S2).

## 검증 근거

- `pytest tests` → **1434 passed, 60 skipped** (r3 보고와 동일). 실 Redis/PG 증명 테스트는 env 미설정이라 skip.
- fakeredis+lupa로 실제 Lua 실행 (스크래치 스크립트, 실행 후 삭제):
  - CB: 토큰 없는 성공 → open 유지 / 토큰 성공 → open+halfopen 삭제 ✅
  - budget: 월 첫 reserve 센티넬 → rebuild는 no-op이고 센티넬만 해제(`5`) ✅ · 키 유실 후 reserve → rebuild `db30 + live10 = 40`, resvsum `10` ✅ · 재실행 시 `0` 반환, 이중가산 없음 ✅ · 앱 키 부재 rebuild → `0` 생성, 다음 요청부터 재트리거 없음 ✅
  - **CB 프로브 close 직후의 일반 성공 → `opened=True`, `is_open=True`** ❌ (S1)
- `ruff F401/F811/F841/F821`: 변경 파일의 지적은 전부 HEAD에도 있던 것(`openai_compat json`, `agentcore field`, `health_checker Decimal`)이다. 이번 diff가 만든 미사용 import는 없다.

## r3 수정 정합 확인

| # | 항목 | 결과 | 근거 |
|---|---|---|---|
| 1 | `_ws_cb_record` 토큰 소비 | ✅ | `messages.py:715`, `openai_compat.py:940` — `_token, _ws_probe_token = _ws_probe_token, None` 뒤에 `record_success(probe_token=_token)`. 성공 뒤 release 가드(`messages.py:873,878`)는 토큰이 None이라 지나가고, 슬롯은 `record_success` 가 이미 지웠다 |
| 2 | 앱 키 단독 유실 트리거 | ✅ | `budget_service.py:402-428` — 유저 트리거가 False여도 `client in PER_APP_BUDGET_CLIENTS` 이면 앱 키의 부재·음수·`:lost` 를 검사한다. 모든 `float()` 가 `db is not None` 단락평가 뒤에 있어 db=None 계약을 지킨다. 팀도 `:564-568` 에서 같은 구조 |
| 3 | 웜업 `since` 무기한 | ✅ | `budget_service.py:247-261` — `set(..., nx=True)`, ex 없음 |
| 4 | `record_success` 조건부 DEL | ✅ | `circuit_breaker.py:96-104` — 토큰이 있을 때만 eval. open은 무조건 DEL, halfopen은 GET==token일 때만 DEL. 키 2개가 같은 `{pmid}` 슬롯 |
| 5 | BoundedCapture + CancelledError | ✅ | `body_log_records.py:304-318` — GeneratorExit·CancelledError 둘 다 `_detach_close_and_fire`. `fired` 가드로 이중 발화 없음. 스트림 3헬퍼(`streaming.py:319,615,847`)와 `bedrock.py:303-304` 모두 3인자 콜백 |
| 6 | body_log_flag 이중 인코딩 | ✅ | `body_log_flag.py:107-125` — 파싱 결과가 str이면 `_truthy`, 파싱 실패는 default(False)로 fail-safe |
| 7 | MCP `_session_gen` | ✅ (잔여 N2) | `agentcore_mcp_client.py:358,387,403` |
| 8 | 503 Retry-After / `:stale` / 상수 | ✅ | `middleware/budget.py:148-149`, `rate_limit_enforcement.py:541-548`, `routing_profile_loader.py:85-91`. `_FAILURE_BACKOFF_S` 잔존 0건 |

r1~r2 누적분도 재확인했다. 결함은 S1 외에 없다.
- floor rebuild: `budget_rebuild.lua:46-82`
- pendingidx: reserve HSET은 `reserve.lua:126-131`, settle HDEL은 `settle.lua:82-84`. 마커 prefix + 필드 = 마커 키로 일치하고, client 필드 `"<client>:"` prefix 필터도 일치
- sentinel 3종: reserve `:134`, settle `:70-73`, deduct `:47-49`
- 4/5키 롤링 호환: `cost_recorder.py:30-47`, `budget_service.py:964-977`
- credential single-flight: `shield` + 실패 TTL 분류. STS `BotoConfig` 타임아웃(`main.py:189-196`)이 `ConnectTimeoutError/ReadTimeoutError` 를 내서 transient 2s로 분류된다
- body_logger 소진 루프: `body_logger.py:144-146`. 0 반환 시 break라 Firehose 전면 실패에서도 공회전하지 않는다

---

## Should fix

1. **프로브로 닫은 회로를 다음 "성공"이 다시 연다** (`redis_scripts/circuit_breaker.lua:39`)
   - **What this is:** 회로 차단기(circuit breaker)는 30초 창의 실패율이 50%를 넘으면 회로를 연다. half-open 프로브(회로가 열린 동안 회복을 확인하려고 한 요청만 통과시키는 것)가 성공하면 `record_success(probe_token=)` 가 회로를 일찍 닫는다(r1 M4 / r2 N1 / r3 S1).
   - **Problem:** Lua의 열기 판정은 `is_failure` 와 상관없이 "창 안 실패율 ≥ 임계"만 본다. 프로브 close는 `:open` 만 지우고 초당 버킷은 남긴다. 그래서 close 직후 첫 일반 요청이 성공해도 옛 실패 이력 때문에 `SET open` 이 된다. 재현: 실패 10회 → open → 프로브 성공 → closed → 일반 성공 1회 → `opened=True, is_open=True` (fakeredis 실측). 버킷이 사라질 때까지(≈window 30s) 프로브 회복이 효과가 없다. 그동안 원본 모델은 건너뛰어지고, 폴백 체인이 없으면 503(all-open)이 난다. `record_success` 의 반환값도 "opened"로 거짓이 된다.
   - **Fix:** 열기 판정을 실패 기록일 때만 하도록 한 줄 바꾼다. `if is_failure == 1 and total >= min_calls and (fails / total) >= rate_thr then`. 성공은 실패율을 내리기만 하므로 성공이 회로를 열 이유가 없다. 원하면 close eval에서 창 버킷까지 지워 "close 직후 실패 1회로 즉시 재오픈"도 없앨 수 있지만, 그 동작은 표준 half-open 의미라 필수는 아니다.
   - **If we skip it:** r1~r3에서 쌓은 프로브 토큰·close 로직이 실질적으로 죽은 코드가 된다. 장애 회복이 프로브 기준(수 초)이 아니라 버킷 만료 기준(≈30–40s)으로 늘어진다.

2. **settle 4/5키 분기 복붙** (`services/budget_service.py:961-977` vs `services/cost_recorder.py:30-47`)
   - **What this is:** 롤링 배포 창에서 구버전 스펙(idx 키 없음)은 4키로, 신버전은 5키로 `budget_settle.lua` 를 부른다.
   - **Problem:** 같은 분기가 `cost_recorder._settle_args` 헬퍼와 `BudgetService.settle_budget` 인라인 if/else 두 곳에 있다. 인자 순서(ARGV[3]=idx_field)가 바뀌면 두 곳을 같이 고쳐야 한다. 한쪽만 고치면 그 경로의 인덱스 HDEL이 조용히 빠진다. 고아 필드는 rebuild가 청소하므로 정합은 남지만, 인덱스가 계속 부푼다.
   - **Fix:** `_settle_args` 를 `budget_service` 로 옮기고(또는 import해서) `settle_budget` 에서 `redis.eval(script, *_settle_args(s), str(actual), s.get("fallback") or "", s.get("idx_field") or "")` 한 줄로 쓴다. 4키 경로에 ARGV[3]="" 을 넘겨도 Lua는 `KEYS[5]` nil로 무시한다(cost_recorder가 이미 그렇게 호출 중).
   - **If we skip it:** 다음 settle 계약 변경 때 한쪽만 갱신되는 회귀 위험이 남는다. Lean: −12줄.

## Nice to have

3. **check 핫패스 Redis 왕복 6회** (`budget_service.py:403-428, 564-568`)
   - **What this is:** 요청마다 유저 키·`:lost`, 앱 키·`:lost`, 팀 키·`:lost` 를 각각 GET/EXISTS한다.
   - **Problem:** 순차 왕복 6회(이전 2회)라 요청당 수 ms가 늘어난다. 유저 키와 앱 키는 같은 `{uid}` 태그다.
   - **Fix:** `MGET user_key, user_key:lost, app_key, app_key:lost` 1회(같은 슬롯), 팀은 `MGET team_key, team_key:lost` 1회. `:lost` 는 값 `'1'` 존재 여부로 판정한다.
   - **If we skip it:** 기능 문제는 없고 지연만 조금 늘어난다.

4. **늦은 응답이 새 MCP 세션 id를 덮어쓴다** (`agentcore_mcp_client.py:223-225`)
   - **What this is:** r3 N8 세대 가드는 `_initialized` 리셋만 막는다.
   - **Problem:** 세션 id 캡처는 세대 검사 없이 모든 응답에서 한다. 재핸드셰이크(gen 2) 뒤에 도착한 gen 1 호출의 응답이 `Mcp-Session-Id` 를 되돌려주면 옛 id로 덮어쓴다. 다음 호출이 404 → 리셋 → 재핸드셰이크로 1회 헛돈다. AgentCore가 모든 응답에 이 헤더를 에코하는지는 확인하지 못했다.
   - **Fix:** 캡처를 `method == "initialize"` 일 때로 한정한다(MCP 규격상 서버가 세션 id를 주는 시점).
   - **If we skip it:** 드물게 웹서치 1회가 실패한다. 스스로 회복된다.

5. **Redis 플러시 후 웜업 재시작 = 복구 폭풍 중 키스페이스 SCAN** (`budget_service.py:247-259`)
   - **What this is:** `since` 가 없으면 4h 웜업을 시작하고, 그동안 모든 rebuild가 전체 SCAN을 한다.
   - **Problem:** 주석은 플러시 후 재웜업이 "보수적이라 맞다"고 하지만, 플러시 직후에는 구버전 마커가 없으므로 웜업할 이유가 없다. 오히려 rebuild가 몰리는 바로 그 창에 유저 수 × 키스페이스만큼 SCAN이 돈다(r1 S1이 없애려던 비용).
   - **Fix:** 웜업 판정을 "구버전 파드가 존재할 수 있는가"로 바꾼다. 예: 배포 시 Helm 값/env로 `PENDINGIDX_WARMUP_UNTIL` 을 받거나, 웜업을 이미 지났다면 `since` 를 다시 세울 때 과거 시각(now − ttl)으로 세운다. 지금 배포 상태(이미 웜업 종료)라면 후자 한 줄로 충분하다.
   - **If we skip it:** 대량 유실 직후 Redis CPU 부하가 커진다. 정합에는 영향이 없다.

6. **주석·문서 정합** — 동작 영향 없음
   - `health_checker.py:165-170`: `_reconcile_budget_counters` docstring이 `budget_rebuild.lua`/pendingidx/ARGV 스캔값을 설명한다. 실제로 이 함수는 `budget_reconcile.lua` + 전체 SCAN 마커를 KEYS로 넘긴다(`:211-298`). r3 N3이 엉뚱한 스크립트를 기술했다 → reconcile.lua 기준으로 다시 쓴다.
   - `budget_deduct.lua:44`: "rebuild 결과는 db+pending+cur 로 동일값" — 구(additive) 의미다. floor는 "cur ≥ db+live면 no-op" 이다.
   - `budget_service.py:396, 509`: 오타 "그 달 내나" → "내내" (r1 편집에서 생김).

Verdict: fix 1 and 2 first.
Lean: −12 lines possible.
Not checked: 실 Redis Cluster(Valkey cluster.on)에서 미선언 키 접근 Lua의 실제 동작과 AgentCore의 `Mcp-Session-Id` 에코 여부는 확인하지 못했다(로컬 fakeredis는 단일 노드).
