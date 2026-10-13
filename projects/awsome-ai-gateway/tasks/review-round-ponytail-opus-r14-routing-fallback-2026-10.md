# R14 — 게이트웨이 라우팅/폴백 심층 리뷰

대상: `fallback_loop.py`, `fallback_resolver.py`, 어댑터 4종, `messages.py`, `openai_compat.py`,
`streaming.py` 경계, 타임아웃 체계. 리뷰어: swe-2 + kiro `claude-opus-5.5` (본심 14건 + 검증 3건).

## Findings & Fixes

### H1 — 클라이언트측 read-timeout이 폴백을 발동해 이중 청구 위험 (수정)

`ReadTimeoutError`→504는 `_FALLBACK_STATUSES`라 다음 후보로 재시도됐다. 소켓 타임아웃은
상류가 이미 추론을 처리·과금했을 수 있는 신호 — 성공한 첫 시도 비용은 usage에 없는 채
두 번째 모델이 다시 청구된다. Mantle/httpx 어댑터도 모든 예외를 502로 뭉개 같은 위험.

- `ATTEMPT_UNSETTLED_HEADER` 내부 마커 도입(`providers/base.py`). 어댑터가
  "상류 처리·과금 불명" 실패에 표시하면 루프가 그 오류를 즉시 반환(폴백 중단).
- `classify_transport_error`: `ReadTimeout`→504+마커; Connect/Pool/Write 실패(미도달
  확정)→503; 기타→502. **PoolTimeout/WriteTimeout을 503으로 분리**(검증 V1) —
  풀 고갈 시 폴백이 가장 필요한 순간에 포기하는 역효과 방지.
- bedrock/mantle/mantle_openai/bedrock_openai 어댑터 전부 적용.
- 서버가 명시 거부한 `ModelTimeoutException`(504, 마커 없음)은 폴백 허용 유지.
- 회귀: `test_unsettled_read_timeout_aborts_fallback`,
  `test_server_side_timeout_still_falls_back`, `test_pool_timeout_is_503_not_unsettled`.

### H2 — Codex 웹서치 스트림에 `unsettled_reservation_guard` 누락 (수정)

`openai_compat.py` ws 경로가 guard 없이 반환 — 첫 바이트 전 disconnect면
`max_iterations × max_output` 예약이 마커 TTL까지 누수. `background=` 추가.

### M3 — half-open 프로브 이중 획득 + ws 경로 CB 결과 미기록 (수정)

- `messages.py`가 ws 활성 여부와 무관하게 프로브를 먼저 획득 → 폴백 루프의 재획득이
  NX 실패 → 정당한 프로브 요청이 원본을 건너뛰고 8s 창 낭비(체인 없으면 허구 503).
  → 프로브를 ws-enabled 조건 안으로 이동.
- ws 분기는 폴백 루프 우회라 CB 성공/실패를 기록하지 않았다 — 닫힌 회로에서 ws 턴
  실패가 회로를 열지 못하고, 프로브 성공도 회로를 못 닫아 ws가 8s당 1요청에 갇힘.
  → `_ws_cb_record`로 턴 결과 반영(messages·openai_compat 공통).
- `openai_compat` ws 경로엔 CB 게이트 자체가 없었다 → 추가(프로브 패배 시 503 +
  `release_reservations` — 입장심사가 먼저라 누수 방지).

### M4 — 스트림 미드에러가 CB에 기록되지 않음 (수정)

루프는 헤더 도착(2xx)에 record_success — 헤더 뒤 죽는 스트림(modelStreamErrorException
등)은 회로가 닫힌 채로 남음. `_record`에서 `outcome["status"]=="error"`면
`record_failure`(disconnect drain은 "success"라 오인 없음).

### M5 — 후보 에러가 원본 에러를 덮어씀 (수정)

후보의 비-폴백 오류(400 잘못된 pmid 등)가 클라이언트에 그대로 전달돼 원본의 진짜
오류를 가렸다 → 후보 슬롯 스킵 + last_error 보존. 소진 시 last_error가 없으면
(원본 미시도 + 후보 전원 스킵) 후보의 4xx 대신 일괄 503(검증 V3).
회귀: `test_candidate_error_does_not_mask_original`,
`test_skipped_candidate_errors_do_not_mask_all_unavailable`.

### M6 — 폴백 총 시간 예산 없음 (수정)

최대 6후보 × 상류 타임아웃 300s 누적 → ALB idle 초과. `fallback_budget_sec`(기본 180)
데드라인 도입 — 첫 후보는 항상 시도, 이후는 예산 내에서만.
회귀: `test_fallback_budget_stops_new_attempts`.

### M7 — 비-2xx 스트림 응답이 SSE 형태로 나감 (수정)

4xx/5xx에 `text/event-stream` + 오류 제너레이터 → Anthropic/OpenAI SDK가 JSON 오류로
파싱 불가 → 제너레이터를 소비해 프로토콜 JSON 응답으로 환원(양쪽 라우터).
**검증 V2**: JSON 환원이 usage_logs ERROR 행과 `bedrock_request_id`를 유실 →
status≥500에 finalize 복원(4xx는 클라이언트 오류라 미기록 — 기존 관례 유지).

### M8 — /v1/messages가 Retry-After/x-ratelimit 헤더 폐기 (수정)

`_advisory_headers` 화이트리스트(retry-after, x-ratelimit-*)로 스트림/비스트림 오류
응답 양쪽에 전달.

### M9 — MANTLE_HTTP_TIMEOUT 미배선 + idle보다 짧음 (수정)

httpx read=120s(기본) < STREAM_IDLE_TIMEOUT(dev 240/prod 540) → 긴 thinking 스트림이
timeout_error 대신 stream_failed로 죽고, 그 예외가 502로 CB를 오염.
configmap에 `idleTimeout+30` 기본 배선(override: `gatewayProxy.mantleHttpTimeout`).

### L13 — 후보 해석 예외가 500으로 탈출 (수정)

`resolve_model_config`에 broad except → 로그 + 슬롯 스킵.
회귀: `test_candidate_resolve_exception_skips_slot_not_500`.

### L14 — endpoint 없는 mantle 후보가 원본 endpoint 상속 (수정)

`_resolve_candidate`에서 endpoint 결핍 → `LookupError`→슬롯 스킵(다른 리전/계정
호출 방지).

## 수용 잔여 (accepted)

- **Codex `/v1/responses`, `/v1/chat/completions`에 가용성 폴백 없음** — 단일 디스패치.
  폴백 루프 이식은 체인 정책 + 프로토콜별 에러 빌더 필요한 설계 변경. ws 경로 CB는 이번에 추가.
- **`/model/*` passthrough** — wire 포맷/라우팅 프로파일 미적용은 ARCHITECTURE 문서상
  기존 known item.
- **ws 스트림 턴 idle timeout/keepalive/drain 부재** — total_deadline은 턴 시작만 게이트.
  별도 작업 후보.
- **레코드 성공 시점 = 헤더 도착** — 미드스트림 실패는 F4의 사후 record_failure로 상쇄.
- Codex 비-2xx 스트림 JSON 환원은 ERROR 행만 복원, body-log 스트림 레코드는 미보존(4xx/5xx 본문은 nonstream 로거가 커버 가능 — opt-in 기능이라 미구현 수용).

## 검증

- kiro 검증 라운드: must-fix 3건(PoolTimeout 분류, usage/bedrock_request_id 유실,
  last_error-None 마스킹 잔여) — 전부 수정 후 재검토 항목 클린 확인.
- 게이트웨이 전체: **1385 passed, 60 skipped** (회귀 +7: test_r14_fallback_review.py).
- helm template 렌더 검증: `MANTLE_HTTP_TIMEOUT: "270"` (dev idle 240+30).
