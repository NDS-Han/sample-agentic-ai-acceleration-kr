# R23 라운드 6: 라운드 5 수정분 검증 결과

범위: 라운드 5 지적 M-1(client scope 크로스앱 합산)과 N-1(`:done` 이행기 잔재)의 수정분(`git diff HEAD`). 중점은 `budget_reconcile.lua`, `health_checker.py`, `budget_service.py::_pending_reservation_sum`, `gateway-cli/scripts/restore_redis_budget.py` 이다.

## 검증 방법 (실행 증거)

- 실 Redis 7.4(docker)에서 `src/app/redis_scripts/*.lua` 원본을 `LuaScriptLoader` 로 그대로 로드해 eval 했다.
- reconcile 은 **실 `HealthChecker._reconcile_budget_counters`** 로 돌렸다. SCAN, GET, eval 은 모두 실 Redis 호출이다. DB 는 `budget_usages` 행만 돌려주는 가짜 엔진으로 대체했다. r5 는 실 PG 를 썼지만 이번 경로는 DB 를 행 스냅샷으로만 읽으므로 결과는 같다.
- reserve 와 settle 은 `budget_service.py:885-904` 의 키 레이아웃을 그대로 따랐다. settle 의 키 목록은 실 `settle_eval_args` 로 만들었다.
- `restore_redis_budget.restore()` 는 asyncpg 만 가짜로 바꾸고 실 Redis 위에서 실행했다.
- 하네스 결과는 **29/30 PASS** 다. 실패 1건은 아래 N-1(의도적으로 넣은 탐색 케이스)이다.
- gateway-proxy 스위트: **1449 passed / 60 skipped**.
- 임시 컨테이너와 하네스는 검증 후 삭제했다.

---

## 1. client scope reconcile 재검증

| 시나리오 | 결과 | 근거 |
|---|---|---|
| r5 재현(DB cc 10 / cx 20, 예약 cc 5 / cx 9) | **해소** | reconcile 후 cc `15/5`, cx `29/9` 다(r5 에서는 24/14, 34/14). settle 후 cc `15/0`, cx `29/0` 이다. 공유 인덱스에는 `claude-code:r1`, `codex:r9` 가 그대로 남는다. |
| codex DB 행 없음(5b) | **해소** | cc `15/5` 이고 cx 는 손대지 않은 `9/9` 다. 라이브 codex 필드는 **HDEL 되지 않고 생존**한다. settle 후 cc `15/0`, cx `9/0` 이다. |
| covered 의 접두사 무관 마킹 | **정확** | cc 행 Lua 에 codex 마커 키를 KEYS 로 일부러 넘겨도 live 는 `5` 다. 접두사 밖이고 covered 처리돼 KEYS 경로로도 들어오지 않는다. codex 행에서 인덱스와 KEYS 양쪽에 있는 마커는 1회만 합산된다(live `9`). 실제로는 health_checker 가 KEYS 를 `(USER, sid, c)` 로 묶어 넘기므로 타 앱 마커가 KEYS 에 들어올 일 자체가 없다. covered 는 이중 방어다. |
| user/team(field_prefix="") | **필터 무력화 확인** | user 3+4 → `57/7`, team 6+7 → `93/13` 으로 전 필드가 합산된다. user 마커 b 를 만료시키면 orphan 4 가 회수되고(`53/3`) dead 필드 `b` 는 HDEL 된다. |
| 접두사 충돌 `codex` vs `codex-x` | **격리** | `(4/3)`, `(6/4)` 다. 콜론 포함 접두사(`"codex:"`)라 겹치지 않는다. |
| req_id 에 `:` 포함 + 비인덱스(KEYS 경로) | **정확** | `3/2` 다. KEYS 쪽 `string.sub(KEYS[i], #ARGV[3]+1)` 가 `claude-code:x:y` 로 재구성돼 인덱스 필드 규약과 일치한다. |
| dead 스윕 cross-app | **설계대로 동작, 단 부작용 있음** | cc 행이 codex 의 만료 필드를 HDEL 한다. codex 행이 같은 패스에 있으면(`29/0`) 정합이고, 틱이 나뉘어도(cc 틱 → 늦은 settle → codex 틱, `29/0`) 정합이다. **codex 행이 reconcile 되지 않으면 어긋난다** → 아래 N-1. |

## 2. `:done` 필터 (세 소비자)

| 소비자 | 결과 | 근거 |
|---|---|---|
| `health_checker` 스캔 파서 | **정확** | team t9 에 구 툼스톤 3개(각 4), 라이브 5, DB 90 을 두고 reconcile 하면 **`95/5`** 다(r5 에서는 107/17). |
| `BudgetService._pending_reservation_sum` | **정확** | `budget:pending:team:{t9}:*` 의 합이 **5** 다(r5 에서는 17). bytes/str 양쪽을 디코드한다. |
| `restore_redis_budget.py` | **정확** | 실행 결과 카운터 `95`, resvsum `5` 다(`pending=$5.0000`). |

- `endswith(":done")` 은 req_id 가 정확히 `done` 인 경우에만 오탐한다. req_id 는 UUID 계열이라 실질 위험은 없다.
- 이행기가 끝나면 무해한 분기다.

---

## 3. 새 결함 점검

| 점검 | 결론 |
|---|---|
| union 잔여 경합 | 문제 없음. SCAN 이후 settle 된 마커는 eval 안에서 GET nil 이 되고 인덱스 필드도 함께 지워져 제외된다. SCAN 이후 들어온 신규 예약은 인덱스로 잡힌다. 인덱스와 KEYS 양쪽에 있으면 covered 로 1회만 센다. `cursum` 스냅샷과 eval 사이에 예약/정산이 끼면 퀴에센스 게이트가 orphan 과 HDEL 을 건너뛰고, 플로어(`db+live`)만 적용한다. 플로어는 안전 방향이다. DB 스냅샷이 오래돼도 플로어가 낮아지는 쪽이라 역시 안전하다. |
| 필드 접두사 ↔ 마커 필드 불일치 경로 | 없음. reserve 의 `idx_field = f"{client}:{request_id}"`, health_checker 의 `field_prefix = f"{c}:"`, 파서의 `parts[2]`(client), 마커 접두사 `budget:pending:client:{sid}:` 가 서로 일관된다. rebuild 웜업 경로의 SCAN 패턴 `marker_prefix + field_prefix + "*"` 도 같은 규약을 따른다. |
| 비숫자 마커 / covered 비용 | r5 N-2·N-3 수용 그대로다. |

### N-1 (신규, 이번 diff 의 설계 선택) cross-app dead 스윕이 **reconcile 되지 않는 타 앱**의 늦은 settle 복구를 무력화한다

- 메커니즘:
  - cc 행의 eval 은 **cc 의 resvsum 퀴에센스만 보고** 공유 인덱스에서 codex 의 dead 필드를 HDEL 한다.
  - codex 의 resvsum 과 usage 에서 orphan 을 되돌리는 일은 codex 행 eval 에서만 일어난다.
  - codex 행이 `budget_usages` 에 없거나 창이 끝날 때까지 매 틱 raced 이면, 되돌림 없이 필드만 사라진다.
  - 그 뒤 늦게 도착한 codex settle 은 `HEXISTS=0` 이라 `reserved=0` 으로 판단하고 실비만 더한다.
- 재현(실 Lua + 실 HealthChecker):
  - 설정: codex 예약 9, 마커 만료, codex DB 행 없음. cc 행만 reconcile 한 뒤 늦은 settle(실비 9)을 보낸다.
  - 결과는 **cx `18/9`** 다(기대 `9/0`).
  - 대조군(스윕 없음)은 같은 settle 에서 `9/0` 이다. 즉 HEAD 대비 이번 diff 가 만든 차이다.
- 등급을 N 으로 둔 이유:
  - 방향이 과대 계상(조기 차단)이다. 차단 우회가 아니다.
  - 크기는 예약 1건의 추정치로 제한된다.
  - 전제 조건이 모두 겹쳐야 한다:
    - 마커 TTL(4h)을 넘긴 요청이 있다.
    - 그 사이에 열화 복구 창이 열린다.
    - 그 앱이 해당 월에 flush 된 실비가 없다(첫 사용). 또는 창 전체가 raced 다.
  - r3 N-2(rebuild/reconcile 후 늦은 settle 의 과대 잔량)와 같은 계열이다.
  - 다만 자가 치유되지 않는다. 다음 달 키 회전 또는 해당 행이 생긴 뒤의 다음 열화 창까지 남는다.
- "rebuild 와 동일" 이라는 근거에 대해: rebuild 의 cross-app 스윕에도 같은 잠재 결함이 있다(기존 코드). 다만 rebuild 는 센티넬이 선 카운터 1개에서만 돌기 때문에 노출 빈도가 훨씬 낮다. reconcile 은 열화 창마다 모든 client 행에서 돌기 때문에 노출 면이 더 넓다.
- 권고(선택): dead 분류에도 접두사를 적용한다(r5 권고 원안). 한 줄 수정이다.
  ```lua
  else
      if field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
          dead[#dead + 1] = f
      end
  end
  ```
  - 그러면 각 앱의 필드는 자기 resvsum 게이트와 같은 eval 안에서만 지워진다(r4 N-1 의미론).
  - DB 행이 없는 앱의 고아 필드는 인덱스 TTL(마커의 2배)로 소멸한다.
  - rebuild 에도 같은 수정을 하면 대칭이 된다.

### N-2 회귀 테스트 미추가

- `test_health_checker.py` 는 user 행에서 ARGV[3](마커 접두사)까지만 단언한다. **client 행의 `field_prefix`(ARGV[4]) 를 단언하는 테스트가 없다.**
- r5 가 권고한 시나리오 5(실 Lua 또는 fakeredis-lua)도 추가되지 않았다.
- 이번 하네스 1a·1b 와 같은 케이스를 고정해 두지 않으면, 이후 리팩터링에서 M-1 이 조용히 재발할 수 있다.
- 정확성 영향은 없어 N 으로 분류한다. 권장 항목이다.

---

## 4. 미해결 M/S 잔여

- **없음.**
  - r5 M-1 은 실행 경로(1a·1b)에서 해소를 확인했다.
  - r5 N-1 은 세 소비자 모두에서 해소를 확인했다.
  - r4 이전의 M/S 지적(툼스톤 네임스페이스, SCAN-gap, 웜업 비인덱스 마커)은 회귀가 없다. 2·1c·3 의 시나리오가 그 경로를 다시 지난다.
- 남은 항목은 N-1(cross-app 스윕의 보수 방향 잔량, 선택 수정)과 N-2(테스트)다. 둘 다 수렴을 막지 않는다.

## 수렴 판단

- client scope reconcile 은 자기 앱 필드만 live 에 합산한다. 타 앱의 라이브 필드는 보존한다. covered 의 접두사 무관 마킹으로 이중계산이 없다. user/team 에서는 필터가 무력화된다. 모두 실 Lua 로 확인했다.
- `:done` 필터는 세 소비자 모두 정확하다.
- 새로 확인한 결함은 보수 방향의 좁은 전제 N급 1건과 테스트 공백 1건이다.

VERDICT: CONVERGED
