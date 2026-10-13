# 리뷰 라운드 — cost/usage 기록 파이프라인 (gateway finalize → Redis stream → worker → DB/budget)

대상: `cost_recorder.finalize` → `cost:stream` XADD → `stream_consumer`/`batch_flusher` → `usage_logs`/`budget_usages`/일별 카운터, + admission 추정치·시드·타임존 정합.

이전 라운드(R1–R6 인증/Cognito, R6–R10 pricing sync, UI-R1–R3 도넛)는 각각 CONVERGED. 이번이 청구 파이프라인 첫 라운드.

## 1차 심층 리뷰 결과 — High 3 + Med 4 + Low 3

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| F1 | **High** | Codex/Responses 스트림이 `response.completed` 전에 끊기면 usage 전량 0 → 예약 환불 + 기록 유실(과소청구) | admission 의 입력 추정치를 `rate_limit_state["estimated_input"]`에 싣고 finalize 가 대체 과금(추정 과금 게이트: `is_stream` + status≠ERROR) |
| F2 | **High** | `XADD MAXLEN~100k` 가 미소비 entry 도 트림 — 워커 stall 수 분이면 청구 기록 영구 유실 | 워커 post-ack `XTRIM MINID min(미처리 최솟값, last-delivered-id)`로 전환, MAXLEN 은 500k 메모리 백스톱 |
| F3 | **High** | 웹검색 멀티턴 합산 프롬프트로 장문 티어 판정 → 턴 단위 short 인 요청이 전량 long 요율(과대청구) | `TokenUsage.turn_usages` 턴 스냅샷 수집 + `calculate_cost`/`resolve_context_tier` 턴별 재귀 |
| F6 | Med | `seed_spent` 절대값 덮어쓰기가 동시 settle/예약 유실 | 델타 시맨틱(FOR UPDATE + `used_usd + :delta`, Redis 조건부 INCRBYFLOAT) |
| F7 | Med | 팀 없는 사용자의 지출이 default team 센티널에 합산 → 엉뚱한 팀 hard_block | additive `teamless` 플래그 — 워커 TEAM 스코프 집계 스킵 |
| F9 | Low | `to_char(requested_at,'YYYY-MM')` UTC 버킷 vs period(KST) 불일치 — 월 경계 몇 시간이 다른 달로 샘 | `to_char(timezone(reporting_tz, requested_at))` |
| F8 | Low | 단가 없는 활성 alias 가 $0 조용 통과 | `model_pricing_missing` ERROR 로그 |
| F4 | Med | Redis 열화 시 finalize 의 순차 호출이 응답을 수십 초 지연 | **Accepted residual** — 지연 문제지 정확도 결함 아님, 통합 breaker 는 별도 설계 필요 |
| F5 | Med | cost spool 이 인메모리 — 롤아웃/축출 시 유실 | **Accepted residual** — durable spool/직접 DB 폴백은 별도 작업 |
| F10 | Low | `web_search_count` 미과금(AgentCore WebSearch $7/1k 전가 여부 미결) | **Accepted residual** — 단가 스키마 확장 + 제품 결정 필요 |

## kiro(claude-opus-5.5) 검증 라운드 — 내 수정에 6 must-fix 추가 발견

| # | 발견 | 조치 |
|---|---|---|
| A2 | **입력 추정치에 `_gw_admission_input_mult` 배수가 섞임** — 끊긴 웹검치 요청이 iters 배 과대청구 | 배수 제외한 단일턴 추정치를 저장 |
| A3 | **출력 버킷까지 0 요구**라 스트림 헬퍼의 output 역산 후(input=0/out>0) 미발동 | 입력 3버킷만 검사, output 추정치 보존, `turn_usages` 클리어(B2 포함) |
| A1 | 비스트리밍 상류 4xx/5xx + bedrock 패스스루 504 에 추정 과금 가능 | `is_stream` 게이트 + bedrock 패스스루 비-2xx `release_reservations` 추가 |
| B1 | 요청 레벨 `cache_ttl_1h` 가 턴 스냅샷에 없어 1h 캐시가 5m 요율로 과소청구 | 재귀 시 플래그 전파 |
| C1 | `_trim_consumed` 폴백 레이스 — XPENDING 빈 사이 다른 소비자의 진행 중 배치를 last-delivered 로 지움 | XINFO 먼저 읽고 `min(pending최솟값, last-delivered)` 경계 |
| E1 | per-app 시드 델타가 총합 키에도 가산 → "총합+앱" 일괄 시드 시 이중계상 | 총합 키 쓰기 제거 |
| E2 | 없는 Redis 키에 INCRBYFLOAT → 델타만 든 키 생성 + 게이트웨이 복원이 키 존재로 스킵 → 한 달 내내 틀어짐 | `EXISTS` 게이트 Lua |
| E3 | 없는 행에 `FOR UPDATE` 무효 — 동시 첫 시드 델타 이중적용 | ensure-insert(0) 선행 + item 정렬 |
| C2 | 트림 실패가 debug 라 조용히 스트림 비대 | warning 격상 |

## 정합 확인(수정 불요)

- 이중청구 없음: `usage_fired` 가드 4개 스트림 출구 + `state.pop("rate_limit_state")` + `request_id` UNIQUE + 워커 재처리 필터
- `turn_usages` 는 `exclude=True` + `CostStreamEntry.make` 명시 필드 → 스트림 유출 없음, 메모리는 턴 수로 bounded
- XAUTOCLAIM deleted_ids/트림 로깅, dead-letter, malformed ACK 경로 무훼손
- `func.timezone(tz, requested_at)` 는 Postgres `timezone(zone, timestamptz)` 로 렌더 — `periods.period_at`(zoneinfo, DST 안전)과 동일 계약
- `teamless` 기본 False 하위호환, 구 워커는 extra 필드 무시(배포 순서: 워커 먼저여야 TEAM 집계 스킵 적용)

## 수용 잔여

- D1: default team 복원 폴백(`SUM(usage_logs) WHERE team_id`)은 여전히 teamless 행 포함 — `usage_logs.teamless` 컬럼 마이그레이션 필요(문서화)
- F4 finalize Redis 지연 통합 breaker, F5 durable spool, F10 웹검색 단가 — 위 표 참조
- E3 잔여: ensure-insert 와 worker upsert 의 극히 드문 교차는 남으나 데드락 아님
- `_anthropic_stream` 자체 필드합산 경로는 `_merge_usage` 미경유(B3) — Claude 장문 티어가 생기면 스트리밍만 합산 판정. 현재 threshold 는 카탈로그 `has_long`(GPT/Responses)에서만 유래해 무영향
- 턴별 quantize 후 재합산 — 오차 ≤0.5e-6/턴(docstring과 다른 미세 경로)

## 검증

- 회귀 테스트 +7: 추정입력 과금(전0/부분텍스트/비스트림/ERROR 4경로), 턴별 장문 판정+1h 플래그, teamless 페이로드·TEAM 집계 스킵, 트림 경계 3케이스, seed 델타+조건부incr+총합키 미기록
- gateway **1369**, worker **42**, admin **946** 전부 통과
