# R23 라운드 3: 라운드 2 수정분 검증 결과

범위: 라운드 2 지적 수정분 전체(`git diff HEAD`, 24 files). cost-recorder-worker, notification-worker, gateway-proxy 정산 경로.

## 검증 방법 (실행 증거)

- 실 Redis 7.4(docker): `budget_reserve/settle/reconcile/rebuild.lua` 원본을 그대로 `register_script` 로 실행했다. 시나리오는 11개(A~J, E')다.
- 실 PostgreSQL 16(docker): 실 `create_db_engine`/`create_session_factory` 위에서 실 `run_daily_aggregation` 을 실행했다. 동시에 풀 churn 태스크 3개를 돌렸다.
- 실 Redis 위에서 실 `StreamConsumer` + 실 `TaskSupervisor` 로 main.py 12단계 종료 시퀀스를 그대로 재현했다.
- 실 Redis(`decode_responses=False`, bytes)에서 실 `BaseHandler._claim_cooldown/_release_cooldown` 을 실행했다.
- 기존 스위트는 모두 통과한다: cost-recorder 55 pass / notification 83 pass / gateway-proxy 1445 pass. 다만 **아래 M-1 은 이 스위트에 잡히지 않는다.**

---

## M급

### M-1 (신규, r1 diff 회귀) `_AGG_SQL` 주석의 `:requests` 가 바인드 파라미터로 파싱됨 → 일일 집계가 매번 실패
- 위치: `cost-recorder-worker/src/worker/daily_aggregator.py:80` (`-- Redis :requests 카운터가 ...`)
- SQLAlchemy `text()` 는 SQL 주석 안의 `:name` 도 바인드로 인식한다. `text(_AGG_SQL)._bindparams` 의 결과는 `['end', 'requests', 'start', 'tz']` 다. HEAD 에는 이 주석이 없다(`grep -c ":requests"` = 0). r1 의 COUNT FILTER 수정과 함께 들어왔다.
- 실 PG 재현: `run_daily_aggregation` 을 호출하면 `InvalidRequestError: A value is required for bind parameter 'requests'` → `daily_aggregator.failed` 가 난다. `backfill_missing_days` 도 같은 `_aggregate_window` 를 쓰므로 함께 죽는다. 결과적으로 `usage.daily_aggregates` 가 갱신되지 않고 `/v1/usage/me` 의 과거일 사용량이 빠진다. 라운드 2 M-1(락 고착)과 사용자에게 보이는 증상이 같고, 원인만 다르다.
- 기존 테스트에 안 잡힌 이유: `test_daily_aggregator.py` 는 `session.execute` 를 AsyncMock 으로 대체한다. 그래서 바인드 검증이 일어나지 않는다.
- 최소 수정: 주석에서 콜론을 뺀다(`Redis requests 카운터`). 회귀 테스트 두 개를 추가한다.
  - `assert set(text(_AGG_SQL)._bindparams) == {"start", "end", "tz"}` (세 SQL 상수 모두)
  - PROOF_DSN 실 PG 테스트로 `run_daily_aggregation` 1회 실행 후 행 생성 확인
- 주석을 고친 뒤 재실행하니 12일치 행이 생성됐다. 0토큰 마커 행이 있는 날은 `request_count=1` 로 FILTER 가 의도대로 동작했다.

---

## S급

### S-1 (S-3 잔여) HEXISTS 생존검증이 reconcile 과 맞지 않음: 동시 예약이 있는 scope 에서 S-3 이 여전히 재현
- 위치: `gateway-proxy/src/app/redis_scripts/budget_settle.lua:66-78` ↔ `budget_reconcile.lua`
- settle 의 계약은 "인덱스 필드가 있으면 아직 미정리 예약"이다. 그런데 `budget_reconcile.lua` 는 orphan 을 usage·resvsum 에서 걷어내면서 **인덱스 필드를 HDEL 하지 않는다**. 인덱스 키를 받지도 않는다. 그래서 HEXISTS=true 가 되고, `min(caller, resvsum)` 상한이 **다른 요청의 라이브 예약분**을 차감한다.
- 실 Lua 재현(E): 아래 순서로 진행했다.
  1. used 50 상태에서 A 5, B2 7 을 예약한다.
  2. A 마커가 소실된다.
  3. reconcile 이 실행된다(usage 57, resvsum 7, idx `[A, B2]`).
  4. A 를 settle(3) 하면 `55 / resvsum 2` 가 된다.
  5. B2 를 settle(7) 하면 **`used 55, resvsum -5`** 가 된다.

  기대값은 60/0 이다. HARD_BLOCK 카운터가 예약치만큼 과소 계상된다. 평상시엔 자가 치유 경로가 없다(rebuild 는 sentinel 이 있을 때만 돌고, reconcile 은 열화 창에서만 돈다).
- 단독 scope(B)에서는 resvsum=0 이라 우연히 맞는다. team scope 는 부하 중 동시 예약이 거의 상시라 실질적으로 미해결이다. 다만 r2 코드보다 나빠지지는 않았다(같은 값).
- 트리거 조건: 인덱스는 남고 마커만 소실(volatile 축출 등) + 소실과 settle 사이에 REDIS_DEGRADED reconcile 창 + 동시 예약. failover 유실은 reserve 의 효과가 통째로(마커·인덱스·INCR) 사라지므로 해당하지 않는다. 이 경우는 HEXISTS=false 로 올바르게 분기한다.
- 최소 수정: reconcile 이 orphan 을 적용하는 분기(`resvsum == snap`)에서 인덱스의 dead 필드를 HDEL 한다. 이를 위해 인덱스 키와 마커 접두사를 KEYS/ARGV 로 넘기고, rebuild 와 같은 HKEYS+GET 루프를 쓴다. 해당 `pendingidx` 키는 health_checker 가 scope 별로 이미 구성할 수 있다.
- 시뮬레이션(E'): 같은 시나리오에서 reconcile 직후 `HDEL idx A` 를 하면 `60 / 0` 으로 정확하다.
- 회귀 테스트: 시나리오 E 를 실 Lua/fakeredis 로 추가한다.

---

## N급

- **N-1 단독 요청의 TTL 정상만료(D)가 r1 대비 회귀했다.** 이 경우 마커와 인덱스 해시가 같은 reserve 에서 같은 TTL 을 받는다. 다른 예약이 해시 TTL 을 갱신하지 않으면 둘이 동시에 만료되고, HEXISTS=false → reserved=0 이 된다.
  - 실 Lua(`EX 1` 후 settle 3): `used 58, resvsum 5` (기대 53/0). 예약치만큼 과대 계상(보수 방향)되고 resvsum 이 누수된다.
  - 4h 를 넘는 요청에서만 발생한다.
  - 수정: reserve 의 인덱스 `EXPIRE` 를 `marker_ttl + 여유`(예: ×2)로 둔다. 그러면 필드가 마커보다 오래 살아 C 경로(정확)로 간다. 고아 필드는 rebuild 가 청소한다.
- **N-2 rebuild(인덱스 모드) 후 늦은 settle(F)은 과대 계상된다.** rebuild 는 dead 필드를 HDEL 하고 resvsum 을 live 로 내린다. 하지만 usage 는 플로어 설계라 **내리지 않는다**. 고아 예약치가 usage 에 남은 채 HEXISTS=false 가 되고, `+actual` 만 적용된다.
  - 실 Lua: `65 / 0` (기대 60/0).
  - settle.lua 주석의 "rebuild 가 고아 예약을 이미 걷어냈거나"는 사실과 다르다(rebuild 는 resvsum 만 걷는다). 보수 방향이다. 주석을 정정하고 의미론을 문서화한다.
- **N-3 rebuild 웜업 모드(G)·구버전 4키 호출자(J)는 resvsum 이 음수가 된다.** G 는 usage 는 맞지만 resvsum 이 -5 라 표시 실지출이 틀린다. J 는 E 와 같은 과소 계상(55/-5)이다. 둘 다 롤링/웜업 창 한정이다.
- **N-4 툼스톤 TTL 이 상수 `14400` 이다.** 마커 TTL 은 `budget_reservation_ttl_seconds` 로 설정 가능하다. 운영자가 TTL 을 늘리면 N-4(r2)가 재발한다. reserve 의 ARGV[4] 처럼 TTL 을 인자로 받거나 settle 에 같은 값을 넘긴다.
- **N-5 `run()` finally 의 15s 대기가 stop 미요청 크래시에도 걸린다.** `stream_consumer.py:153-160`.
  - 실 Redis 재현: `_consume_live` 가 예외로 죽자 `run()` 반환까지 **15.11s** 가 걸렸다. reclaim 루프는 stop 이벤트가 없어 안 끝나므로 timeout 까지 다 기다린다.
  - 결과적으로 Redis blip·flush 실패 크래시마다 재시작이 15s + backoff 만큼 늦어진다. 유실은 없다.
  - 수정: `if self._stop_event.is_set(): await asyncio.wait(...)`, 아니면 즉시 cancel.
- **N-6 락 커넥션이 집계 도중 끊기면 처리가 틀어진다.** 실 PG `pg_terminate_backend` 로 재현했다.
  - 집계는 락 없이 끝까지 진행되고, finally 의 unlock 이 `InterfaceError: connection is closed` 를 낸다. 그러면 성공한 실행이 `daily_aggregator.failed` 로 기록된다.
  - 락은 서버 측에서 자동 해제되므로 다음 실행은 정상이다(locks held = 0).
  - 수정: unlock 을 `try/except → warning` 으로 감싼다.
- **N-7 `_BACKFILL_SQL`(첫 기동 전량 백필)은 여전히 `COUNT(*)` 다.** `ON CONFLICT DO NOTHING` 이라 최근 8일 밖의 날짜는 이 정의로 남는다. 두 저장소의 request_count 정의가 다시 어긋난다. `_AGG_SQL` 과 같은 FILTER 로 맞춘다.
- **N-8 쿨다운 `SET NX` 실패 → `GET` 사이에 키가 만료되면 문제가 생긴다.** holder=None ≠ event_id 라서 창이 끝난 직후의 이벤트가 1회 억제된다. 마이크로 창이다. 고치려면 `SET NX` 와 `GET` 을 하나의 Lua 로 묶는다.
- **범위 밖 참고: admin-api 에 같은 결함 계열이 있다.** `admin-api/src/app/scheduler/main.py:33-62` 는 AsyncSession 위에서 세션 레벨 `pg_try_advisory_lock` 을 잡고 같은 세션에서 unlock 한다. 잡 본문이 중간에 commit 하면 라운드 2 M-1 과 같은 풀 커넥션 고착이 생긴다. 잡 본문의 commit 여부는 확인하지 않았다(R23 범위 밖).

---

## 검증 OK (수정이 결함을 실제로 해결)

| 항목 | 근거 |
|---|---|
| r2 M-1 advisory lock 누수 | `session_factory.kw["bind"] is engine` → `True` (`AsyncEngine`). 실 PG + 풀 churn 3개 조건에서 (1) 정상 실행 후 잔존 락 0, (2) 2-replica 동시 실행 시 집계 태스크 1개 + 나머지 `skipped_lock_held`(WARN) + 잔존 0, (3) 집계 중 예외(aborted tx)에도 잔존 0, (4) 락 백엔드 강제 종료 후에도 잔존 0이고 다음 실행 정상. 락 커넥션이 AUTOCOMMIT 전용이라 집계 세션의 per-day commit 과 독립이다. ※ 이 검증은 M-1(`:requests`) 주석을 고친 상태에서 했다. |
| 키 부재 경로 | `create_session_factory` 는 엔진을 위치 인자로 넘기므로 `kw["bind"]` 가 항상 존재한다. 다른 호출자는 없다(`run_daily_aggregation` 호출처는 main.py 1곳). 키가 없으면 KeyError → `daily_aggregator.failed` 로 fail-loud 하며 락 없이 돌지 않는다. |
| r2 S-1 2단계 stop_all | 실 Redis + 실 StreamConsumer/TaskSupervisor 로 재현: flush 0.6s 진행 중 `request_stop()` + `stop_all(30, cooperative_window=20)` 을 실행하면 **committed 5 / PEL 0 / 0.76s 자연 종료 / task_cancelled=False** 다. 협조 창이 flush 보다 짧은 경우(0.3s < 2s)는 cancel 되고 PEL 5 가 재전달 대상으로 보존된다(유실 없음). `run()` 이 정상 반환하면 supervisor 는 `task_exited_normally` 후 break 하므로 재시작 스핀이 없다. |
| `stop_all` 하위호환 | cost-recorder `TaskSupervisor.stop_all(grace_period)` 만 호출하면 `cooperative_window=0` 으로 기존 즉시-cancel 동작과 같다(재현: committed 0 / PEL 3). |
| notification-worker cooperative_window 부재 | **의도된 것이고 맞다.** 별개 클래스다. 리스너는 pub/sub 이라 배치 경계·ACK 가 없다. cancel 이 곧 협조 신호다: `ChannelListener.run` finally 가 unsubscribe 후 in-flight dispatch 태스크(별도 태스크라 cancel 되지 않음)를 `_INFLIGHT_GRACE_SEC=15` 까지 기다린다. 예산은 `stop_all(18)` + drain(10) = 28 < terminationGracePeriodSeconds 30(values.yaml:949). |
| `_reclaim_loop` 의 `wait_for(event.wait(), timeout)` | 실 Redis에서 interval 0.2s 로 0.7s 동안 `_reclaim_orphans` 를 4회 호출했다(정상 주기). stop 시 즉시 break, `claim_task_done=True`. cancel 경로에서는 CancelledError 가 `except TimeoutError` 에 걸리지 않고 전파돼 태스크가 cancelled 로 끝난다(누수 없음, Py3.14). |
| `run()` 조기리턴 / 배치 경계 플래그 | `_drain_backlog`·`_reclaim_orphans`·`_consume_live` 외부/내부 루프 모두 경계에서 확인한다. 진행 중 배치는 flush→XACK 후 종료한다. |
| 빈 PEL 가드 | `if not ids: break` + 회귀 테스트 `test_drain_backlog_exits_on_empty_pel_page` 가 존재하고 통과한다. |
| r2 S-2 미실행 예약 환불 | 실 Lua(A): reserve 미실행 + settle(0) → `used 50 / resvsum 0` (인덱스 필드 부재 → reserved=0 → delta 0). |
| r2 S-3 (단독 scope) / 마커 축출·인덱스 생존 | 실 Lua B: `53/0`, C: `53/0`. 동시 예약이 있는 경우는 위 S-1 참조. |
| 툼스톤 / 정상 경로 | H: 중복 settle 은 `{settled:false}` no-op, 툼스톤 TTL 14400. I: 정상 settle `53/0`, `recovered=false`. |
| r2 S-4 쿨다운 TOCTOU | 실 Redis(bytes): 서로 다른 event_id 4건을 동시에 claim 하면 `['str', False, False, False]`. 승자 event_id 의 fan-out 복사본은 `None`(진행), 다른 id 는 `False`(억제). 타 event_id 의 CAS 해제는 무시되고(holder 유지), 자기 해제는 DEL 된 뒤 재claim 이 가능하다. `None/False` 해제는 no-op. `handle()` 의 `cooldown_key is False` 분기, `NotificationInfraError` 에서만 해제하는 것도 맞다. 같은 event_id 의 버퍼 재처리는 holder==event_id 로 통과하므로 cancel/버그 경로에서 키를 남겨도 재처리를 막지 않는다. |
| N-1~N-8(r2) | PUBLISH WARN 격하, drain `BaseException` 재큐(재전파), 툼스톤 EX 14400, fallback_loop redis-None 선검사, `estimated_lost`, notification `stop_all(18)` 모두 diff 로 확인했다. 결함은 없다. |

## 수렴 판단

M 1건(신규): r1 에서 들어와 r2 까지 놓친 SQL 주석 바인드 결함이다. 일일 집계 전체가 정지한다. 수정은 한 줄이다.
S 1건(잔여): S-3 이 동시 예약 scope 에서 미해결이다. reconcile 의 인덱스 HDEL 누락 때문이다.
r2 의 M-1/S-1/S-2/S-4 는 실행 경로에서 해소를 확인했다.

VERDICT: NOT CONVERGED
