# R23 라운드 5: 라운드 4 수정분 검증 결과

범위: 라운드 4 지적(M-1, S-1, N-1~N-3)의 수정분(`git diff HEAD`, 28 files). 중점은 `budget_settle.lua`, `budget_reconcile.lua`, `health_checker.py` 이다.

## 검증 방법 (실행 증거)

- 실 Redis 7.4(docker)에서 `src/app/redis_scripts/*.lua` 원본을 `LuaScriptLoader` 로 그대로 로드해 eval 했다.
- reconcile 은 **실 `HealthChecker._reconcile_budget_counters`** 를 실 PG16(`budget.budget_usages` 스크래치 DB)과 실 Redis 위에서 실행했다. SCAN, GET, eval 은 모두 실제 호출이다. DegradationManager 만 목이다.
- 예약과 정산은 `budget_service.py:885-904` 와 같은 키 레이아웃으로 reserve/settle Lua 를 직접 호출했다. settle 의 키 목록은 실 `settle_eval_args` 로 만들었다.
- gateway-proxy 스위트: **1448 passed / 60 skipped**.
- 임시 컨테이너, 스크래치 DB, 하네스는 검증 후 삭제했다.

---

## 라운드 4 지적 검증

| 항목 | 결과 | 근거 |
|---|---|---|
| `string.sub(marker_key, 16)` | **정확** | `len('budget:pending:') = 15` 이다. settle 후 생성된 키를 scope 별로 확인했다. user `budget:settled:user:{sid1}:rq1`, team `budget:settled:team:{sid1}:rq1`, client `budget:settled:client:{sid1}:codex:rq1` 이다. 세 경우 모두 해시태그 `{sid}` 가 유지되고 구 `…:done` 키는 생성되지 않는다(`exists=0`). |
| 정산됨 vs 만료됨 구별 | **동작** | 정산됨 경로: reserve 5 → settle 4 → 중복 settle 4 를 하면 두 번째 결과는 `{settled:false}` 이고 상태는 `4/0/[]` 다. 만료됨 경로: 마커만 삭제(인덱스 필드는 생존)한 뒤 settle 7 을 하면 `settled=true, recovered=true` 이고 상태는 `11/0/[]` 다(4+7, 기대값과 일치). |
| M-1 툼스톤 재합산 | **해소(신규 키 기준)** | team t9 에서 예약 5 / 정산 4 를 10회 했다. 툼스톤 10개가 `budget:settled:*` 에 있고 `budget:pending:team:{t9}:*` 는 라이브 1개뿐이다. 이 상태에서 라이브 5 를 예약하고 DB 를 90 으로 두면, 실 HealthChecker 결과는 **`95 / 5 / [live]`** 다(r4 에서는 135/45). `_pending_reservation_sum` 은 **5** 이고(r4 에서는 45), 라이브 settle 후는 `95/0`. gateway-cli restore 의 패턴(`budget:pending:user:{uid}:*`)도 같은 이유로 배제된다. 단, 롤아웃 이전에 생긴 구 `:done` 키는 아래 N-1 에서 다룬다. |
| S-1 SCAN-gap (team/user) | **해소** | `scan_iter` 소진 직후 실 reserve(B 7)를 끼워 넣었다. 결과는 **`62 / 12 / [A,B]`** 이고 settle 후 **`62 / 0`** 이다(r4 에서는 55/0). 인덱스 경유로 신규 예약이 live 에 잡혔다. |
| 시나리오 E' | **유지** | A 5 / B2 7 을 예약하고 A 마커를 삭제한 뒤 reconcile 하면 `57 / 7 / [B2]` 다. 이어서 settle A(3)(`recovered=true`), settle B2(7) 을 하면 **`60 / 0 / []`** 다. |
| 웜업 비인덱스 마커 | **해소** | 인덱스 필드 I 5 와 구버전식 비인덱스 마커 N 7(`ARGV[5]=''`)을 두고 reconcile 하면 `62 / 12 / [I]` 다. 즉 KEYS[4..] 의 미커버분이 live 에 더해지고 이중 계상은 없다. settle 후는 `62/0`. |
| N-1 상시 HDEL | 해소 | `HDEL` 이 `resvsum == snap` 분기 안에 있다(reconcile.lua:76-82). |
| N-2/N-3 | 수용 판단 동의 | 정확성 영향이 없는 비용·테스트 범위 문제다. |

---

## M급

### M-1 (신규, r4 S-1 수정의 회귀) client scope reconcile 이 **공유 인덱스의 모든 앱 예약을 live 로 합산**해 해당 앱 카운터를 부풀리고 resvsum 에 영구 잔량을 남긴다

- 원인:
  - client scope 의 인덱스는 유저당 하나다: `budget:pendingidx:client:{uid}`, 필드는 `<client>:<req_id>`(budget_service.py:902-903). 반면 usage 와 resvsum 은 앱별 키다(`budget:user:{uid}:<client>:P`, `budget:resvsum:user:{uid}:<client>:P`).
  - 새 reconcile 은 `HKEYS KEYS[3]` 전체를 필터 없이 순회한다(reconcile.lua:41-51). 그래서 claude-code 행의 live 에 codex 예약까지 더해진다.
  - rebuild.lua 는 `ARGV[3]` 필드 접두사로 같은 필터를 이미 하고 있다(rebuild.lua:54-60). r4 권고안도 "client scope 는 rebuild 와 같이 필드 접두사 `<client>:` 로 필터" 였지만 반영되지 않았다.
- 재현(실 HealthChecker + 실 PG + 실 Redis):
  - 초기 상태: u1, DB 행은 claude-code 10 / codex 20. 예약은 claude-code r1 5 / codex r9 9. 이때 상태는 cc `15/5`, cx `29/9` 로 정상이다.
  - reconcile 1회 후:
    - cc **`24 / 14`**(기대 15/5)
    - cx **`34 / 14`**(기대 29/9)
  - 이어서 두 예약을 정상 settle 하면:
    - cc **`24 / 9`**(기대 15/0)
    - cx **`34 / 5`**(기대 29/0)
  - codex 행이 DB 에 없는 경우(5b, 흔한 구성)에도 cc 는 `24/14` 가 되고, settle 후 `24/9` 가 남는다.
- 메커니즘: `orphan = resvsum − live` 가 음수가 된다. 그러면 `SET resvsum = live` 로 **타 앱 예약치가 resvsum 에 주입**되고, 플로어 `target = db + live` 가 **usage 를 타 앱 예약치만큼 끌어올린다**.
- 영향:
  - REDIS_DEGRADED 복구 창마다 per-app 예산 카운터가 동시 진행 중인 타 앱 예약 합만큼 과대 계상된다. 그 결과 **오탐 HARD_BLOCK** 과 거짓 임계 알림이 생긴다.
  - settle 후에도 resvsum 에 타 앱 예약치가 남아 표시 실지출(usage−resvsum)도 어긋난다.
  - 자가 치유되지 않는다. 다음 퀴에센스 틱에서는 live 가 다시 타 앱 예약을 포함하므로, 라이브 예약이 0 이 되는 틱에서야 resvsum 만 0 으로 돌아간다. usage 는 플로어 설계라 내려오지 않는다.
  - 창 안의 틱 수(재시도 창)만큼 반복 적용될 수 있다.
- 기존 테스트에 안 잡힌 이유: `test_health_checker.py` 는 eval 을 목으로 처리해 인자만 검사한다. r4 의 "client scope 공유 인덱스" 검증은 dead 스윕만 봤고 live 합산 경로는 이번 diff 에서 새로 생겼다.
- 최소 수정: reconcile 에 필드 접두사 ARGV 를 추가한다(rebuild 의 `ARGV[3]` 규약과 같다).
  - health_checker 는 client 행이면 `f"{c}:"`, 그 외에는 `""` 를 넘긴다.
  - Lua 의 HKEYS 루프에서 `field_prefix ~= '' and string.sub(f,1,#field_prefix) ~= field_prefix` 이면 `skip` 한다. 이렇게 하면 live·covered·dead 모두 자기 앱 필드로 한정된다. dead 스윕도 자기 resvsum 게이트로 보호되는 필드만 지우게 된다(r4 N-1 의 의미론과 일치).
  - KEYS[4..] 쪽은 이미 `(USER, sid, c)` 로 분리돼 있어 변경할 필요가 없다.
  - 회귀 테스트로 위 시나리오 5를 실 Lua(또는 fakeredis-lua)로 추가한다.

---

## S급

없음.

---

## N급

- **N-1 롤아웃 잔존 구 `…:done` 툼스톤(이행기 한정)**
  - 배포 이전, 그리고 롤링 중 구버전 파드가 만든 `budget:pending:…:<req>:done` 은 최대 4h(마커 TTL) 동안 남는다.
  - 새 reconcile 은 이 키를 KEYS[4..] 로 받는다. 필드 `"<req>:done"` 이 인덱스에 없으니 미커버로 판정하고 GET 값(실비)을 live 에 더한다.
  - 재현: 구 툼스톤 3개(각 4)와 라이브 5, DB 90 인 상태에서 reconcile 결과는 **`107 / 17`**(기대 95/5)이다. `_pending_reservation_sum` 은 **17** 이다.
  - 배포 후 4h 안에 열화 복구 창이 와야 발동하고 1회성이다. 그래서 N 으로 분류한다. 다만 r4 M-1 과 같은 과대 계상 경로다.
  - 수정: health_checker 의 SCAN 파서와 `_pending_reservation_sum` 에서 `endswith(":done")` 키를 스킵한다. 한 줄씩이고 이행기가 끝나면 무해하다.
- **N-2 covered 테이블·HKEYS 비용**
  - covered 는 eval 당 인덱스 필드 수만큼의 Lua 테이블이라 메모리·시간 모두 O(n) 이고 정확성 문제는 없다.
  - 다만 client scope 에서는 한 유저의 앱 행 수만큼 같은 공유 인덱스를 반복 순회한다. M-1 수정 후에도 HKEYS 자체는 전체 필드를 읽는다.
  - 고아 필드 누적(r4 N-2 수용)과 겹치면 eval 이 길어질 수 있다. 문서화만 하면 된다.
- **N-3 숫자 아닌 마커 값**
  - 인덱스 필드의 마커 값이 숫자가 아니면 `dead` 로 분류돼 필드가 HDEL 된다(마커는 생존). reserve 가 항상 `ARGV[2]`(숫자 문자열)를 쓰므로 정상 경로에서는 발생하지 않는다. 참고 항목이다.

---

## 새 결함 점검 요약 (요청 2항)

| 점검 | 결론 |
|---|---|
| covered 딕셔너리 비용 | O(index fields), 정확성 무관 (N-2) |
| client scope field→marker 재구성 | **키 재구성 자체는 정확**하다. `budget:pending:client:{uid}:` 와 `codex:r9` 를 이으면 실 마커 키와 같다. KEYS[4..] 의 `string.sub(KEYS[i], #prefix+1)` 도 `codex:r9` 로 인덱스 필드와 일치한다. **그러나 필드 필터가 없어 타 앱 예약이 live 에 합산된다** → **M-1** |
| union 이 필드에 없는 스캔 마커를 잘못 카운트 | 인덱스에 있지만 마커가 소멸한 경우는 양쪽 모두 GET nil 이라 0 이다. 비인덱스 구버전 마커는 정확히 1회 합산된다(4c). 예외는 구 `:done` 툼스톤으로, 미커버 스캔 키라 합산된다 → **N-1(이행기)** |

## 미해결 M/S 잔여

- M-1(client scope live 과대 합산): 미해결, 신규.
- r4 의 M-1·S-1 은 user/team scope 에서 해소됐다. client scope 의 S-1 SCAN-gap 도 인덱스 경유로 잡히기는 하지만, M-1 때문에 결과가 틀린다.

## 수렴 판단

r4 의 지적 4건은 실행 경로에서 모두 해소를 확인했다.
- 툼스톤 네임스페이스 이동과 15자 접두사가 정확하다.
- 정산됨/만료됨 라운드트립이 동작한다.
- M-1 재현이 95/5 로 나온다.
- SCAN-gap 결과가 62/12→62/0 이다.
- 웜업 비인덱스 마커 결과가 62/12 다.

그러나 S-1 수정으로 도입된 인덱스 기반 live 계산이 client scope 의 공유 인덱스를 필터 없이 합산한다. 실 HealthChecker 에서 claude-code 카운터가 +9 / resvsum +9 영구 잔량으로 재현되는 M급 회귀가 생겼다. rebuild.lua 와 같은 필드 접두사 필터 한 개로 닫힌다.

VERDICT: NOT CONVERGED
