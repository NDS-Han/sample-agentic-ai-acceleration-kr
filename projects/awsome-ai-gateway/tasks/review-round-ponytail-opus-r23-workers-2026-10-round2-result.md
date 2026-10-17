# R23 라운드 2 — 라운드 1 수정분 검증 결과

범위: cost-recorder-worker, notification-worker, gateway-proxy 정산 경로(cost_recorder.finalize / budget_service / fallback_loop / budget_settle.lua).
기준: 현재 working tree (`/tmp/r23-round1.diff` 대비 `_drain_backlog` 빈 PEL 가드가 리뷰 중 추가 반영됨 — 아래 "검증 OK" 참조).

## 검증 방법 (실행 증거)

- 실 Redis 7.4(docker) + 실 Lua 스크립트(budget_reserve/settle/reconcile) 직접 실행
- 실 PostgreSQL 16(docker) + SQLAlchemy async 풀로 advisory lock 커넥션 친화성 재현
- 실 `StreamConsumer`/`TaskSupervisor` 코드로 main.py 종료 시퀀스 재현
- 실 `BaseHandler` + dict 기반 Redis 목으로 동시 버스트 재현
- 기존 스위트: cost-recorder 53 pass / notification 81 pass / gateway-proxy 1443 pass — **아래 결함은 전부 기존 테스트에 안 잡힘**

---

## M급

### M-1 (신규, N급 수정의 회귀) daily_aggregator advisory lock 이 풀 커넥션에 누수 → 집계 영구 스킵
- `cost-recorder-worker/src/worker/daily_aggregator.py:268-301`
- `pg_try_advisory_lock` 은 **세션(커넥션) 레벨** 락인데, `_aggregate_window`(L145)가 날짜마다 `session.commit()` → AsyncSession 이 커넥션을 풀에 반납 → 다음 execute 는 다른 커넥션을 받을 수 있다. `finally` 의 `pg_advisory_unlock` 이 다른 커넥션에서 실행돼 `false` 를 반환하고, 락은 풀에 돌아간 원래 커넥션에 **커넥션 수명 동안** 남는다(db.py 에 pool_recycle 없음).
- 재현(PG16, 배치 플러셔처럼 동시 풀 사용 3개): `pid_at_lock=63 pid_at_unlock=66 unlock_returned=False advisory_locks_still_held=1`, 다른 replica 의 다음 실행 `pg_try_advisory_lock -> False`.
- 결과: 이후 크론은 (락 쥔 커넥션을 우연히 받은 경우 외엔) 전 replica 에서 `daily_aggregator.skipped_lock_held`(INFO)로 조용히 스킵 → `backfill_missing_days` 도 같은 블록이라 함께 정지 → `usage.daily_aggregates` 공백 → `/v1/usage/me` 과거일 사용량(routers/usage.py:64) 누락. 사용자 가시.
- 실행 중 예외로 트랜잭션이 aborted 면 finally 의 unlock 자체가 실패해 같은 누수.
- 최소 수정: 락 전용 커넥션을 따로 잡는다.
  ```python
  engine = session_factory.kw["bind"]
  async with engine.connect() as lock_conn:
      lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
      if not (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": KEY})).scalar():
          logger.warning(...); return
      try:
          async with session_factory() as session: ...  # 기존 집계 그대로
      finally:
          await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": KEY})
  ```
  회귀 테스트: 실 PG 로 "commit 후 다른 커넥션에서 unlock" 시나리오.

---

## S급

### S-1 (미해결) S1 협조 종료가 실제로는 동작하지 않음 — stop_all 이 즉시 cancel
- `cost-recorder-worker/src/worker/main.py:154-155` + `worker.py:63-67`
- `consumer.request_stop()` 직후 `supervisor.stop_all()` 을 호출하는데, `stop_all` 은 **먼저 `task.cancel()`** 하고 그 다음에 grace 를 기다린다. 주석의 "루프가 안 멈추면 grace 만료 후 cancel" 과 실행 순서가 반대다. 커밋~XACK 사이의 배치는 여전히 잘린다.
- 재현(실 StreamConsumer+TaskSupervisor, flush 0.3s 진행 중 main.py 12단계 그대로 실행): `flush committed: False | xack calls: 0`. 회귀 테스트 `test_request_stop_exits_consume_loop_at_batch_boundary` 는 `_consume_live` 만 단독으로 돌려 이 순서를 검증하지 못한다.
- 같은 결함 계열: `run()` finally 의 `self._claim_task.cancel()`(stream_consumer.py:143) 이 진행 중인 reclaim 배치(flush→XACK)를 즉시 끊는다. stop 후 flush 예외 시 supervisor 가 재시작하면 `run()` 이 `_reclaim_orphans()` 를 무조건 다시 돈다.
- 최소 수정:
  1. main.py: `request_stop()` → `await asyncio.wait(supervisor 태스크들, timeout=grace)` → 남은 것만 `stop_all(grace=짧게)`. (또는 `stop_all(cooperative=True)` 로 wait-then-cancel 모드 추가)
  2. `run()` 진입 시 `_stop_requested` 면 즉시 return, `_reclaim_loop`/`_reclaim_orphans` 루프도 배치 경계에서 플래그 확인, finally 는 cancel 대신 `await asyncio.wait_for(claim_task, timeout)` 후 cancel.
  3. 테스트는 main 의 실제 시퀀스(supervisor 포함)로.

### S-2 (신규, S5 회귀) reserve eval 실패 시 "혹시 몰라 환불"이 받지도 않은 예약치를 차감
- `gateway-proxy/src/app/services/budget_service.py:925-936`
- 예외 분기가 방금 실패한 scope 를 `"reserved": est_str` 와 함께 `committed` 에 넣고 `settle(0)` 한다. 주석 전제는 "마커가 없으면 settle(0) 은 no-op" 인데, r1 의 복구 경로가 그 전제를 깼다. reserve 가 서버에서 실행되지 않았으면 마커·툼스톤이 없고 ARGV[4] 가 있으니 `recovered` → `usage -= est`.
- 재현(실 Lua): used=50, reserve 미실행 + refund(0) → `settled=True recovered=True used=45 resvsum=-5`. 기대값은 used=50.
- 트리거: reserve 는 실패했는데(페일오버 순간, 죽은 풀 커넥션 재시도 소진 등) 바로 다음 settle 은 성공하는 경우. 건마다 HARD_BLOCK 카운터가 최악비용 추정치만큼 과소 계상된다.
- 최소 수정: 예외 분기는 `"reserved": ""` 로 넣는다(마커가 실존하면 마커 값이 쓰이고, 없으면 no-op 이 정답). 아래 S-3 의 Lua 상한을 넣으면 이 경우도 함께 막힌다.

### S-3 (신규, S5) 복구 경로가 "예약치가 아직 카운터에 남아 있다"고 가정 — reconcile/rebuild 와 충돌해 이중 차감
- `gateway-proxy/src/app/redis_scripts/budget_settle.lua:47-60`
- `budget_reconcile.lua`(REDIS_DEGRADED 복구 창)와 `budget_rebuild.lua` 는 **만료/고아 마커의 예약분을 usage/resvsum 에서 걷어내도록** 설계돼 있다. 걷어낸 뒤 늦은 settle 이 오면 recovered 가 예약치를 한 번 더 뺀다.
- 재현(실 Lua): reserve 5(used 55) → 마커 소실 → reconcile(orphan=5, used 50, resvsum 0) → settle(actual 3) recovered → `used=48 resvsum=-5`. 기대값 53/0. 실비 3 이 반영되지 않고 오히려 2 가 줄었다.
- 최소 수정(Lua 한 줄 계열): recovered 분기에서 `reserved = math.min(caller_reserved, math.max(tonumber(GET resvsum) or 0, 0))`. 결과: reconcile 이 이미 걷어냈으면 0 → plain deduct(+actual), 정상 만료면 그대로 복구. S-2 시나리오도 resvsum=0 → delta 0 → 무해. 동시 타 예약이 resvsum 을 채우는 경우엔 근사지만, 현재처럼 무조건 빼는 것보다 상한이 있다. 더 정확하게 하려면 `HEXISTS pendingidx field` 를 복구 조건으로 쓴다(rebuild 가 dead field 를 HDEL 하므로 "아직 미정리" 신호가 된다).
- 회귀 테스트: 위 C 시나리오와 S-2 의 A 시나리오를 fakeredis/실 Lua 로 추가.

### S-4 (신규, S7 회귀) 쿨다운 "확인→처리→기록" TOCTOU 로 동시 버스트 억제가 깨짐
- `notification-worker/src/worker/handlers/base.py:138-139, 182, 194-233`
- 쿨다운이 겨냥한 대상은 **서로 다른 event_id** 로 거의 동시에 재발행되는 같은 정체성 이벤트다(pod × uvicorn 워커). 슬롯 유니크는 (event_id, type, recipient) 라서 이들을 못 막는다. 기록이 발송 완료(SES 왕복·재시도 수 초) 뒤라, 그 창 안에 도착한 이벤트는 전부 `exists=0` 을 보고 통과한다.
- 재현(실 BaseHandler, send 0.2s, 같은 정체성 4건 동시): `assert 4 == 1` 실패, 메일 4통. 이전 SETNX 는 1통. 기존 테스트는 순차 호출이라 통과한다.
- 최소 수정(S7 의 "죽은 claimer 가 fan-out 복사본을 막는" 문제를 다시 만들지 않는 방식): `SET key <event_id> NX EX cooldown` 으로 선점하고, 실패하면 `GET key == event.event_id` 일 때만 진행한다(같은 이벤트의 fan-out 복사본 — 슬롯 유니크/dead-claim takeover 가 중복을 처리). 다른 event_id 면 억제. 인프라 실패로 되돌릴 땐 compare-and-del(값이 자기 event_id 일 때만 DEL, Lua).

---

## N급

- N-1 `batch_flusher.py:718-728` `threshold_publish_lost`: prod 는 cluster 모드(elasticache-valkey, prod 는 항상 num_node_groups)이고, cluster 의 PUBLISH 반환값은 **발행 노드에 붙은 구독자 수**만 센다. notification-worker 는 단일 노드 연결(redis_client.py:50 주석)이라 발행 노드가 다르면 정상 전달인데도 ERROR + 메트릭이 오른다 → 오탐. cluster 모드에선 notification-worker heartbeat 키(`SET notif:alive EX 30`)로 판정하거나 WARN 으로 내린다.
- N-2 `notification_buffer.py:85-98` drain 중 `CancelledError`(BaseException)는 `except Exception` 에 안 걸려 이미 dequeue 된 batch[i:] 가 사라진다. `stop_all(15)` 가 health_check 태스크를 drain 도중 취소하면 그 뒤의 종료 drain(main.py:260)이 그 이벤트들을 못 본다. `except BaseException: 재큐; raise` 로 감싼다.
- N-3 같은 곳: 재큐가 큐 꼬리로 붙어 drain 중 리스너가 넣은 새 이벤트 뒤로 밀린다(80%→90% 알림 순서 역전 가능). 큐가 가득 차 있으면 재큐가 oldest(더 새 이벤트일 수 있음)를 드랍한다. 중복은 (event_id, recipient) 슬롯이 막으므로 안전. 순서 보장이 필요하면 별도 retry deque 를 앞쪽에서 소비한다.
- N-4 `budget_settle.lua:104` 툼스톤 TTL 300s < 마커 TTL 14400s. 300s 이후 중복 settle 이 오면 recovered 로 이중 적용된다(실 Lua: 53 → 51, resvsum -5). 지금은 pop-first 로 in-process 중복 생산자가 없어 잠재 리스크다. 불변식("같은 스펙은 한 번만 settle 된다")을 주석과 테스트로 고정하고, S-3 상한을 넣으면 피해도 묶인다.
- N-5 `fallback_loop.py:214-218` pop 이 `redis is None` 검사보다 앞에 있어, redis 가 None 이면 상태를 버린다(이전엔 finalize 가 처리하도록 남겼음). 실제로는 redis None 이면 예약 자체가 없어 무해하지만, 검사를 pop 앞으로 옮기면 의미가 명확해진다.
- N-6 `stream_consumer.py:281-297` `stream_gap_detected` 는 유실 건수가 아니라 감지 **샘플 수**를 센다(크래시 루프 중 재시작마다 +1). Redis 7 에선 `entries-added − entries-read − lag` 로 유실 건수를 추정할 수 있다(실 Redis 재현: 25−2−5=18 = 실제 유실 수). 게이지로 노출하면 과소청구 규모가 바로 보인다.
- N-7 `channel_listener._INFLIGHT_GRACE_SEC=15` 와 `stop_all(grace_period=15)` 가 같아 경계에서 경합한다. stop_all 을 18s 정도로 두고 drain(10) 을 합쳐도 30s 안에 들어온다.
- N-8 `daily_aggregator.skipped_lock_held` 가 INFO 다. M-1 처럼 락이 고착되면 신호가 없다. 연속 스킵 시 WARN 으로 올린다(M-1 수정과 같이).

---

## 검증 OK (수정이 결함을 실제로 해결)

| 항목 | 근거 |
|---|---|
| M1 갭 탐지 | 실 Redis 7.4, `decode_responses=False`(bytes): 2건 배달 후 MAXLEN 5 트림 → `cost_stream_delivery_gap` ERROR + `stream_gap_detected.add(1)`. `_trim_consumed` 는 exact `MINID=min(pending, last_delivered)` 라 정상 상태에선 first ≤ last_delivered 로 오탐 없음. 0-0 신생 그룹 제외 동작 확인. |
| `_drain_backlog` 빈 PEL | r1 diff 의 `break→continue` 는 빈 PEL 응답 `[[stream, []]]`(truthy, 실 Redis 확인)에서 무한 루프였다. 현 working tree 의 `if not ids: break` 가드로 해소됐다(실 Redis: 신규 consumer / 이력 있는 consumer 모두 xreadgroup 1회 후 반환). 이 가드에 대한 회귀 테스트는 없어 추가를 권장한다. |
| `_stop_requested` 루프 발화 | `_drain_backlog`·`_consume_live` 바깥/안쪽 루프 모두 배치 경계에서 확인한다. stop 후 남은 배치는 flush→XACK 뒤 종료하며, 루프 자체엔 XACK 누락 경로가 없다. 진입 순서 결함은 S-1 참조. |
| S2 drain 잔여 재큐 | batch[i:] 전부 재큐. 중복은 슬롯 유니크가 dedup(실패 이벤트는 전원 인프라 실패일 때만 raise). |
| S3 백오프 리셋 | 두 supervisor 모두 `started_at` 기준 300s 초과 시 1.0 리셋. 크래시 루프 누적은 유지. |
| S4 socket_timeout | `xread_block_ms/1000+5`(10s) > BLOCK 5s. keepalive/health_check 적용. |
| S5 툼스톤(정상 경로) | 실 Lua: settle 후 300s 안의 재정산은 `{settled:false}` no-op, used 53 유지. |
| S5 pop-first | release_reservations 는 첫 await 전에 동기 pop 한다. finalize 호출자 전부(routers 19곳)가 호출 시점에 `state.pop` → 단일 이벤트 루프에서 배타 소유. guard 는 drain_pending 동안 대기 후 release(0) 로, 기존 의도와 같다. 정산 유실 경로는 새로 생기지 않았다. |
| S6 | standalone 에선 정상. cluster 오탐은 N-1. |
| S7 rescue 속성 | 죽은 claimer 가 키를 남기지 않는 것은 맞다. 대신 버스트 억제가 깨졌다(S-4). |
| misfire_grace | 3600s, coalesce 기본, 작업 멱등 — OK(락 문제는 M-1). |
| N급 나머지 | email_mask 정규식·SES/SMTP 분류·mock 'mocked'·resend is_active·abandoned 계측·config_reload heartbeat(pong 이 activity 갱신)·DELCONSUMER(pending=0 & idle>1h, 자기 제외) — 결함 없음. |

## 생산자–소비자 계약

- gateway `app/schemas/cost_stream.py` ↔ worker `worker/schemas/cost_stream.py` `CostStreamEntry`: AST 비교 결과 필드 집합·타입·기본값이 완전히 일치한다(only-gateway ∅, only-worker ∅, diff ∅). schema_version 3 = `_SUPPORTED_SCHEMA_VERSION` 3.
- threshold 흐름: finalize 의 settle/deduct `threshold_triggered`(user 우선, 없으면 team + `threshold_scope="team"`) → XADD → worker `recorded`(실제 INSERT 된 행만) → `_publish_thresholds` 가 `payload{threshold_pct, scope, target_type, period=e.period}` 를 envelope 안에 담는다(event_id=request_id) → notification fan-out 은 슬롯 유니크로 dedup. 정상.
- settle 이 `{settled:false}`(툼스톤 no-op)면 threshold 는 None 이다. 이미 정산한 쪽이 발화했으므로 맞는 동작이다. recovered 는 `logical_old = used − reserved` 로 평가하므로 S-3 상황에선 교차 판정도 어긋난다(S-3 수정으로 같이 해소).

## 수렴 판단

M 1건(신규), S 4건(미해결 1 + 신규 3). 신규 결함 대부분이 r1 수정이 기대는 전제(예: "stop_all 이 기다려 준다", "마커가 없으면 no-op", "기록이 곧 억제", "세션=커넥션")가 실행 경로에서 성립하지 않아서 생겼다. 회귀 테스트는 그 전제를 목으로 고정해 통과하고 있다.

VERDICT: NOT CONVERGED
