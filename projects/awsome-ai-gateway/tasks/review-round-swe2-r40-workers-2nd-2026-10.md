# R40 — workers/scheduler + cost 파이프라인 정합 2차

날짜: 2026-10 · 대상: cost-recorder-worker(~2.5k), notification-worker(~2.3k), 발행↔구독 계약, 스케줄러.
베이스라인: cost-recorder 55p/12sk · notification-worker 83p · gateway 1459p(+9 신규).

## 수정 — `notifications:system` 무발행 채널 연결

notification-worker 는 이 채널을 구독하고 SystemHandler·템플릿 3종·시드
(notification_configs → admin)가 모두 준비됐지만, **어디서도 이 채널에 발행하지
않았다** — `degradation_mode`/`service_health_change`/`provider_error` 이벤트가
한 번도 전달되지 않은 기능 갭.

| 파일 | 내용 |
|---|---|
| `gateway-proxy/src/app/services/degradation_notifier.py` (신규) | 전이/회로 이벤트 → `notifications:system` 발행. fire-and-forget(루프 비차단), 발행 실패는 로그만 |
| `degradation/manager.py` | `set_transition_listener` hook — `_transition_to` 가 실제 전이마다 콜백 (old,new) |
| `services/fallback_loop.py` | `cb.record_failure` 반환(회로 OPEN) 시 `notify_provider_error` — 새 `degradation_notifier` kwarg |
| `routers/messages.py` | 웹서치 CB·미드스트림 CB 의 `record_failure` 2곳에 동일 발행 + `run_fallback_loop` 에 notifier 전달 |
| `routers/openai_compat.py` | responses 웹서치 CB `record_failure` 에 동일 발행 |
| `main.py` | `DegradationNotifier(redis)` 1개 생성 → 전이 리스너 + `app.state` (fallback 호출부 공유) |
| `tests/unit/test_degradation_notifier.py` (신규) | 9건 — 열화/복구/부분복구 매핑, 무발행, 발행 실패 격리, 템플릿 필드 계약, dedup |

매핑: 열화 심화 → `degradation_mode` (mode/reason/affected_providers), 완화·복구 →
`service_health_change` (service_name/status/previous_status). 회로 OPEN →
`provider_error` (provider_name/error_type/occurred_at). 모두 payload 에 worker
쿨다운 dedup 정체성 필드(service/provider)를 싣고, provider_error 는 pod 로컬
300s dedup 도 추가(OPEN 지속 중 요청당 1건 발행 방지).

## 검증된 정상 (변경 없음)

- **stream_consumer**: 고아 PEL XAUTOCLAIM 회수, dead-letter grace+경로별 streak 분리,
  커밋 경계 XTRIM(pending 최솟값∩last-delivered), lag/갭 샘플링, 미지원 스키마 loud 신호.
- **batch_flusher**: 배치 내 dedup → replay 필터 → INSERT RETURNING 기준 가산의
  3중 이중청구 방어. 22/23 클래스=행 poison per-row 소화, 42 클래스=배포 문제 전파의
  sqlstate 판별이 정확. per-app 예산 행(총합과 별도 누적)이 복원·폴백 경로의 SoT.
  threshold 누적 조회 + PUBLISH 구독자 수 확인(Cluster 경고)까지.
- **daily_aggregator**: 전용 AUTOCOMMIT 커넥션의 advisory lock(풀 고착 방지), 멱등
  UPSERT, 통째 누락일 백필, GROUP BY=충돌키 입도, COUNT FILTER가 Redis 카운터 정의와 일치.
- **notification-worker**: atomic claim+펜싱 토큰 재검증+하트비트의 sweeper,
  dead-claim 인수(300s), abandoned 터미널+24h 연령 상한, half-open TCP heartbeat,
  쿨다운 선점 Lua(claim/compare-and-del), infra-error 버퍼→health-check 드레인.
- **계약 정합**: `_PER_APP_CLIENTS`↔`PER_APP_BUDGET_CLIENTS` 는 AST 회귀 테스트가
  수동 동기화를 감시(`test_high_per_app_budget_rows`). `periods.py` 가 reporting-TZ
  경계의 단일 소스 — gateway `date`/`period` 와 aggregator `AT TIME ZONE :tz` 가 동일
  기준. 채널 맵: `notifications:budget`←cost_recorder, `:key`←admin-api key_service,
  `:security`←event_detector — 전부 구독과 일치.

## 판정 보류

- **`notifications:key` 의 KEY_EXPIRING/EXPIRED 발행자 없음** — 의도된 상태(api-key-helper
  자동 갱신, 시드 주석 명시). 발행자가 생기면 `enabled` 로 제어.
- **알림 채널 자체는 lossy pub/sub** — 미구독 중 발행은 sweeper 도 못 잡음(설계 한계,
  배치 발행자 쪽 구독자 수 WARN 로 커버).
- **provider_error pod 로컬 dedup** — 파드 수만큼은 발행될 수 있으나 worker 쿨다운
  (600s, provider 키)이 최종 수신자 발송을 1건으로 억제 — 의도된 2단계.
