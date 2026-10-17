# R22 — gateway-proxy 보조 서비스군 리뷰 (kiro/opus, ponytail)

범위: R20 핫패스(web_search_loop·스트리밍·fallback·라우터) 이외의 보조군 —
budget_service/redis lua, circuit_breaker, credential 3종(bedrock_account_client,
mantle_credentials, sigv4_signer), body_logger/body_log_records/body_log_flag,
agentcore_mcp_client, auth_service, routing_profile_loader, cost_recorder,
rate_limit_enforcement, middleware/budget, main.py(STS/BotoConfig).

## 라운드 1 — NOT CONVERGED (M4 + S7 + N6)

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | Redis 카운터 유실 시 settle이 음수 델타로 얇은 키를 재생성하고 pending 미가산 — `:lost` 센티넬 + DB+pending 기반 rebuild | `budget_settle.lua`(마커 생존+usage 부재 → 실비 전액+센티넬), `budget_reserve.lua`(usage 부재+resvsum>0 → 센티넬), `budget_deduct.lua`(첫 생성 시 센티넬), `budget_rebuild.lua`(성공 시 센티넬 해제). `budget_service`는 user/team/app 카운터 복원 시 센티넬 확인 + `budget:rebuild-lock:` per-scope 락으로 폭풍 억제 |
| M2 | CB 키 `cb:{pmid}:*` 가 Redis Cluster 다슬롯 — EVAL CROSSSLOT으로 브레이커 영구 fail-open | 전 키 `cb:{{pmid}}:*` 해시태그 + halfopen 슬롯 동일 태그. 구 키는 TTL 자연 소멸 |
| M3 | STS/Mantle/SigV4 자격증명이 프로세스 전역 락 없이 stampede, 실패도 캐시돼 반복 실패 | 세 서비스 모두 캐시 선확인 → per-key 단일비행(asyncio.Lock) → 이중확인, 실패 미캐시 + 타임아웃. `main.py` STS 클라이언트에 `BotoConfig` connect/read timeout 명시 |
| M4 | 임의 성공이 open CB를 즉시 닫아 flap — 회로 열리기 전 시작된 in-flight 늦은 성공이 open 삭제 | `record_success(probe_token=)` — half-open 슬롯 소유자의 조건부 CAS close 만 open+슬롯 삭제. 토큰 없는 성공은 카운터만 기록. 호출부 3곳 갱신 |
| S1 | pending 합산이 rebuild 마다 키스페이스 SCAN — 복구 폭풍 × 스캔 | `budget:pendingidx:{scope}:{tag}` 해시 인덱스(reserve가 HSET, settle이 HDEL, 같은 hash tag) — 읽기는 HKEYS+MGET, 고아 필드 자가청소, 인덱스 부재 시 SCAN 폴백(롤링 배포·만료 안전) |
| S2 | `_estimate_input_tokens` non-ascii 카운트가 문자별 파이썬 루프 — MB급 바디에서 이벤트루프 정지 | `isascii()`/`encode(ignore)` C 경로 |
| S3 | body_logger 틱당 1배치(20/s 상한) — 큐 적재 시 처리율 상한 | `_flush_once` 를 소진 루프로 — 완료분만 추적, 취소 시 미처리분만 큐 한도 내 재적재 |
| S4 | `body_log_records` streaming frames 무제한 누적 — 대형 스트림이 진단 버퍼로 메모리 압박 | `_MAX_CAPTURE_BYTES=4MB` 캡 — 클라이언트 스트림은 그대로, 로그 버퍼만 절단 + truncation 메타 |
| S5 | MCP 429/5xx 도 세션 무효화 — 일시 과열도 session 재수립 강제 | `AgentCoreMcpProtocolError.transient` — 429/5xx는 세션 유지, tool-not-found/프로토콜 오류만 무효화. `tools/call`의 `isError:true` 는 실패로, SSE는 마지막 유효 JSON-RPC data 이벤트 채택 |
| S6 | 예약 한도 429가 실고갈과 같은 코드·메시지 | `*_budget_reservation_exceeded` reason 분기 — 예약 차단은 잔량 존재를 명시하는 별도 메시지 |
| S7 | `budget_backend_unavailable`이 429로 나가 클라이언트가 rate-limit 백오프를 탐 | 503 + Retry-After:5 (`middleware/budget.py` 동일 매핑) |
| N1 | tool_filter/messages 라우터 미세 정합 | 반영 |
| N2 | rate_limit_enforcement 예약 해제 경로 | 반영 |
| N3 | 프로파일 없는 client(other)가 매 요청 DB 조회 | `__absent__` 음성 캐시 TTL 30s |
| N4 | (S7로 승격 처리 — 503 분기) | — |
| N5 | Converse 캐시토큰 total 미포함 — input=0·cache_read>0 응답이 전액 환불 | 환불 게이트에 `cache_read_input_tokens`/`cache_creation_input_tokens` 추가 |
| N6 | auth/routing-profile 잡음 정리 | stale 라우팅 프로파일은 DB 열화 시에만 서빙(`:stale` 키), body_log_flag bytes 정규화 |

### 검증
- gateway-proxy `pytest tests` → **1429 passed, 60 skipped**
- 신규 회귀: CB 3건(일반 성공 비폐쇄·프로브 소유자 폐쇄·스테일 토큰 비폐쇄), pending 인덱스 2건(HKEYS+MGET 경로·client 필드 필터), 기존 더블 갱신(fallback_loop probe_token, budget_reservation eval 인자)
- `test_fallback_loop`/`test_circuit_breaker`/`test_budget_reservation`/`test_budget_service` 전부 그린

### 잔여 관찰(비차단)
- 인덱스 TTL은 마지막 HSET 시 marker_ttl 로 갱신 — 인덱스가 마커보다 먼저 죽는 창은 SCAN 폴백으로 정합 유지
- 롤링 배포 창의 구버전 마커는 인덱스에 안 실림 — SCAN 폴백이 흡수

## 라운드 2 — NOT CONVERGED (M2 + S8 + N4)

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | additive `base+cur` rebuild + 항상 발화하는 sentinel 조합으로 이중가산 — 재구축이 현재값 위에 DB+pending을 더해 카운터 팽창 | `budget_rebuild.lua` floor semantics 재작성 — `cur < base`(DB+live pending)일 때만 SET, Lua 내부에서 인덱스 HVALS/MGET 으로 live pending 합산, 성공 시 센티넬 해제·락 제거. `budget_reserve.lua`는 `usage_existed==0` 이면 항상 센티넬(resvsum 동시유실 케이스 커버). Python 측은 `_pending_reservation_sum` SCAN 전용 복귀 + `_rebuild_counter` 헬퍼(in-flight 플래그 5s, 항상 eval, 적용될 때만 로그) |
| M2 | `body_log_flag` JSONB가 asyncpg에서 str로 디코딩돼 `bool("false")=True` — off 설정이 켜진 것으로 오인 | JSONB 문자열/불리언 양쪽 정규화 |
| S1 | (연쇄 수정) — rebuild 트리거 경로 shield/재구성 | `_index_in_warmup` 웜업 헬퍼 + in-flight 플래그로 rebuild 스탬피드 억제 |
| S2 | 자격증명 실패 백오프 미분화 — 비재시도와 일시 오류가 같은 TTL | 공통 분류기 모듈 신설(3곳 공용) — 비재시도 30s / 일시 오류 2s, 캐시된 예외 재발생 시 traceback 누적 방지 |
| S3 | `streaming.py` 세 헬퍼의 `accumulated_frames` 무제한 누적(S4 연쇄) | `BoundedCapture` 로 교체 — 증분 UTF-8 디코더 + `_MAX_ACCUMULATED_CHARS`(1M) 캡 + `truncated` 플래그, cap 이후에도 종결 표지 스캔 계속(절단 스트림이 partial 오인 안 함) |
| S4 | (r1에서 처리, S3이 연쇄 잔여) | — |
| S5 | (r1 transient 처리의 연쇄) — 세션 id 정리 누락 | S8 참조 |
| S7 | usage/resvsum 이 서로 다른 순서로 유실되면 재구축이 legitimate reserve/settle 델타를 덮어씀 | reserve의 usage 부재 시 항상 센티넬 + Lua 내부 floor 계산으로 해소 |
| S8 | MCP 비일시 리셋이 `_mcp_session_id` 를 정리하지 않아 죽은 세션 id로 재핸드셰이크 시 initialize 거절 → 네거티브 캐시 TTL만큼 반복 실패 | 비일시 리셋 시 `_mcp_session_id=None` + `_handshake()` 시작 시 항상 클리어 |
| N1 | 스테일 프로브 토큰 성공이 open을 못 닫음 — 슬롯 재획득 뒤에도 우리 프로브의 성공은 회복 증거 | `record_success` Lua: `open`은 프로브 토큰 보유자의 성공이면 무조건 DEL, `halfopen` 슬롯만 토큰 일치 시 DEL(타 파드 신규 프로브 보호) |
| N2 | `budget_backend_unavailable` 503에 `Retry-After` 없음 | `retry-after: 5` 헤더 추가 |
| N3 | DB 정상+행 부재(프로파일 삭제)인데 `:stale` 스냅샷이 남아 DB 열화 창에 삭제된 프로파일 부활 | `row is None` 경로에서 `:stale` 키도 함께 DEL |
| N4 | `budget_counter_restored` 로그가 미적용 재구축에도 발화 | Lua 반환값을 검사해 실제 SET된 경우만 로그 |

### 검증
- gateway-proxy `pytest tests` → **1429 passed, 60 skipped**
- 콜백 계약 변경(`(text, status, truncated)` 3인자)에 따른 테스트 더블 갱신: `test_high_body_logging_gate`(콜백 3곳 + AST append→feed), `test_high_openai_dialect_body_logging`(훅 10곳)
- `test_stale_probe_token_does_not_close_open` → `test_stale_probe_token_closes_open_but_keeps_new_probe` 의미 갱신(N1)

## 라운드 3 — NOT CONVERGED (M0 + S3 + N9)

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| S1 | **r2 N1 회귀** — 웹서치 멀티턴이 같은 프로브 토큰을 재사용, 무조건 close로 바뀐 뒤 턴2+ 성공이 사이에 다른 파드가 재오픈한 회로를 지움 | `messages.py`/`openai_compat.py` `_ws_cb_record` — 첫 `record_success` 와 함께 토큰 소비(`_ws_probe_token=None`) |
| S2 | 앱 카운터 단독 유실은 복원 루프가 유저 트리거 블록 안에 중첩돼 영구 fail-open | 유저 트리거 실패 시에도 현재 client 의 앱 키 부재·음수·센티넬을 검사해 같은 복원 블록 발화(복원 블록은 진입 시 PER_APP 전체 순회) |
| S3 | `_index_in_warmup` 의 `since` 키가 `ex=ttl*2` — 만료 때마다 웜업 재시작돼 시간의 절반이 SCAN 폴백 | `since` 를 무기한 키로 — 만료 재시작 제거(Redis 플러시 시 보수적 재웜업은 유지) |
| N1 | 주석이 HVALS/MGET 인데 구현은 HKEYS+필드별 GET | 주석 정정(`budget_reserve.lua`, `budget_service.py`) |
| N2 | `OnComplete` 주석이 구형 2인자 | 3인자 + BoundedCapture 명세로 갱신 |
| N3 | `health_checker` docstring이 "additive + NX 락" 구판 | floor semantics로 갱신 |
| N4 | 캡이 문자 수인데 주석은 바이트 한도 일치 주장 | 문자/바이트 구분 명시 |
| N5 | upstream 대기 중 CancelledError 시 `_fire` 미발화 — 본문 레코드 유실 | `except asyncio.CancelledError` → GeneratorExit 와 같은 detach 태스크 경로(`_detach_close_and_fire` 추출) |
| N6 | JSONB `'"false"'`(이중 인코딩) 행이 True 로 판정 — postdeploy 체크는 OFF 로 읽음 | 파싱 결과가 str 이면 `_truthy` 재판정 |
| N7 | `credential_backoff` 도입 후 구 `_FAILURE_BACKOFF_S` 잔존 ×3 | 제거 |
| N8 | MCP 리셋에 세대 검사 없음 — 늦은 실패가 방금 만든 새 세션을 지움 | `_session_gen` 카운터 — 초기화 성공 시 증가, 호출이 잡은 세대와 현재가 다르면 리셋 스킵 |
| N9 | Lua 가 미선언 키(`:lost`, 마커) 접근 — 클러스터 계약 위반이지만 같은 해시태그라 동작 | `budget_rebuild.lua` 헤더에 계약 명시(호출자가 같은 슬롯 보장) |

### 검증
- gateway-proxy `pytest tests` → **1434 passed, 60 skipped**
- 신규 회귀 `tests/services/test_r22_round3_regressions.py` 5건: 재오픈 회로 보존(턴2 성공), 라우터 토큰 소비 AST(2 라우터), 앱 키 단독 유실 복원 발화, 웜업 종료 지속
- `test_stale_probe_token_closes_open_but_keeps_new_probe` 로 N1 의미 검증 유지

## 라운드 4 — NOT CONVERGED (M0 + S2 + N5) — fakeredis+lupa 실측 포함

리뷰어가 fakeredis+lupa로 Lua를 실제 실행해 검증. r3 수정 8건 전부 정합, r1~r2 누적분 재확인.

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| S1 | `circuit_breaker.lua` 가 성공 기록에도 열기 판정 — 프로브 close 직후 첫 일반 성공이 옛 실패 버킷으로 회로를 즉시 재오픈(실측 재현: 실패10→open→프로브성공→closed→일반성공1→reopened). 프로브 회복이 ≈window(30s) 동안 사실상 무효 | 열기 판정을 `is_failure == 1` 일 때만 — 성공은 실패율을 내리기만 함 |
| S2 | settle 4/5키 분기가 `cost_recorder._settle_args`와 `BudgetService.settle_budget` 인라인 두 곳에 복붙 — 계약 변경 시 한쪽만 갱신돼 인덱스 HDEL 유실 위험 | `settle_eval_args` 를 `budget_service` 모듈 함수로 단일화, 양쪽 호출부 공용 |
| N3 | check 핫패스 Redis 왕복 6회(키별 GET/EXISTS) | 같은 `{uid}`/`{tid}` 슬롯이므로 MGET 으로 묶음 — 유저+앱 1회, 팀 1회 |
| N4 | MCP 세션 id 캡처가 모든 응답에서 — 재핸드셰이크 뒤 도착한 구버전 응답이 새 id를 덮어씀 | `method == "initialize"` 응답에서만 캡처 |
| N5 | Redis 플러시 후 `since` 재생성으로 웜업 재시작 — rebuild 폭풍 창에 SCAN 폭풍 | `BUDGET_PENDINGIDX_WARMUP` 설정 추가 — 롤아웃 완료 후 false로 두면 재웜업 없음. .env.example 문서화 |
| N6 | health_checker docstring이 reconcile 대신 rebuild.lua 를 기술, deduct.lua 구 additive 주석, 오타 "내나" | 정정 |

### 검증
- gateway-proxy `pytest tests` → **1434 passed, 60 skipped**
- `mget` 도입에 따른 테스트 더블 갱신: conftest 기본값 + `test_team_budget_enforcement`/`test_bedrock_e2e`/`test_r22_round3_regressions`의 fake 갱신(카운터 존재·센티넬 부재 응답)
- S2 회귀 테스트가 mget 경로를 검증하도록 갱신(유저 생존+앱 센티넬)

### 잔여 관찰(비차단)
- 실 Redis Cluster(Valkey cluster.on)에서 미선언 키 접근 Lua 동작은 미실측 — 같은 hash tag 라 슬롯은 동일하지만 계약상 KEYS 선언이 아님(r3 N9 주석으로 계약 명시)
- AgentCore 가 비-initialize 응답에도 Mcp-Session-Id 를 에코하는지 미확인 — N4 는 보수적 한정

## 라운드 5 — CONVERGED (M0 + S0 + N5)

리뷰어가 fakeredis+lupa로 실 Lua를 실행해 전수 실측. r4 수정 6건 정합 + r1~r4 누적 정합 확인, 새 회귀 없음.

### 수렴 후 처리한 N 항목

| ID | 내용 | 조치 |
|---|---|---|
| N1 | r4 동작 수정에 회귀 테스트 부재 | `test_r22_round3_regressions.py`에 4건 추가 — 실 Lua 실행 CB(close 후 성공 재오픈 방지 + 실패율 경계), db=None mget 스파이, MCP initialize 한정 캡처(MockTransport 에코), warmup=false |
| N2 | settle 유실 경로 `logical_old=used-reserved` → new_used=-25 오보고, 임계 교차 알림 누락 | `usage_lost` 플래그 — 유실 시 logical_old=0 |
| N3 | 프로브 성공 `record_success`가 True("opened") 반환 — 기록 이전 상태 | close eval 성공 시 False 반환 |
| N4 | 성공 기록이 창 전체(window×2=60 GET)를 읽음 | 성공은 버킷 INCR 후 early-return — 판정 결과 동일 |
| N5 | 웜업 주석과 config 의미 불일치 + Helm 미배선 | 주석 정정 + `gatewayProxy.env.BUDGET_PENDINGIDX_WARMUP` values.yaml 추가(롤아웃 후 false 가이드) |

### 최종 검증
- gateway-proxy `pytest tests` → **1439 passed, 60 skipped**
- 수렴 판정: M0/S0 — 잔여 전부 N급 처리 완료

### 미실측 잔여(문서화)
- 실 Valkey Cluster(cluster.on)에서 미선언 파생 키(`:lost`, 마커, CB 버킷) 접근 Lua 동작 — 같은 hash tag라 슬롯은 동일
- AgentCore가 비-initialize 응답에 `Mcp-Session-Id` 에코하는지 — N4는 보수적 한정이라 무해
- `BUDGET_PENDINGIDX_WARMUP` 은 이번 배포가 롤아웃된 뒤 marker TTL(4h) 경과 시 false 전환 — 운영 절차
