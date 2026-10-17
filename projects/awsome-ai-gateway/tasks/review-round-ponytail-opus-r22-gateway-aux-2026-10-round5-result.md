# R22 라운드 5 재검증 — gateway-proxy 보조 서비스군

범위: 작업 트리(미커밋 38파일 + `credential_backoff.py`, `test_r22_round3_regressions.py`).
r4 수정 6건과 r1~r4 누적 변경(budget lua floor·sentinel·pendingidx, CB probe token, BoundedCapture/3인자 콜백,
credential backoff)을 재검증했다.

## Verdict: **CONVERGED** (M0 + S0 + N5)

r4 수정 6건은 모두 의도대로 반영됐고, 새 회귀는 없다. r4 S1(프로브 close 뒤 일반 성공이 회로를 다시 여는 결함)은
fakeredis에서 해소를 확인했다. 남은 것은 테스트 공백, 알림 정확도, 반환값·성능·문서에 관한 Nice-to-have뿐이다.

## 검증 근거

- `pytest tests` → **1434 passed, 60 skipped** (r4와 동일). 실 Redis/PG 증명 테스트는 env가 없어 skip.
- `ruff --select F`(변경 서비스 5파일) → `agentcore_mcp_client.py:28 field`, `health_checker.py:176 Decimal` 2건. 둘 다 r4에서 HEAD 기존분으로 확인했다. 이번 diff가 만든 건 없다.
- fakeredis + lupa로 실제 Lua/서비스 코드를 실행했다(스크래치 스크립트는 실행 후 삭제):

| 시나리오 | 결과 |
|---|---|
| CB: 실패 10 → open → 프로브 close → 일반 성공 20회 | `opened` 0회, `open` 키 없음 ✅ (r4에서는 1회차에 재오픈) |
| CB: close 후 실패 1 (11F/31T=35%) | 0 — 열지 않음 ✅ |
| CB: 2F3S 뒤 F (3/6=50%) / 2F4S 뒤 F (3/7=43%) | 1 / 0 — 실패율 경계(`>=`) 정상 ✅ |
| CB: 4F(min_calls 미달) 뒤 S (5/5, 80%) → 다음 F | S=0, F=1 — 성공은 열지 않고 다음 실패에서 연다 (N4 참고) |
| CB: 30s 경과 버킷 | 창 밖으로 빠져 집계 제외 ✅ |
| CB 서비스: `try_acquire_halfopen_probe("T")` → `record_success(probe_token="T")` → `record_success()` | close 후 `is_open=False`, 다음 일반 성공도 `False` ✅. 단 프로브 성공의 반환값이 `True` (N3) |
| budget: 월 첫 reserve → rebuild(db=0) | 센티넬 1 → rebuild `1`, usage `10` 유지, 센티넬 해제 ✅ |
| budget: 키 유실 후 reserve → rebuild(db=30) | `45` = 30 + live(10+5), resvsum `15` ✅ · 재실행 `0`, 이중가산 없음 ✅ |
| `settle_eval_args` 5키 / 4키(구스펙) | 5키는 HDEL로 `r1` 필드 제거 ✅ · 4키는 settle만 하고 인덱스 유지(`r2` 잔존 → rebuild 자기청소 대상) ✅ |
| settle 유실 경로(예약 40, 실비 15) | 카운터 `15` ✅, 반환 `new_used=-25` ❌ (N2) |
| `check_budget(db=None, client=codex)` | mget 호출은 `[team_key, team_key:lost]` 1회뿐 ✅ |
| `check_budget(db=…, client=codex)`, 키 정상 | mget은 `[user, user:lost, app, app:lost]` 1회 + `[team, team:lost]` 1회 ✅ |
| `_index_in_warmup`, `BUDGET_PENDINGIDX_WARMUP=false` | `False`, `since` 키를 만들지 않음 ✅ · `true`면 `True` + `since` 생성 ✅ |
| MCP: `httpx.MockTransport`. tools/call 응답이 `Mcp-Session-Id: S-STALE-ECHO` 를 에코 | 모든 호출이 `S-NEW` 를 송신하고 최종 id도 `S-NEW` ✅ |

## r4 수정 정합 확인

| # | 항목 | 결과 | 근거 |
|---|---|---|---|
| 1 | CB 열기 판정은 실패일 때만 | ✅ | `circuit_breaker.lua:43` `if is_failure == 1 and total >= min_calls and (fails / total) >= rate_thr`. 버킷 INCR(`:21-26`)과 합산 루프(`:30-37`)는 그대로라 실패율 분모·분자는 바뀌지 않았다. 성공은 실패율을 내리기만 하므로, 판정을 실패 시점으로 옮겨도 "임계 첫 도달"을 놓치는 경우는 min_calls 경계(N4) 하나뿐이다 |
| 2 | `settle_eval_args` 단일화 | ✅ | 정의는 `budget_service.py:52`. 호출은 `cost_recorder.py:423,455,511`, `budget_service.py:998` 네 곳이다. 모두 뒤에 `str(actual), fallback or "", idx_field or ""` 를 같은 순서로 붙여 `budget_settle.lua` 의 ARGV[1..3] 계약과 일치한다. 인라인 4/5키 분기는 0건 |
| 3 | check 핫패스 MGET | ✅ | 유저+앱 MGET은 `budget_service.py:433` `if db is not None:` 블록 안(`:448`)에 있다. 네 키가 모두 `{user_id}` 태그라 Cluster 단일 슬롯이다. 팀 MGET(`:597`)은 무조건 호출되며, HEAD의 무조건 `redis.get(team_key)`(HEAD `:439`)를 1:1로 대체한 것이다. `float()` 는 전부 `db is not None` 단락평가 뒤에 있다 |
| 4 | MCP 세션 id는 initialize에서만 캡처 | ✅ | `agentcore_mcp_client.py:225` `if method == "initialize":`. `_handshake` 가 먼저 id를 None으로 비우므로(`:334`) 재핸드셰이크는 항상 새 id로 시작한다 |
| 5 | `BUDGET_PENDINGIDX_WARMUP=false` | ✅ | `budget_service.py:270` 에서 Redis 접근 전에 `return False`. 설정은 `config.py:148`(기본 True), `.env.example:72-77` |
| 6 | health_checker docstring | ✅ | `health_checker.py:165-174` 가 `budget_reconcile.lua` 계약과 항목별로 일치한다: 마커를 KEYS[3..]로 전달(`reconcile.lua` 헤더 KEYS 정의), `orphan = resvsum - live` 되돌림, resvsum 양방향 SET, `db + live` 상향 플로어, ARGV[2] 퀴에센스 게이트. `budget_deduct.lua:44` 주석도 floor 의미로 고쳐졌고, "내나" 오타는 0건 |

## r1~r4 누적 정합

- **budget_rebuild.lua**: 트리거 재확인(`lost==0 and cur>=0` → 0) 뒤 `cur < target` 일 때만 SET하고, resvsum은 live로 덮어쓰고, 센티넬은 DEL한다. 인덱스 경로는 HKEYS → `marker_prefix..field` GET이다. 마커 prefix(`budget:pending:client:{uid}:`)에 필드(`<client>:<req_id>`)를 이으면 reserve 마커 키, 그리고 health_checker SCAN 파서 형식(`budget:pending:client:{uid}:{client}:{req_id}`)과 일치한다. 죽은 필드는 HDEL로 자기청소한다. 실측으로 확인했다.
- **sentinel 3종**: reserve `budget_reserve.lua:117,135-137`, deduct `budget_deduct.lua:46-51`, settle `budget_settle.lua:70-73`. 세 스크립트 모두 "키를 생성하면 `:lost` 를 세운다"는 같은 규칙을 따른다.
- **record_success의 probe_token 의미**: `circuit_breaker.py:95-106` — 토큰이 있을 때만 eval한다. `open` 은 무조건 DEL하고, `halfopen` 은 GET==token일 때만 DEL한다. 세 호출부 모두 토큰을 소비한다(`fallback_loop.py:588`, `messages.py:715-717`, `openai_compat.py:940-942`). 키 3개는 같은 `{pmid}` 슬롯이다.
- **BoundedCapture / 3인자 콜백**: `OnComplete = Callable[[str, str, bool], …]`(`streaming.py:33`). 세 헬퍼는 `streaming.py:319,615,847` 에서 `(text, status, truncated)` 를 넘긴다. 래퍼(`body_log_records.py:240-318`)도 같은 3인자 형태다. 수신측 5곳(`messages.py:782,1180`, `openai_compat.py:444,1014,1225`)은 모두 `truncated: bool = False` 를 받는다. `bedrock.py:303-304` 는 `_log_parts.truncated` 를 직접 쓴다. 누적은 `on_complete is not None` 일 때만 일어난다(`streaming.py:411,703,952`).
- **credential_backoff 분화**: 사용처는 `sigv4_signer.py:150`, `mantle_credentials.py:129`, `bedrock_account_client.py:92` 세 곳이다. 네트워크 예외와 비영구 ClientError는 2s, `_NONRETRYABLE_CODES` 와 비-botocore 예외는 30s다. `reraise_fresh` 로 traceback이 누적되지 않는다.

---

## Nice to have

1. **r4 수정에 회귀 테스트가 없다**
   - **What this is:** r4는 동작 수정 4건(CB 판정, MGET, MCP 캡처, 웜업 off)을 넣었다.
   - **Problem:** `tests/` 에 `pendingidx_warmup`, initialize 한정 세션 캡처, "close 후 일반 성공" 시나리오를 다루는 테스트가 없다(grep 0건). r4 S1은 조건 한 줄이라, 누가 `is_failure == 1 and` 를 지워도 1434개 테스트가 그대로 통과한다.
   - **Fix:** 위 실측표의 CB(A→B→C), `check_budget(db=None)` mget 스파이, MCP MockTransport 에코, warmup=false 4개를 `test_r22_round3_regressions.py` 옆에 옮긴다. 기존 `fake_redis` 픽스처로 충분하다.
   - **If we skip it:** 코드는 지금 맞다. 다음 리팩터에서 조용히 되돌아갈 위험만 남는다.

2. **settle 유실 경로가 `new_used` 와 임계값 교차를 잘못 계산한다** (`budget_settle.lua:86-87`)
   - **What this is:** 유실 경로(`:70-73`)는 델타 대신 실비 전액을 적용한다(r1 M1).
   - **Problem:** 그 아래 `logical_old = used - reserved` 는 유실 여부를 보지 않는다. `used=0` 이라 `logical_old=-reserved` 가 된다. 실측(예약 40, 실비 15)에서 카운터는 `15` 인데 반환은 `new_used=-25` 였다. 이 settle이 임계값을 넘어도 `threshold_triggered` 가 null이라 그 교차 알림이 빠진다.
   - **Fix:** 유실 플래그를 지역 변수로 두고 `local logical_old = lost and 0 or (used - reserved)` 로 쓴다.
   - **If we skip it:** 카운터 유실 직후 한 요청분의 알림만 정확도가 떨어진다. 과금·차단에는 영향이 없다.

3. **프로브 성공의 `record_success` 반환값이 `True`("opened")다** (`circuit_breaker.py:95,107`)
   - **Problem:** `_record` 는 `:open` 이 있으면 즉시 1을 돌려준다(`circuit_breaker.lua:14-16`). 그 값을 close 이후에도 그대로 반환한다. 실측: 반환 `True`, `is_open=False`.
   - **Fix:** 토큰 close를 수행했으면 `return False` 로 바꾼다. 아니면 docstring에 "반환값은 기록 이전 상태"라고 적는다. 현재 호출부 3곳은 반환값을 쓰지 않는다.
   - **If we skip it:** 지금은 무해하다. 나중에 반환값을 메트릭·로그에 쓰면 오보가 된다.

4. **성공 기록이 여전히 창 전체를 읽는다** (`circuit_breaker.lua:28-37`)
   - **Problem:** r4 이후 성공 기록은 판정에 쓰이지 않는다. 그런데 Lua는 성공마다 `window_sec × 2` = 60회 GET을 돈다. 정상 상태에서 거의 모든 상류 호출이 성공이므로, 요청당 Redis 연산의 대부분이 버려진다.
   - **Fix:** 버킷 INCR 직후 `if is_failure ~= 1 then return 0 end`. 판정 결과는 지금과 같다.
   - **Note:** 판정을 실패 시점으로 옮긴 부작용이 하나 있다. min_calls 미달 상태에서 성공이 total을 채우는 경우(4F+1S = 80%), 회로는 다음 실패 1건 뒤에 열린다(실측). 의도된 trade-off로 보이고 수정은 불필요하다. 다만 이 early-return을 넣을 때 같은 의미임을 주석에 남긴다.
   - **If we skip it:** 기능 문제는 없다. Redis CPU만 쓸데없이 쓴다.

5. **웜업 문서 정합 / 배포 배선**
   - `budget_service.py:279-283` 주석("Redis 전체 플러시 시 보수적 재웜업이 오히려 맞다")은 `config.py:143-147` 과 r4 N5의 근거(플러시 직후 SCAN 폭풍)와 반대 말을 한다 → "웜업 on일 때만 해당. 롤아웃 후에는 `BUDGET_PENDINGIDX_WARMUP=false` 로 이 경로를 끈다"로 고친다.
   - `BUDGET_PENDINGIDX_WARMUP` 는 Helm values 어디에도 없다(`deployment/` grep 0건). 차트는 `gatewayProxy.env` 맵으로 주입할 수 있다(`templates/gateway-proxy/deployment.yaml:71`). 기본값이 True라서, 운영자가 직접 넣지 않으면 r4 N5 수정의 효과는 나타나지 않는다. 롤아웃 런북이나 deployer-guide에 "전 파드 교체 + marker TTL(4h) 경과 후 false"를 한 줄 넣는다.

Verdict: 수렴. N1(회귀 테스트)과 N2부터 하면 된다.
Not checked: 실 Redis Cluster(Valkey cluster.on)에서 미선언 파생 키(`:lost`, 마커, CB 초당 버킷)를 접근하는 Lua의 실제 동작. AgentCore가 initialize 응답에 `Mcp-Session-Id` 를 주는지 여부. 로컬 fakeredis는 단일 노드이고, MCP는 MockTransport로만 확인했다.
