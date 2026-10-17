# R23 — cost-recorder-worker + notification-worker 리뷰 (kiro/opus, ponytail)

범위: 청구 파이프라인 워커 2종과 그 생산자/정산 계약 —
`cost-recorder-worker/src/worker/`(stream_consumer, batch_flusher, daily_aggregator,
worker, main, redis_client, config, observability) +
`notification-worker/src/worker/`(handlers, listeners, senders, services, schemas) +
교차검증 `gateway-proxy`(cost_recorder.finalize 정산, budget_service, fallback_loop,
budget_settle.lua) + deployment termination grace.

## 라운드 1 — NOT CONVERGED (M1 + S7 + N15)

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | 소비 정지(DB 장애·스키마 오류) 중 XADD MAXLEN~500K 백스톱이 **미배달** entry 절단 — `XREADGROUP '>'`이 갭을 조용히 건너뛰어 그 구간 usage_logs/budget_usages 영구 유실 | `stream_consumer._sample_lag`에 first-entry > last-delivered-id 갭 탐지 + `stream_gap_detected` 메트릭 + `cost_stream_delivery_gap` ERROR 로그(신생 그룹 `0-0`은 제외해 오탐 방지). 워커 측 `_trim_consumed`는 커밋 경계(pending 최솟값과 last-delivered 의 작은 쪽) 기준으로 XTRIM — 소비된 것만 잘라 MAXLEN 도달 자체를 지연 |
| S1 | SIGTERM 즉시 cancel이 commit~XACK 사이 배치 절단 → 재전달이 `_filter_replays`에 전량 스킵돼 임계 알림/일일 카운터(비멱등) 영구 생략 | `StreamConsumer.request_stop()` 협조 종료 — `_consume_live`/`_drain_backlog`가 배치 경계에서만 확인, `main.py`는 stop 요청 후 grace까지 완료 대기 |
| S2 | notification drain이 실패 이벤트만 재큐 — 배치 잔여(tail)가 인메모리에서 증발 | `notification_buffer.drain`이 `batch[i:]` 전체를 재큐 |
| S3 | notification TaskSupervisor 백오프가 누적만 함 — 반복 blip이 재연결 지연을 영구 60s로 | `_STABLE_RESET_SEC=300` — 오래 산 실행의 실패는 백오프 리셋(cost-recorder와 동일 패턴) |
| S4 | cost-recorder Redis 클라이언트에 socket timeout/keepalive/health check 부재 — 반절 연결의 XREADGROUP 무한 대기, liveness는 프로세스만 검사 | `socket_timeout`(xread_block_ms+α)/`socket_connect_timeout`/`socket_keepalive`/`health_check_interval` 설정 |
| S5 | gateway `finalize`가 `settled:false` 무시 — 마커 TTL 만료 시 정산이 no-op인데 실비 미반영(그 달 내내 과소계상) | `budget_settle.lua`에 `:done` 툼스톤 + ARGV4(예약치) — 툼스톤 없이 마커만 없으면 예약치만큼 델타 복구(`recovered`), 예약치 미상이면 안전 no-op. `fallback_loop.release_reservations`를 `state.pop` 선점으로 바꿔 drain/finalize 이중 정산 제거. `cost_recorder` 3개 스코프에 `budget_settle_noop`/`budget_settle_recovered` 로그 |
| S6 | 임계 알림 PUBLISH는 at-most-once — 구독자 0이면 기록 없이 유실 | `_publish_thresholds`가 publish 반환값 검사 → 구독자 0이면 `threshold_publish_lost` 메트릭 + 경고 로그 |
| S7 | 동일 수신자 쿨다운을 SETNX 선점 → 처리 실패 시 cooldown만 남아 후속 알림 억제 | 처리 완료 후 기록(GET 확인+SET)으로 변경 — 실패 시 재발송 가능 |
| N1~N8 | (cost-recorder) 드랍 경로 미계측·lag/pending 혼용·레플리카마다 daily cron·APScheduler misfire grace 1s·DB/Redis 집계 기준 불일치·죽은 consumer 누적·PEL 트림 항목 | `entries_dropped`/`flush_errors`/`stream_pending` 게이지 추가, daily_aggregator에 pg advisory lock+misfire grace 상향+request_count 정합, `_sweep_dead_consumers`(idle>1h+pending=0), XAUTOCLAIM deleted_ids 계측 |
| N9~N15 | (notification) PII/시크릿: mock_sender 수신자 평문 로그·`str(exc)`에 수신자·페이로드 로그·`sent`로 위장·비활성 수신자 재발송·retry 고갈 미계측·listen() 청각상실·종료예산>grace·영구 오류 재시도 | `email_mask.mask_emails_in_text`로 로그/저장 마스킹, mock status `mocked`, resend 시 `is_active` 재확인, `abandoned`/retry 메트릭, config_reload 리스너 get_message 하트비트, main 종료 순서 예산 조정, SES/SMTP 영구 오류 분류 |

### 검증
- cost-recorder-worker: **52 passed, 12 skipped** — 신규 회귀 5건(gap 탐지·신생그룹 오탐·배치경계 종료·dead consumer 스윕·구독자0 계측)
- notification-worker: **81 passed, 4 skipped** — 신규 회귀 4건(tail 재큐·백오프 리셋/누적 대조·텍스트 이메일 마스킹)
- gateway-proxy: **1443 passed, 60 skipped** — 신규 회귀 4건(pop 선점·마커만료 복구·툼스톤 no-op·예약치미상 no-op, 전부 fakeredis+lupa 실 Lua 실행)
- 테스트 수정: `test_r15_reliability` 쿨다운 계약 갱신(처리 후 기록), mock 상태 `is True` 체크

### 잔여 관찰(비차단)
- M1 갭 탐지는 "감지+알람" — 복구는 운영 조치(백필) 영역. 스트림 확장/스풀은 설계 변경이라 미적용
- S6 임계 알림 durable outbox는 별도 설계 — 현재는 유실 계측으로 가시성 확보
- `_day_window`의 `datetime.combine(tz=)`은 Asia/Seoul(무DST)에서 안전 — 다른 TZ 배포 시 재검토

## 라운드 2 — NOT CONVERGED (M1 + S4 + N8)

리뷰어가 실 Redis 7.4·실 PostgreSQL 16·실 Lua·실 코드로 전부 재현 검증.
r1 수정이 기댄 전제 4개("stop_all이 기다려준다", "마커 없으면 no-op",
"기록이 곧 억제", "세션=커넥션")가 실행 경로에서 성립하지 않아 회귀 발생.

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| (추가) | r1의 `break→continue` 수정이 빈 PEL 응답 `[[stream,[]]]`(truthy, 실 Redis 확인)에서 `_drain_backlog` 무한루프 — 기동 시 `_consume_live`에 도달 불가, 소비 전면 정지 | 리뷰 중 발견·즉시 `if not ids: break` 가드 + 회귀 테스트(빈 페이지 1회에서 종료) |
| M1 | advisory lock 커넥션 누수 — 날짜별 `commit()`이 커넥션을 풀에 반납 → `finally`의 `pg_advisory_unlock`이 다른 커넥션에서 실행돼 락 고착, 전 replica가 영구 스킵 → `daily_aggregates` 공백 → `/v1/usage/me` 과거일 누락 (PG16 재현: unlock=false, 다른 replica try_lock=false) | 락 전용 AUTOCOMMIT 커넥션(`engine.connect()`)에서 획득·해제 — 커넥션 종료가 곧 해제라 고착 경로 제거. `skipped_lock_held` INFO→WARN(고착 신호) |
| S1 | `stop_all`이 대기 **전에** `cancel()` — `request_stop()`이 무효(flush 중 종료 시 xack 0건 재현) | `stop_all(cooperative_window=)` 2단계 — 자연 종료를 먼저 기다리고 잔여만 cancel. `main.py`는 `batch_interval+15s` 창 부여. `run()` 진입 시 `_stop_event` 조기리턴(종료 중 재시작 방지), `_reclaim_loop`/`_reclaim_orphans`도 플래그 확인, claim_task 즉시 cancel 대신 15s 자연종료 대기 |
| S2 | reserve eval 실패 환불이 `"reserved": est`를 넘겨 복구 경로 발동 — 받지도 않은 예약치 차감(실 Lua: 50→45, resvsum −5) | S-3의 생존검증으로 해소 — 인덱스 필드가 없으면(예약 미실행) reserved=0 → delta=0 |
| S3 | 복구 경로가 "예약치가 카운터에 남아있다" 전제 — reconcile/rebuild가 걷어낸 예약을 이중 차감(실 Lua: 기대 53 → 48, resvsum −5) | `HEXISTS pendingidx field`(reserve만 HSET, settle/rebuild만 HDEL → 생존 증거) + `reserved=min(caller, max(resvsum,0))` 상한. 인덱스 미제공 호출자는 resvsum 근거. 이미 걷어졌으면 reserved=0 → 실비만 적용 |
| S4 | 쿨다운 "확인→처리→기록" TOCTOU — 같은 정체성 이벤트 4건(다른 event_id) 동시 도착 시 메일 4통(실 BaseHandler 재현) | `SET key <event_id> NX EX` 선점 + 실패 시 `GET==event_id`(fan-out 복사본)만 통과 + 인프라 실패 시 compare-and-del 해제. 죽은 claimer의 고아 키는 TTL 해소 + CAS 해제로 기아 방지 |
| N1~N8 | cluster PUBLISH 오탐·drain CancelledError 유실·requeue 순서·툼스톤 TTL 300<14400·pop-전 redis 검사·갭 건수 추정·inflight=stop_all 경합·락스킵 INFO | PUBLISH 구독자0을 WARN으로(cluster는 발행노드 구독자만 카운트), drain에 `except BaseException` 재큐+재전파, 툼스톤 `EX 14400`(마커 TTL과 동일), redis-None 검사를 pop 앞으로, 갭 로그에 `estimated_lost`(entries-added−entries-read−lag), stop_all 15→18s, `skipped_lock_held` WARN |

### 검증
- cost-recorder-worker: **53 passed, 12 skipped** — 빈 PEL 종료 회귀 추가
- notification-worker: **83 passed, 4 skipped** — 쿨다운 동시버스트(4건→1통, dict-redis 실제 경쟁) 추가, r15 쿨다운 4건을 클레임 계약으로 갱신
- gateway-proxy: **1446+ passed** — 스윕 후 실비적용·미실행 환불 no-op·툼스톤 no-op 회귀 추가(실 Lua)

### 잔여 관찰
- 복구 생존검증은 reconcile이 인덱스 필드를 HDEL하지 않는 창(스윕~rebuild 사이)에 resvsum>0이면 bounded(min) 오차 가능 — resvsum 상한으로 피해 제한
- N3 재큐 순서 역전(80%→90% 알림 순서) — 중복은 슬롯이 막으므로 수용
- `daily_aggregates` 조회는 `/v1/usage/me` 과거일 — 락 고착 회귀는 WARN 로그로 가시화

## 라운드 3 — NOT CONVERGED (M1 + S1 + N8)

리뷰어가 실 Redis 7.4·실 PostgreSQL 16에서 Lua 11개 시나리오·풀 churn·
`pg_terminate_backend`·종료 시퀀스까지 전부 재현.

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | `_AGG_SQL` 주석 속 `:requests`를 SQLAlchemy가 바인드 파라미터로 파싱 → 실 PG 실행이 "A value is required"로 실패, 일일 집계 전면 정지(리뷰어·Devin 동시 독립 발견) | 주석에서 `:` 제거 + 바인드 파라미터 화이트리스트 회귀 테스트(3개 SQL 전수) |
| S1 | 동시 예약 scope에서 복구 오분기 — reconcile이 orphan을 걷어내면서 인덱스 dead 필드를 HDEL하지 않아 HEXISTS 생존검증이 미정리 예약을 라이브로 판정, 다른 요청의 resvsum을 차감(실 Lua: 최종 55/-5, 기대 60/0) | `budget_reconcile.lua`가 KEYS[3]에 인덱스 키·ARGV[3]에 마커 접두사를 받아 dead 필드를 상시 스윕(마커 부재는 확정 증거라 퀴에센스 무관). health_checker가 scope별 idx/접두사 배선. 회귀 테스트는 시나리오 E 전체를 실 Lua로 재현 |
| N1 | 인덱스·마커 TTL이 같아 정상 만료 후 늦은 settle이 복구 못함(used 58/resvsum 5 누수 — 4h 초과 요청 한정) | reserve 인덱스 EXPIRE를 `marker_ttl×2`로 — 필드가 마커보다 오래 살아 생존검증이 정확히 분기 |
| N2 | rebuild(인덱스 모드) 후 늦은 settle은 usage에 고아 예약치 과대 계상 잔류(보수 방향) | settle.lua 주석 정정 + 한계 문서화(플로어 설계상 usage는 안 내림) |
| N3 | 웜업/구버전 경로에서 resvsum이 음수로 갈 수 있어 표시 실지출이 어긋남 | settle 차감 후 resvsum<0이면 0 플로어(불변식 >=0) |
| N4 | 툼스톤 TTL이 상수 14400 — 마커 TTL을 늘리면 재적용 창 재발 | 스펙이 `marker_ttl`을 보존해 settle ARGV[5]로 전달(호출부 4곳+settle_budget) |
| N5 | `run()` finally의 15s reclaim 대기가 stop 미요청 크래시에도 걸려 재시작이 15s+backoff 지연(실측 15.11s) | `stop_event.is_set()`일 때만 협조 대기, 크래시 경로는 즉시 cancel |
| N6 | 락 커넥션 도중 단절 시 unlock이 InterfaceError로 성공 실행을 failed로 오기록(실 PG `pg_terminate_backend` 재현) | unlock을 try/except→warning(서버 측 자동 해제라 잔존 0 확인됨) |
| N7 | `_BACKFILL_SQL`이 여전히 `COUNT(*)` — 백필 날짜의 request_count가 Redis 정의와 어긋남 | `_AGG_SQL`과 동일한 FILTER로 통일 |
| N8 | 쿨다운 `SET NX` 실패→`GET` 사이 키 만료 시 holder=None을 오판해 1회 억제(마이크로 창) | 선점+holder 판독을 단일 Lua로(1=점유/2=fan-out/0=억제) — r15/r23 테스트를 eval 계약으로 갱신 |

### 검증
- gateway-proxy: **1446 passed, 60 skipped** — 시나리오 E(reconcile→settle A/B2) 실 Lua 회귀 추가
- cost-recorder-worker: **55 passed, 12 skipped**
- notification-worker: **83 passed, 4 skipped**

### 범위 밖 발견(후속 라운드)
- `admin-api/src/app/scheduler/main.py:33-62` — AsyncSession 위 세션 레벨 advisory lock 패턴이 r2 M-1과 같은 계열. 잡 본문의 중간 commit 여부 미검증 → R25(admin-api)에서 확인 필요

## 라운드 4 — NOT CONVERGED (M1 + S1)

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | **r2 회귀**: `:done` 툼스톤이 `budget:pending:` 네임스페이스 **안**에 있고 값이 실비 — reconcile·웜업 `_pending_reservation_sum`·`gateway-cli` 복원 스크립트가 라이브 마커로 오인해 직전 4h 정산 실비를 재합산(실 HealthChecker 재현: 95/5 → 135/45). team scope 오탐 HARD_BLOCK, 표시값은 정상이라 진단 어려움 | 툼스톤을 `budget:settled:` 네임스페이스로 이동(`'budget:settled:' .. sub(marker,16)` — 해시태그 동일, 슬롯 동일). 세 스캐너 전부 자연 배제 |
| S1 | **HEAD 기존 결함**: 전역 SCAN → 행별 resvsum 스냅샷 사이에 들어온 예약이 퀴에센스 게이트를 통과한 채 고아로 차감(실측 62→55, 차단 우회 방향, 틱 누적). N-3 플로어가 음수 신호까지 덮음 | reconcile의 live를 eval 내부에서 **인덱스∪스캔 합집합**으로 원자 계산 — 인덱스 필드 우선, 스캔이 넘긴 미커버 키(구버전 파드 마커, 웜업 창용)만 추가. SCAN~스냅샷 창의 예약이 인덱스 경유로 잡힘 |
| N | 상시 HDEL이 스냅샷 어긋난 틱에도 실행돼 부분 정리 | dead-스윕을 `resvsum==snap` 퀴에센스 분기 안으로 이동 |
| N | 고아 필드 누적(정확성 무해, HKEYS 비용만) | 수용 — rebuild/reconcile 스윕이 정리 |
| N | 실 PG 집계 회귀 테스트 미추가 | 수용 — 바인드 파라미터 화이트리스트 테스트가 결함 계열을 결정적으로 가드(PG 의존성 없이 매 실행). 실 PG는 리뷰어가 수동 검증 완료 |

### 검증
- r23 회귀 14/14 — 툼스톤 위치·union live·시나리오 E 전수 실 Lua 재현
- 리뷰어 실증: PROOF_DSN/REDIS_PROOF_URL 지정 시 cost-recorder 67 passed

## 라운드 5 — NOT CONVERGED (M1 + N3)

리뷰어가 r4 수정분을 실 Redis로 전수 재검증 — 접두사 길이·툼스톤 분기·
union live 3시나리오·M-1 재현(95/5) 전부 확인.

### 수정 완료

| ID | 발견 | 조치 |
|---|---|---|
| M1 | **client scope reconcile이 공유 인덱스의 모든 앱 예약을 합산** — 인덱스는 유저당 하나인데 카운터는 앱별이라 claude-code 행 교정에 codex 예약이 부픔(실측 15/5 → 24/14, 열화 창마다 오탐 HARD_BLOCK) | reconcile에 `field_prefix` ARGV 추가(rebuild와 같은 계약) — client 행은 `"<client>:"`, 나머지 `""`. live 합산만 필터, dead 스윕은 앱 무관 전역 위생 유지 |
| N1 | 배포 이전 `:done` 툼스톤 잔재가 스캔에 합산될 수 있음(이행기, 기대 95/5 → 107/17) | `:done` 접미 키를 스캔에서 배제 — health_checker 파서 + `_pending_reservation_sum` + `gateway-cli` 복원 스크립트 3곳 |
| N2 | covered 딕셔너리 비용(필드 수 비례) | 수용 |
| N3 | 비숫자 마커 값이 dead로 지워짐(정상 경로에선 불가) | 수용 |

### 검증
- r23 회귀 15/15 — client scope 앱별 필터·cross-app dead 스윕 실 Lua 재현

## 라운드 6 — CONVERGED (M0 + S0 + N2)

리뷰어가 실 Redis 7.4 + 실 `HealthChecker._reconcile_budget_counters`로
30개 하네스 재검증(29 통과, 1 실패는 의도된 탐색 케이스). 라운드 5의
M-1(cross-app 합산)·N-1(`:done` 필터) 모두 해소 확인.

### 잔여 N 처리

| ID | 발견 | 조치 |
|---|---|---|
| N1 | dead 스윕이 앱 무관이라 다른 앱의 만료 필드를 지우면 그 앱의 늦은 settle이 생존 증거를 잃어 예약 복구 불가(cx 18/9 vs 기대 9/0 — 과대 계상 방향, 마커 TTL 초과+열화 창+앱 첫 사용 3중 조건) | dead 분류에도 `field_prefix` 적용 — 각 앱 행이 자기 필드를 스윕, 미사용 앱은 인덱스 TTL(마커×2)이 거둠. 회귀 테스트 갱신(자기 앱 dead만 스윕) |
| N2 | client 행 `field_prefix` 회귀 테스트 부재 | `test_reconcile_client_scope_sums_only_own_app_fields`가 라이브/dead/필터 무력화를 커버 |

### 최종 검증
- gateway-proxy: **1449 passed, 60 skipped**
- cost-recorder-worker: **55 passed, 12 skipped**
- notification-worker: **83 passed, 4 skipped**

### 범위 밖 잔여(후속 라운드 인수)
- `admin-api/src/app/scheduler/main.py:33-62` — 세션 레벨 advisory lock이 r2 M-1과 같은 계열(잡 본문 중간 commit 시 커넥션 고착) → R25에서 확인
- rebuild의 앱 무관 dead 스윕에도 N-1과 같은 잠재 결함 — 기존 코드, 동일 논리로 후속 처리 권고
