# R23 라운드 4: 라운드 3 수정분 검증 결과

범위: 라운드 3 지적(M-1, S-1, N-1~N-8)의 수정분 전체(`git diff HEAD`, 28 files).

## 검증 방법 (실행 증거)

- 실 Redis 7.4(docker)에서 `budget_reserve/settle/reconcile/rebuild.lua` 원본을 수정 없이 eval 했다. 시나리오 13개 중 12개가 기대값과 일치했다. 불일치 1건은 아래 N-1 이다.
- 실 PG16(pgvector)에 `db/init/*.sql` 을 적용하고 alembic 을 head(0048)까지 올렸다. 그 위에서 실 `run_daily_aggregation`, `backfill_if_empty`, `pg_terminate_backend` 를 실행했다.
- 실 `HealthChecker._reconcile_budget_counters` 를 실 PG(`budget.budget_usages`)와 실 Redis 위에서 실행했다.
- 실 `StreamConsumer.run()` 의 크래시 경로와 stop 경로를 실 Redis 에서 실행했다.
- 실 `BudgetHandler._claim_cooldown/_release_cooldown` 을 실 Redis(bytes 응답)에서 실행했다.
- 스위트 결과:
  - cost-recorder: 55 pass / 12 skip. `PROOF_DSN`/`REDIS_PROOF_URL` 을 실 PG/Redis 로 지정하면 **67 pass**.
  - notification-worker: 83 pass.
  - gateway-proxy: 1446 pass.

---

## 라운드 3 지적 검증

| 항목 | 결과 | 근거 |
|---|---|---|
| M-1 SQL 주석 바인드 | **해소** | 실 PG 에서 `run_daily_aggregation` 을 실행하니 3일치 행이 생성됐다. 각 날짜는 SUCCESS 2건과 0토큰 ERROR 1건으로 넣었고, 결과는 `request_count=2`, `cost=0.03` 이다. 실행 후 advisory lock 잔존은 0 이다. AST 스캔으로 4개 서비스와 admin-api 의 SQL 리터럴 79개를 전수 확인했다. 주석에만 있는 바인드 파라미터는 **0건**이라, 같은 패턴이 다른 SQL 에는 없다. 정적 회귀 테스트 `test_aggregation_sql_has_only_declared_bind_params` 는 통과한다. |
| S-1 reconcile dead 스윕 (E') | **해소(퀴에센스 틱 기준)** | 시나리오: team, used 50, A 5 / B2 7 예약, A 마커 삭제, reconcile. 직후 상태는 `57 / 7 / idx [B2]` 이다. 이어서 settle A(3)(`recovered=true`), settle B2(7) 을 하면 **`60.0 / 0.0 / []`** 이다. 단 비퀴에센스 틱에서는 아래 N-1 이 남는다. |
| client scope 공유 인덱스 | **해소** | 시나리오: claude-code r1/r2, codex r9/r8 예약 후 r1·r9 마커를 삭제했다. `claude-code` 행 하나만 reconcile 해도 인덱스가 `['claude-code:r2','codex:r8']` 로 남는다. 즉 두 앱의 dead 필드가 모두 청소되고, live 마커(`exists=1,1`)는 건드리지 않는다. 접두사 `budget:pending:client:{u1}:` 와 필드 `<client>:<req_id>` 를 이으면 실제 마커 키와 일치한다(budget_service.py:900-904). 이후 settle 결과는 claude-code `60/0`, codex `23/0` 이다. |
| N-1 인덱스 TTL ×2 | **해소** | `ttl=1` 로 예약하면 마커 TTL 1, 인덱스 TTL 2 다. 1.3s 뒤 늦은 settle(3) 결과는 **`53/0`, `recovered=true`** 다(r3 에서는 58/5). 고아 필드 5개(마커 만료 후 settle 없음)는 이후 reserve 가 인덱스 TTL 을 200 으로 갱신해 살아남는다. reconcile 1회로 `52/2/['live']` 가 되어 정확히 청소된다. |
| N-2 settle 주석 | 해소 | 주석에 "usage 는 플로어라 고아 예약치가 남는다(보수 방향)" 가 명시됐다. |
| N-3 resvsum 플로어 | **해소, 정상 경로 미발동** | 소수 예약 5건(0.1/0.2/0.3/0.07/0.013)을 순차 settle 했다. resvsum 궤적은 `0.583→0.383→0.083→0.013→0.0` 이고 최소값은 ≥0 이다. 단 이 플로어가 아래 S-1 의 음수 신호를 가린다. |
| N-4 툼스톤 TTL | **해소** | ARGV[5]=777 이면 TTL 777 이다. `''`, 4-ARGV 호출, 3-ARGV(구버전) 호출은 모두 **14400** 이다. `tonumber(nil)` 에러도 없다. 호출부 4곳(settle_budget, cost_recorder user/team/client)에 `reserved`/`marker_ttl` 이 배선된 것을 diff 로 확인했다. |
| N-5 크래시 경로 즉시 cancel | **해소** | 크래시 경로: `_consume_live` 가 0.2s 에 예외를 내자 **`run()` 이 0.20s 에 반환**했고 claim_task 는 `cancelled=True` 다(r3 에서는 15.11s). stop 경로: 0.21s 에 자연 종료했고 `cancelled=False` 다. |
| N-6 unlock InterfaceError | **해소** | 집계 도중 락 백엔드를 `pg_terminate_backend` 로 끊었다. 이벤트는 `ran, ran, unlock_failed`(WARN)이고 `failed` 는 없다. 잔존 락은 0 이고, 다음 실행은 정상(`ran`)이다. |
| N-7 BACKFILL FILTER | **해소** | 실 PG 에서 `backfill_if_empty` 실행 결과 3일 모두 `request_count=2` 다. ERROR 행이 제외되어 `_AGG_SQL` 과 정의가 같다. |
| N-8 쿨다운 단일 Lua | **해소** | e1, e1(복사본), e2 를 동시에 claim 하면 `[key, None, False]` 다(1=점유 / 2=fan-out 통과 / 0=억제 매핑). holder 는 `b'e1'` 이다. 타 event_id(e2) 의 해제는 무시된다. 창이 만료된 뒤 e2 claim 은 점유에 성공한다. 자기 해제는 DEL 된다. SET NX 와 GET 이 한 eval 안에 있어 사이에 만료가 끼어들 수 없다. |

---

## M급

### M-1 (신규, r2 diff 회귀) `:done` 툼스톤이 reconcile/웜업 SCAN 에 "라이브 마커"로 합산됨 → usage·resvsum 이 최근 4h 실비 총합만큼 부풀어 오른다
- 원인: settle 이 `marker_key .. ':done'` 을 만든다(budget_settle.lua:141). 이 키는 `budget:pending:` 네임스페이스 안에 있고 값이 **실비**다. 이 네임스페이스를 SCAN 하는 세 곳이 모두 툼스톤을 걸러내지 않는다.
  1. `health_checker.py:224-246`. `match="budget:pending:*"` 로 스캔하는데, `…:{tid}:<req>:done` 은 `parts=[team,{tid},req,done]` 이라 `len>=3` 을 통과해 마커 목록에 들어간다(client 도 `len>=4` 로 통과). 이 목록이 reconcile Lua 의 `KEYS[4..]` 가 되고, Lua 는 GET 값을 live 로 합산한다.
  2. `budget_service.py:297` `_pending_reservation_sum`. 웜업 창의 rebuild 폴백에서 `marker_prefix + field_prefix + "*"` 패턴이 툼스톤을 포함한다.
  3. `gateway-cli/scripts/restore_redis_budget.py:101`. 운영자 복원 도구다. 같은 패턴이라 복원값이 부풀어 오른다.
- 재현은 실 `HealthChecker` + 실 PG + 실 Redis 로 했다.
  1. team t9 에서 10건을 예약 5 / 정산 4 했다. 결과 usage 90, DB 90, 툼스톤 10개(각 4).
  2. 라이브 예약 5를 추가했다. usage 95, resvsum 5.
  3. `_reconcile_budget_counters()` 를 실행한 직후 상태는 **`usage 135, resvsum 45`** 다(기대 95/5).
  4. 라이브 예약을 settle 하면 `usage 135 / resvsum 40` 이 된다. 표시 실지출(usage−resvsum)은 95 로 우연히 맞는다. 그러나 **집행 카운터는 +40** 이다.
  5. 웜업 SCAN 합: `_pending_reservation_sum("budget:pending:team:{t9}:*")` 의 결과는 **45** 다(라이브 0 + 툼스톤 40 + …).
- 영향:
  - REDIS_DEGRADED 복구 창마다 team/user/client 카운터가 "직전 marker_ttl(4h) 동안 정산된 실비 총합"만큼 과대 계상된다.
  - team scope 는 팀 전체의 4h 지출이 더해진다. 그래서 팀 단위 **오탐 HARD_BLOCK** 과 거짓 80/90/100% 임계 알림이 생긴다.
  - 대시보드(usage−resvsum)는 정상으로 보이고 집행만 막히므로 진단이 어렵다.
  - 자가 치유가 사실상 없다. 다음 틱은 `orphan = 45−45 = 0` 이라 교정하지 않는다. 툼스톤이 만료된 뒤에 다음 열화 창이 와야 orphan 되돌림으로 회수되고, rebuild 는 센티넬이 있을 때만 돈다. 결과적으로 그 달 내내 남을 수 있다.
- 기존 테스트에 안 잡힌 이유: `test_health_checker.py` 는 `scan_iter` 를 목으로 대체해 마커 키만 돌려준다.
- 최소 수정(택1):
  - (a) 세 SCAN 지점 모두에서 `:done` 접미사 키를 제외한다. 회귀 테스트로는 툼스톤이 있는 상태의 reconcile 을 실 Lua/fakeredis 로 추가한다.
  - (b) **권장**: 툼스톤을 `budget:pending:` 밖의 같은 hash tag 키로 옮긴다. 예: `budget:settled:{scope}:{<id>}:<req_id>`. settle.lua 의 두 곳(GET, SET)만 바꾸면 되고, "`budget:pending:*` = 예약 마커"라는 계약을 지킨다.
  - (c) 아래 S-1 수정(reconcile 의 live 를 인덱스로 계산)을 하면 1번 지점은 구조적으로 사라진다. 2·3번은 (a) 나 (b) 가 여전히 필요하다.

---

## S급

### S-1 (기존 HEAD 결함, 이번 라운드 dead 스윕의 전제를 깸) SCAN→스냅샷 사이의 신규 예약이 퀴에센스 게이트를 통과해 **라이브 예약이 orphan 으로 차감**됨 → 과소 계상(차단 우회 방향)
- 위치: `health_checker.py`. 전역 SCAN(:224)이 **먼저** 돌고, 행마다 `GET resvsum`(:281) 스냅샷과 eval 이 그 뒤에 온다. 퀴에센스 게이트는 "스냅샷 == eval 시점 resvsum" 만 확인한다. 그래서 SCAN 이후·스냅샷 이전에 들어온 예약은 resvsum 에는 잡히지만 KEYS(live)에는 빠진다.
- 재현은 실 `HealthChecker` 로 했고, `scan_iter` 소진 직후 실 reserve 를 끼워 넣었다.
  1. team t7 에서 used 50, A 5 를 예약했다.
  2. SCAN 직후 B 7 을 예약했다.
  3. reconcile 결과는 **`55 / 5 / [A,B]`** 다(기대 62/12).
  4. A·B 를 정상 settle 하면 **`usage 55 / resvsum 0`** 이다. 기대값은 62/0 이라 **7 만큼 과소 계상**된다.
- 영향:
  - SCAN 은 전역 1회이고 행 루프는 수백~수천 행이다. 그래서 뒤쪽 행일수록 이 창이 길어진다(초 단위). 부하 중인 team scope 에서는 거의 매 틱 성립한다.
  - 열화 복구 창의 **틱마다 누적**된다.
  - 예약치는 최악비용 추정치라 크다.
  - 이번 라운드의 N-3 플로어가 settle 의 resvsum 음수(−7)를 0 으로 덮는다. 그래서 유일한 관측 신호도 사라졌다.
- 수정(택1):
  - (a) **권장**: reconcile Lua 가 live 를 이미 받는 `KEYS[3]` 인덱스와 `ARGV[3]` 접두사로 HKEYS+GET 해서 직접 계산한다. client scope 는 rebuild 와 같이 필드 접두사 `<client>:` 로 필터한다. 그러면 live 와 resvsum 을 한 eval 안에서 원자적으로 읽으므로 창 자체가 없어지고, 퀴에센스 게이트·SCAN·M-1(1번 지점)이 함께 사라진다. 웜업 창에만 SCAN 합을 넘긴다(rebuild 의 ARGV[4] 규약과 같다).
  - (b) 행별 resvsum 스냅샷을 SCAN **이전**에 수집한다(파이프라인 GET). 이후의 모든 예약/정산이 게이트를 트리거한다.
- 이번 라운드 E' 검증(60/0)은 "스냅샷이 live 집합과 정합" 을 전제로 한다. 이 전제가 위 창에서 깨진다.

---

## N급

- **N-1 비퀴에센스(raced) 틱에서의 무조건 HDEL**
  - 재현(실 Lua): A 마커 소실 상태에서 스냅샷이 어긋나는 reconcile 을 실행하면 orphan 은 생략되지만 A 필드는 HDEL 된다. 이어 settle A(3), B2(7) 을 하면 **`65 / 5`** 다(이상 60/0). 다음 퀴에센스 틱이 오면 `60/0` 으로 치유된다.
  - 게이트를 걸었다면 A 필드가 남아 settle 이 HEXISTS→`min(5, resvsum)` 로 **즉시 정확**했을 것이다. 무조건 HDEL 은 그 기회를 버리고 다음 틱에 의존한다.
  - 창이 끝날 때까지 퀴에센스 틱이 없으면 예약치만큼 과대 계상이 남는다(보수 방향, bounded).
  - 주석의 "마커 부재는 확정 증거라 게이트 무관" 은 필드의 의미를 "마커 생존"으로 읽은 것이다. settle 계약상 필드의 의미는 "**카운터에 예약치가 남아 있음**" 이다.
  - 수정: HDEL 을 `resvsum == snap` 분기 안으로 옮긴다. S-1(a) 를 적용하면 이 항목도 함께 사라진다.
- **N-2 고아 필드 누적**
  - 파드 크래시로 settle 이 안 온 예약의 필드가 대상이다. 인덱스 TTL 은 reserve 마다 `marker_ttl×2` 로 갱신되므로, 활성 scope 에서는 이 필드가 reconcile(열화 창)이나 rebuild(센티넬)가 돌 때까지 남는다.
  - ×2 이전에도 갱신 구조는 같았다. 그래서 이번 변경으로 누적 속도가 실질적으로 늘지 않았다. 크래시 1회당 scope 당 필드 1개 수준이다.
  - 정확성에는 무해하다. 그 요청의 settle 은 오지 않으므로 HEXISTS 를 묻는 주체가 없다. 비용은 HKEYS O(n) 뿐이다.
  - 상시 청소 경로가 없다는 점은 문서화 대상이다.
- **N-3 M-1 의 실 PG 회귀 테스트 미추가**
  - r3 가 권고한 "PROOF_DSN 실 PG 로 `run_daily_aggregation` 1회 → 행 생성" 테스트가 없다. 정적 `_bindparams` 테스트만 있다.
  - 정적 테스트는 주석 바인드 계열은 잡는다. 다만 FILTER 의미론이나 실행 경로 회귀는 목 세션 기반이라 계속 사각지대다.
  - 이번 라운드에 수동으로 실행해 통과를 확인했다(위 표).
- **N-4 크래시 경로 claim_task 는 cancel 만 하고 await 하지 않는다**
  - supervisor 가 즉시 `run()` 을 재시작하면 옛 reclaim 태스크가 다음 await 까지 잠깐 새 태스크와 공존한다.
  - CancelledError 가 다음 await 에서 전파되므로 실해는 없다. 기존 동작과도 같다. 참고 항목이다.

---

## 새 결함 점검 요약 (요청 2항)

| 점검 | 결론 |
|---|---|
| reconcile 상시 HDEL ↔ racing settle | 두 eval 모두 원자적이라 순서는 두 가지뿐이다. settle 이 먼저면 필드가 이미 HDEL 돼 reconcile 이 볼 것이 없다(정합). reconcile 이 먼저면 퀴에센스 틱은 정확(E' 60/0)하고, raced 틱은 과대 계상이 생겼다가 다음 퀴에센스 틱에 치유된다(N-1). 라이브 마커의 필드는 지우지 않는다(reserve 가 SET 과 HSET 을 같은 eval 에서 하므로 "마커 없는 라이브 필드"는 존재하지 않는다). |
| 인덱스 TTL ×2 고아 필드 | 누적 속도는 사실상 불변이다. reconcile 스윕이 정확히 닫는 것을 확인했다(N-2). |
| 바인드 파라미터 동일 패턴 | SQL 리터럴 79개 중 0건(AST 스캔). f-string SQL 은 스캔 대상 밖이지만, 주석 안에 `:word` 가 있는 f-string SQL 은 grep 으로도 없다. |
| **툼스톤 × SCAN** | **M-1 (신규)** |
| **SCAN→스냅샷 창** | **S-1 (기존)**. N-3 플로어가 신호를 가린다. |

## 수렴 판단

r3 의 M-1 과 S-1(퀴에센스 틱 기준), N-1~N-8 은 실행 경로에서 모두 해소를 확인했다.
그러나 r2 에서 들어온 `:done` 툼스톤이 `budget:pending:*` SCAN 3곳에 라이브 마커로 합산되는 결함(M-1)이 남아 있다. 실 HealthChecker 에서 집행 카운터가 +40 으로 재현됐다.
reconcile 의 SCAN→스냅샷 창이 라이브 예약을 orphan 으로 차감하는 기존 결함(S-1)도 있다. 실 HealthChecker 에서 −7 로 재현됐고, 이번 라운드 dead 스윕의 정합 전제를 깬다.
두 건 모두 reconcile 의 live 를 인덱스 기반으로 계산하고 툼스톤을 네임스페이스 밖으로 옮기면 함께 닫힌다.

VERDICT: NOT CONVERGED
