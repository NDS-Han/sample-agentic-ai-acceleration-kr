# R20 — gateway-proxy 요청 핫패스 리뷰 (2026-10)

리뷰어: kiro `claude-opus-5.5` (read-only) + 자체 검증 + 게이트웨이 테스트 스위트.
대상: `web_search_loop.py`(3.5k), `streaming.py`, `fallback_loop.py`,
`routers/messages.py`, `routers/openai_compat.py`, `upstream_compat.py`,
`circuit_breaker.py`, `rate_limit_enforcement.py` — 비용·정산·프로토콜 정확성이
직결되는 요청 경로. Haiku 5.5 장문 티어 과금 정확성이 이 라운드의 핵심 축.

## 라운드 1 — NOT CONVERGED (M 3, S 5, N 4)

kiro 실측(파일 전독 + 추적) 기반. 수정 후 전체 스위트 **1418 통과**.

### M — material

- **M1 `_anthropic_stream` 이 `turn_usages` 를 채우지 않아 장문 티어 과대청구.**
  스트리밍만 `merged.* +=` 인라인 합산 — `_merge_usage` 미호출로 턴 스냅샷 없음.
  40K×4턴(각 단문)이 합산 190K 로 임계 초과 → 전량 장문 요율.
  → 턴별 `TokenUsage` 축적(`_accumulate_anthropic_turn_usage`, 라이브/드레인
  공용) + 턴 종료 시 `_merge_usage`. 테스트:
  `test_anthropic_stream_turn_usages_populated`.
- **M2 두 스티처가 disconnect 시 진행 중 턴의 usage 유실.**
  `finally` 가 부분 `merged` 만 정산 — Anthropic 은 output 전량, Responses 는
  종결 이벤트 유일이라 입출력 전량 손실(KI-08 이 패스스루에 해결한 그 결함).
  → `except (CancelledError, GeneratorExit)` 에서 `turn_open` 일 때 백그라운드
  드레인(`_drain_anthropic_turn_usage`/`_drain_responses_turn_usage`) — 남은
  청크를 `stream_disconnect_drain_timeout` 내 소비해 merge 후 태스크가 스스로
  정산. `drain_pending` 마커로 `unsettled_reservation_guard` 와의 경쟁 차단,
  `drain_owns_settle` 로 finally 이중정산 차단. 테스트 ×2(disconnect 후
  drain_pending_tasks 로 비공백 검증 — 드레인 없으면 output 0 으로 실패).
- **M3 `/v1/chat/completions` 스트리밍(BEDROCK_RUNTIME_OPENAI)이
  `stream_options.include_usage` 미강제.** OpenAI 스펙상 미설정이면 usage
  프레임이 없어 스트리밍 호출의 입력 토큰이 전량 미청구.
  → 라우터의 model 재작성 지점에 주입(vLLM `openmodel_adapter` 와 동일 정책).
  AWS 샘플 커넥터 문서로 검증 — 공식 커넥터도 빌링용으로 동일 주입.

### S — should-fix

- **S1 입장 예약이 실제 최악치보다 작음.** `conservative_admission_body` 가
  `iters` 턴만 예약했으나 실제 상한은 검색턴 + soft + hard + nudge =
  `iters + 3`, 게다가 `iters<=1` 이면 예약 부풀림 자체를 건너뜀. 입력도
  누적 결과 재전송으로 단순 배수 이상 성장.
  → `turns = iters + 3` (`iters<=1` 특례 폐지), 입력 배수 동일 + 누적 성장분을
  `_gw_admission_input_add` 로 가산(`_estimate_input_tokens` 에서 소비,
  과금폴백 추정치는 add 차감 후 배수 나눔으로 단일턴 복원). 라우터 2곳이
  설정값 전달.
- **S2 Responses 스트림이 억제된 function_call 의 reasoning item 을 클라이언트에
  그대로 방출.** Codex 가 재전송하면 "reasoning item whose call is gone" 400 —
  `_strip_responses_web_search_items` 가 인바운드로 지우는 바로 그 형태.
  → reasoning item 을 `held_reasoning` 으로 보류, 다음 item 이 우리 호출이면
  함께 폐기·클라이언트 아이템이면 글로벌 인덱스로 방출. 종결 객체도
  `_finalize_responses_obj` 에서 직전 reasoning 동반 제거. 테스트 ×2(억제/방출).
- **S3 웹서치 스트림 첫 턴 비-200 이 HTTP 200 으로 반환.** SDK 재시도가
  429/529/5xx 상태코드에 의존 → 재시도 불가. → `status_holder` out-param +
  `run_web_search_loop` 이 첫 프레임을 peek 한 뒤 상태 결정(이후 턴 실패는
  열린 봉투 안의 SSE 오류가 맞는 신호). 테스트: 429 전파.
- **S4 `/v1/responses` 웹서치에 `result_text_chars`/`cache_results` 미전달.**
  기본값 0/True → `_build_evidence` 가 `_fit_to_cap` 건너뛰고 JSON 을 중간에서
  절단. → messages.py 와 같은 설정 전달.
- **S5 잘린/불완전 턴이 검색 턴으로 처리.** `max_tokens` 로 잘린 tool_use 는
  파싱 실패로 `{}` 입력 — 빈 검색 과금 + 동일 max_tokens 로 재루프. 또
  `message_delta` 없이 끝난 턴도 검색으로 오인(비과금 추가 턴).
  → `is_search_turn` 에 `saw_message_delta and stop_reason_final=="tool_use"`
  게이트(스트림), nonstream 은 `final_body.stop_reason=="tool_use"` 필수.
  테스트 ×2(max_tokens/무종결).

### N — nice-to-have (전부 조치)

- **N1 half-open 프로브 낭비** — CB 프로브 획득 후 admission 거절 시 창의
  유일 슬롯 소모 → 토큰 부여 `try_acquire_halfopen_probe` + Lua 조건부 삭제
  `release_halfopen_probe`, 거절 경로에서 반환(messages.py + fallback_loop).
- **N2 `_drain_error` 가 열린 블록 미폐쇄** → `open_global_blocks` 인자로
  받아 `content_block_stop` 발행 후 `message_stop`.
- **N3 미매핑 인덱스 유출** — `local_to_global.get(x, x)` 가 start 없는 로컬
  인덱스를 방출 → 미매핑이면 억제(anthropic delta/stop + responses 4곳).
  테스트: citations_delta.
- **N4 소프트/answer-now 노트가 turn_body 사본에만** → `conversation` 에 영속
  (헬퍼 멱등) — 다음 턴의 캐시 프리픽스 보존. 테스트: nudge 재시도 바디에서
  앞선 유저 메시지의 노트 확인.

### 확인 후 sound 로 유지(라운드 1 kiro 판정)

루프 종결 상한(iters+3 검증값 그대로 예약), 턴 상태 리셋, 봉투 id 고정,
폴백 상태 분류·ATTEMPT_UNSETTLED, 예약 해제 멱등, drain_pending 가드,
thinking 정규화 순서, 캐시 브레이크포인트 4개 회계, 네이티브 블록 재작성,
server tools cache_control 이동.

## 라운드 2 — 진행 중

## 라운드 2 — NOT CONVERGED (M 2, S 3, N 7)

라운드 1 수정 검증에서 **수정이 만든 신규 결함** 발견. 스위트 1420 통과.

### M — material (수정으로 생긴 회귀)

- **M-a S3 peek 래퍼가 스티처 close 를 차단.** `_replayed` 의 `async for` 는
  `gen.aclose()` 를 전파하지 않아 disconnect 시 M2 드레인이 GC 에 의존 →
  `contextlib.aclosing(gen)` 으로 감쌈. peek 가 턴-1 호출을 미리 시작하므로
  결정적 close 가 더 중요해졌다.
- **M-b M1 의 턴 끝 merge 가 예외 경로의 진행 턴 usage 를 유실.** 미드턴
  상류 오류 시 `turn_usage` 미머지 → finally 에서
  `turn_open and not drain_owns_settle` 일 때 merge.

### S — should-fix

- **S-a 드레인의 비타임아웃 오류가 merge 를 건너뜀** → `_drain_anthropic_turn_usage`
  의 merge 를 finally 로.
- **S-b iters+3 은 Anthropic 만 맞음** — Responses 는 soft/nudge 없이 iters+1,
  성장분도 T(T-1)/2(검색 안 하는 턴 포함) → `bound_turns` 인자 +
  `Σ min(t, iters)` 라운드분. Responses 경로 ~3배 과대 예약 해소.
- **S-c 첫턴 비-200 이 200 없이도 SSE 본문** → `status_holder` 가 비-2xx 면
  SSE 페이로드를 파싱해 JSONResponse + Retry-After 전달 + `_log_nonstream`
  배선. 빈 첫 프레임(턴-1 invoke 예외)은 502.

### N — 조치

- 빈 종결턴 → `_EMPTY_TURN_PLACEHOLDER` 텍스트 블록(스트림+비스트림,
  `content: []` 재전송 400 방지). 미매핑 인덱스 억제로 citations_delta 등
  사전 오픈 non-text 델타 손실은 의도된 트레이드오프로 기록.
- 과금폴백 추정치 음수 방지 `max(0, …)`.
- 프로브 누수: messages.py 심사 예외·디스패치 예외(`_ws_probe_used` 플래그),
  fallback_loop 심사/바디조립/invoke 예외, openai_compat 디스패치 예외에
  토큰 반환 추가.
- `stream_options: null` 클라이언트 — 비-dict 는 `{}` 로 교체 후 주입.
- `_final_turn_mode` docstring `+2` → `+3` 정정.
- 확인 후 유지: `_ADMISSION_MAX_OUTPUT_KEYS` 의 first-key vs
  `_extract_max_output` 의 max — 기존 동작, 라운드 범위 밖으로 기록.

## 라운드 3 — 진행 중

## 라운드 3 — NOT CONVERGED (M 1, S 2, N 9)

라운드 2 수정 검증에서 신규 결함 + 잔여 결함. 스위트 1423 통과.

### M — material

- **M-1 빈 첫 프레임이 502 가 아닌 200 반환.** `peek_status` 디폴트 200 이
  `200 <= s < 600` 범위를 통과 → `400 <= s < 600 else 502` 로 정정 + 회귀
  테스트(invoke 예외 → 502, anthropic 에러 형태).

### S — should-fix

- **S-1 peek 가 `unsettled_reservation_guard` 의 "미시작=미호출" 전제 파괴.**
  spec<2.4 첫바이트 전 disconnect 면 `_replayed` 미시작 → 스티처 미클로즈 →
  drain_pending 미설정 → guard 가 release(0) 후 뒤늦은 정산과 경쟁.
  → `run_web_search_loop` 이 `BackgroundTask(_aclose_quiet, gen)` 를 달고,
  라우터 2곳이 `BackgroundTasks([스티처클로즈, guard])` 로 합성.
- **S-2 플레이스홀더가 정상 빈 end_turn 에도 발화.** 사용자 대화록에
  "cut short" 오표기 + 모델 자기모방 위험 → `max_tokens`/억제된 tool_use
  에만 절단 문구, 그 외 `_EMPTY_TURN_NO_TEXT` 중립 문구.

### N — 조치

- N-1 anthropic 방언 에러 페이로드를 `{"type":"error",…}` SDK 네이티브 형태로.
- N-2 Retry-After `str()` 캐스트.
- N-3 첫턴 에러 경로 `gen.aclose()` 만으로는 내부 `_drain_error`/`chunk_iter`
  미정리 → 소비 완료 후 aclose.
- N-4 스티처가 삼킨 예외(턴-1 바디조립 실패)로 상류 미호출 시 프로브 슬롯
  TTL 점유 → 디스패치 후 `_ws_probe_used` 미설정이면 반환(양 라우터).
- N-6 직렬화 실패 폴백 추정치에도 mult/add 적용 — 호출측 역산이 4096 을
  정확히 복원(기존엔 ≈0 과소청구).
- N-7 `_anthropic_stream` except 분기가 open_global_blocks 미폐쇄 → 폐쇄 후
  message_stop.
- N-8 "All six return paths" → seven 정정.
- N-9 `drain_owns_settle` 플래그를 `_spawn` 이후에 설정(스폰 실패 시 finally
  가 대신 정산).
- 기록만: N-5 비폴백 4xx/500 시 프로브 TTL 점유(기존), `first-key vs max`
  최대출력 키 차이(기존).

## 라운드 4 — 진행 중

## 라운드 4 — NOT CONVERGED (M 1, N 5)

라운드 3 수정 검증. S-1 합성이 만든 신규 결함 + 잔여. 스위트 1423 통과.

### M — material

- **M-1 `BackgroundTasks([None, guard])` TypeError.** F-7 패스스루와 MCP
  초기화 실패 경로는 루프가 `background` 를 안 달아 합성이 `[None, guard]`
  → 응답 후 `await None()` TypeError + guard 미실행(예약 TTL 누수).
  → 라우터 2곳이 None 필터 합성 + 두 패스스루 응답에도
  `BackgroundTask(_aclose_quiet, gen)` 부착(미시작 aclose 는 no-op 이지만
  시작-후-버려짐 경로의 스트리머 drain/finally 를 결정적으로 닫는다).

### N — 조치

- **N-a** nonstream 은 stop_reason 재작성(tool_use→end_turn) 후 검사해
  억제 tool_use 턴이 중립 문구 → 재작성 전 값 캡처, `max_tokens|tool_use`
  면 절단 문구로 스트림과 정렬.
- **N-b** 인밴드 에러가 첫 프레임(200 스트림의 throttling/빈 스트림)이면
  봉투 미개방 → JSON 에러 경로(`first_is_error`)로 502 + SDK 네이티브 형태.
  회귀 테스트 추가.
- **N-c** `drain_pending` 도 `_spawn` 이후에 설정 — 스폰 실패 시 마커만
  남아 guard 가 drain_timeout+10s 헛기다리는 것 차단(양 스티처).
- **N-d** 미시작 disconnect 는 본문 로그 미기록 — body-log 래퍼가 시작 전이라
  경로상 불가, 경미로 기록.
- **N-e** 합성 검증이 소스 grep 수준 → 런타임 테스트 추가(루프 단독 태스크
  검증은 있음, 라우터 합성은 문자열 가드 + None 필터 요구로 보강).

## 라운드 5 — 진행 중

## 라운드 5 — CONVERGED (M/S 없음, N 4 + 사전존재 2)

M-1 필터·N-c 순서·N-b 범위·N-a 캡처 전부 검증 통과. 잔여 N:

- **N-1** 패스스루 `_aclose_quiet` 는 미시작 gen 의 no-op — `chunk_iter` 도
  같이 닫도록 고쳐 첫 바이트 전 disconnect 시 상류 커넥션이 GC 전에 해제.
- **N-2** `_spawn`-후-마킹 순서를 `streaming.py`(3곳)·`routers/bedrock.py`
  에도 적용 — 스티처와 같은 계약.
- **N-3** 인밴드 에러 정규화 — responses 형태 `{"error":{…}}` 래핑 +
  `error.type` 에 throttl*/rate_limit 면 429, overload 면 529 매핑.
- **N-4** 합성 런타임 테스트 추가 — F-7 패스스루 응답에 라우터와 같은
  `BackgroundTasks` 합성을 붙여 `await` (pre-fix TypeError 재현 가능 형태).
- 사전존재 기록: 스티처 루프에 idle bound 없음(ALB 가 선제 절단),
  `response.in_progress` 는 `not envelope_open` 일 때만 전달.

## 최종 상태

- 게이트웨이 스위트: **1425 passed, 60 skipped, 1 warning** (22.3s)
- 5라운드 경과: R1 M3/S5 → R2 M2/S3 → R3 M1/S2 → R4 M1 → **R5 CONVERGED**
