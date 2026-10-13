# R12 — 레이트리밋/예약 경로 심층 리뷰 (kiro opus-5.5 + 자체)

대상: gateway-proxy `enforce_rate_limits` → `rate_limit_service`(RPM/TPM/CPM/CPH Lua) → `budget_service`(reserve/settle/rebuild) → `fallback_loop.release_reservations`/`unsettled_reservation_guard` → drain 태스크 정산.

## 수정됨

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| H1 | **High** | **RPM 롤백 영구 no-op** — Lua 가 `request_id:1` 로 ZADD 하는데 `_zrem_committed` 는 bare id 로 ZREM. 거절된 요청이 전 스코프 RPM 슬롯을 윈도우 끝까지 물어 재시도 기아 루프 | `zrem(k, request_id, f"{request_id}:1")` |
| H2 | **High** | **disconnect 시 guard↔drain 경합** — guard 가 sleep(0) 후 release(0) 로 이기면 뒤의 settle(actual) 이 마커 부재로 no-op(실비 유실) + TPM/CPM 이중 환불(음수 카운터) | streamer 가 drain 스폰 전 `outcome["drain_pending"]` 설정·finally 해제, 라우터가 `state["_stream_outcome"]` 으로 노출, guard 가 플래그 동안 drain_timeout+10s 까지 대기 후 release |
| H3 | **High** | **DB blip → GLOBAL 포함 전 한도 300s unlimited 캐시** — 공유 세션이 첫 실패 후 aborted 라 후속 스코프 쿼리도 연쇄 실패·캐시 | 실패 시 `db.rollback()` + `continue`(미캐시 — 다음 요청이 DB 재시도) |
| M1 | Med | CPM/CPH 카운터에 모델 차원 없음 — 모델별 한도를 전 모델 합산 카운터에 대조 | `user_key_alias`/`team_key_alias` 파라미터, 키 `rl:cost:{scope}:{sid}:{alias}:{cpm|cph}:{ts}`, `CostLimitResult.committed_keys` + settle 직접 키 경로 |
| M3 | Med | rebuild `EXISTS` 게이트가 음수 팬텀 카운터(축출 키 위 settle 델타)를 못 잡아 월 한도 해제 | 게이트를 `GET` 기반으로 — 없거나 <0 이면 rebuild(base+cur 가 양수 델타 보존) |
| M5 | Med | `reserve_budget` eval 실패가 `rl_fail_mode` 무시하고 항상 fail-open | closed 이면 `PermissionError("budget_backend_unavailable")`(429 + 친화 메시지) |

## 수용 잔여(문서화)

- M2: 고아 예약 회수가 Redis 복구 후 reconcile 창에만 의존 — 주기적 reconcile 스케줄은 별도 작업
- M4: `reserved > limit` 인 요청은 영구 거절인데 429+Retry-After 반환(재시도 유도 오해) — 거절 자체는 정확, 응답 어휘 개선은 계약 변경 필요
- L1: settle_tpm win 마커 읽기→INCRBY 가 비원자(분 경계 레이스) — Lua 화 후보
- L2: 웹서치 admission 첫 `max_*` 키 채택 vs `_extract_max_output` 최대값 — 출력 과소예약 가능
- L3: `_sanitize_media_blocks` 가 OpenAI file/input_file base64 미커버 — 대형 첨부 과대추정→M4 영구거절 연쇄
- L5: eval 타임아웃 시 실커밋 예약 고아화(버킷 TTL 회수) — TPM/CPM 마커 없음
- L6: 팀 config 캐시 TTL 만료 창에 허위 team_budget_unset 가능
- L7: fail-closed 429 헤더 `X-RateLimit-Limit:-1`, CPH retry_after=3600 고정
- L8: middleware 의 no_team_assigned 게이트가 선행하므로 현재 무영향 — 방어적 일관성만
- M1 잔여: 배포 윈도우 동안 구형식 키에 쓰인 예약이 신형식 키에 정산되지 않음(≤2h 윈도우, 자연 소멸)

## 검증

- 회귀 테스트 +7 (`tests/regression/test_r12_rate_limit_fixes.py`): zrem 멤버 양형태, guard drain 대기/데드라인, loader rollback+미캐시, cost 키 alias, committed_keys 정산, fail-open/closed 분기
- gateway **1377 passed, 60 skipped**
