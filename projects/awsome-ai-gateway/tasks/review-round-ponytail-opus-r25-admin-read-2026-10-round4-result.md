# R25 라운드 4 — 수정 검증 결과 (kiro/opus)

대상: `/tmp/r25-round3-full.diff`(5358줄). 시작할 때와 끝날 때 모두 `git diff HEAD` 와 byte 단위로 일치함(cmp).
라운드 3 처리 내역의 주장은 실행해서 확인함. 리포지토리 코드는 수정하지 않음(임시 vitest 1개는 실행 후 삭제).

## 실행 환경 / 기준선

| 항목 | 결과 |
|---|---|
| admin-api `pytest` 전체 | **1012 passed**, 50 skipped (py3.14 venv) |
| admin-ui `npx tsc --noEmit` / `npm run typecheck` (CI 하드 게이트) | **EXIT=0** (오류 0) |
| admin-ui `vitest run` | **452 passed** (39 files) |
| admin-ui `next build` / `npm run lint` | EXIT=0 / EXIT=0 (경고만) |
| 차트 테스트 `deployment/charts/llm-gateway/tests` | **51 passed** (R3-N7: 산식에 `audit_flush = 10` 포함 확인) |
| 실 Postgres | pgvector:pg16 임시 컨테이너, `db/init/*.sql` 적용 후 `alembic upgrade head`(=0048) |
| 채팅 하네스 | 실제 `chat_agent` 라우터, 실 PG, 실 Redis 7. **prod 와 같은 py3.11 admin-api 이미지**(uvicorn 0.44, redis-py 7.4.0)에 현재 `src` 를 마운트해 uvicorn 프로세스 2개(=pod 2개)로 띄움. boto3 AgentCore 는 fake(청크 간격, stop/commit 지연, commit 1회 실패, 상류 예외를 주입할 수 있음). 시그널은 `docker kill -s TERM/KILL` 로 PID1 uvicorn 에 실제로 보냄. `--timeout-graceful-shutdown 1` |
| drain 단위 | 실제 `drain_chat_bg_tasks`/`_spawn`/`_spawn_persist` 를 대상으로 **py3.11.17 과 py3.14.4 양쪽**에서 10건 실행 |
| UI | 실제 `ChatLayout` + 실제 `useChatStream`(fetch stub) 대상 임시 vitest 4건. MessageList/api/next-intl 만 mock |

---

## 요청 항목별 검증

| # | 주장 | 결과 | 근거 |
|---|---|---|---|
| 1 | R3-S1 tsc | ✅ | `rateLimitsBffTraversal.test.ts:83` `] as Record<string, string>[]`. `npm run typecheck` EXIT=0 (CI `awsome-ai-gateway-ci.yml:377` 과 같은 명령) |
| 2a | drain 이 `_persist_tasks` 는 cancel 하지 않고 완주를 기다림 | ✅ | 단위: snapshot 에 이미 있던 persist 가 완료됐고 `cancelled()=False`(3.11/3.14). 실 SIGTERM **B2**(정상 persist commit 4s 도중 SIGTERM): drain snapshot `[[producer,False,0],[persist,True,0]]` → commit.done → 행 `c0 c1 c2` 1개, 노트 없음(R3 에서는 행이 없었음) |
| 2b | `cancelling()>0` producer 는 재취소하지 않음 | ✅ | 단위: 핸들러 안의 producer(cancelling=1)와 cancel 이 요청만 되고 아직 전달 전인 producer 모두 두 번째 CancelledError 없이 완주. 실 **B3**(Stop → `stop_runtime_session` 4s 도중 SIGTERM): snapshot `[[producer,False,1]]` → stop.end → persist → `[취소됨]` 행 저장(R3 에서는 행 없음). 실 **B4**(Stop → 취소 노트 commit 4s 도중 SIGTERM): `[취소됨]` 행 저장(R3 에서는 행 없음) |
| 2c | timeout 상한 유지 | ✅ | 단위: 취소를 삼키는 태스크 → 0.30s(timeout 0.3), 10s persist → 0.40s(timeout 0.4). 실 **B7**(persist 30s, drain 3s): `drain.end dt=3.004`. 이 경우 행은 유실되지만 상한 안에서 끝나므로 의도된 동작. 루프 종료 시 asyncio.run 이 남은 태스크를 취소하면서 finally 가 돌아 클레임도 삭제됨(TTL -2) |
| 2d | 비-persist 태스크(프리웜 등)는 cancel | ✅ | 단위: 프리웜류 sleep(100) 태스크가 0.001s 안에 cancelled. 실 **B1**(스트림 도중 SIGTERM): producer 1회 cancel 후 `[중단] 서버 재시작…` 행 저장, drain 0.008s |
| 3a | 409 시 user+pending 유령 쌍 제거 | ✅ | 폴링 중 전송 → 409 → `["user:Q1:","assistant::"]`(Q2 와 새 pending 이 사라짐) |
| 3b | 폴링 중이었다면 `pollForAnswer` 로 재개 | ✅ (잔여 N-1) | 409 뒤에 getHistory tick 이 다시 돌고, DB 에 A1 이 생기면 화면이 `["user:Q1:","assistant:A1:"]` 로 바뀜. 다만 재개된 폴링 버블이 **pending 이 아닌 빈 버블**로 남음 → N-1 |
| 3c | N5: 폴링 중이 아닐 때 409 | ✅ | 완료된 세션 → 409 → `[Q1, A1]` 그대로, 3.5s 동안 getHistory 추가 호출 0회(폴링 오개시 없음). 새 세션에서 첫 질문이 409 → `[]` |
| 4 | R3-N1 | ✅ | **N1a**(정상 persist commit 3s 도중 Stop, drain 없음): 프레임 `text×3, done`(error 없음), `stop.start` 이벤트 없음, 행은 노트 없는 완결 답변. **N1b**(같은 조건에서 commit 이 실패): `commit.fail` → 취소 노트 경로로 넘어감 → `body.close`, `stop_runtime_session`, `error` 프레임, `[취소됨]` 노트 행 저장. 설계대로 동작함 |
| 5a | 내 토큰일 때만 연장 | ✅ | Lua 를 실 Redis 에 직접 실행(redis-py 7.4.0): own→`1`, TTL 120 / 남의 토큰→`0`, TTL 30 유지 / 키 없음→`0`. 라이브 **N2a**: 실행 중 키를 `foreigntoken EX 90` 으로 덮어씀 → t=65s TTL 26(연장 안 됨), 소유자 종료 뒤에도 foreign 키가 그대로 남음(남의 키를 지우지 않음) |
| 5b | 파드가 죽으면 TTL 만료로 해제 | ✅ | **N2**: 클레임 TTL 119 → t=58 62 → t=64 **116**(갱신됨, commandstats `eval:1 expire:1`). 살아 있는 동안 p2 POST 409 → SIGKILL 직후에도 409 → **SIGKILL 116.1s 뒤** 키 소멸 → p2 POST 200 으로 스트리밍. R3 에서는 960s 동안 잠겼음 |
| 5c | Lua 인자 계약 | ✅ | `eval(script, 1, key, token, 120)`. int `120` 은 redis-py 가 bulk string 으로 인코딩하고 `EXPIRE key "120"` 로 정상 처리됨. 반환값 1, TTL 120 실측 |
| 6 | R3-N3 취소 지연 ~1s, busy loop 없음 | ✅ | agent 가 침묵하는 구간(청크 간격 8s)에서 크로스파드 `/cancel` → error 프레임까지의 지연을 21회 측정. /cancel 응답 기준 최대 **0.81s**(R3: 7.02s). POST 시작 기준으로는 1회 1.72s 가 나왔고 나머지는 ≤0.88s(콜드 경로로 추정). 10s 침묵 동안 Redis 명령은 `exists` **10회**뿐(초당 1회), pod CPU 2 ticks(0.02s)/10s. 청크 간격 1.3s(>1s timeout 이 반복되는 구간)에서도 청크 8개가 모두 DB 에 저장됨(`c0 … c7`). shield 덕분에 timeout 이 나도 pending 청크를 잃지 않음 |
| 7a | `_spawn(_persist` 잔존 | ✅ (잔여 N-2) | `_spawn(_persist` 는 0건. persist spawn 2곳(정상 경로 :1192, 취소 노트 :1261)은 모두 `_spawn_persist`. 다만 `except Exception` 경로의 `await _persist(...)`(:1279)는 shield 도 추적도 안 됨 → N-2 |
| 7b | 클레임 해제와 TTL 120 의 경합 | ✅ (잔여 N-3) | producer 가 끝난 뒤 남는 키는 없음: 정상/B1–B4/B3x/B7/N1 전부 TTL -2. 갱신과 finally 해제는 같은 코루틴 안에서 순차로 실행돼 서로 경합하지 않음. 다만 **갱신이 스트리밍 루프 안에서만** 일어남 → N-3 |
| 7c | persist 만 남으면 바로 빠지는지 | ✅ | 단위: persist 0.5s, drain timeout 5s → **0.49s** 에 반환. 실 B2/B3/B4: `drain.end` 가 commit.done/stop.end 와 같은 ms 에 찍힘(3.77–3.82s, timeout 15s 아님) |
| 7d | `wasPolling` 을 `stopPolling()` 전에 캡처 | ✅ | `ChatLayout.tsx:222-223` 순서 확인. 동작으로도 확인: 캡처가 뒤에 있었다면 3b 의 재개가 일어날 수 없음 |
| 7e | `_persist_tasks` discard / 누수 | ✅ | 단위: 성공 1000건 + 예외 10건 + 취소 1건 뒤 `_persist_tasks=0`, `_bg_tasks=0`. 실 하네스 `/_debug` 결과 시나리오마다 `bg=0 persist=0 active_tasks=[] relays=[]` |
| 8 | 기존 검증 | ✅ | 기준선 표 참고 |

회귀 테스트 변이 확인: 임시 복사본에서 drain 을 R3 이전 형태(snapshot 전체 cancel)로 되돌리면
`test_drain_does_not_cancel_a_persist_already_in_flight` 과 `test_drain_does_not_re_cancel_a_producer_being_cancelled` 2건이
**FAILED** 됨. 새 테스트가 S2 회귀를 실제로 잡음.

---

## 미해결 finding

M: 없음. S: 없음.

### Nits (N)

**N-1. 폴링 중 409 뒤 재개된 진행 버블에서 스피너가 사라짐** (`ChatLayout.tsx:292` 이후 `:299-301`)
- 409 분기에서 `pollForAnswer(sessionId)` 를 호출한 뒤 handleSubmit 끝의 공통 정리 `prev.map(m => m.pending ? {...m, pending:false} : m)`
  가 이어서 실행됨. 그래서 방금 남긴(또는 재사용한) 폴링 버블의 pending 이 꺼짐. 실측 `["user:Q1:","assistant::"]` (기대값 `"assistant::P"`).
  MessageBubble 은 pending 일 때만 `PendingIndicator` 를 그리므로, 답이 올 때까지(최대 15분) **빈 assistant 버블만** 보임.
  기능은 정상이고(답이 오면 교체됨) R2-N1 의 "진행 표시" 만 이 경로에서 빠짐.
- 수정: 409 분기 끝에 `return;` 을 넣거나, 공통 정리 다음으로 `pollForAnswer` 호출을 옮김.

**N-2. 예외 경로 persist 가 shield 도 추적도 안 됨 → 그 도중 SIGTERM/Stop 이 오면 부분 답변 유실** (`chat_agent.py:1279`, HEAD 부터 있던 형태)
- 실 하네스 E1: 상류 예외(청크 3개 뒤) → `except Exception` 의 `await _persist(...)` commit 도중 SIGTERM → drain 이 producer(cancelling=0)를 cancel
  → `persist.rollback(CancelledError)` → **행 없음**(대조군 E0 은 `[오류] RuntimeError…` 행 저장). 형제 `except` 절이라 CancelledError
  핸들러로 넘어가지도 않음. 로컬 Stop 이 같은 창에 와도 같은 결과.
- 창은 commit RTT 이고 에러 경로에서만 생김. R3-S2 와 같은 종류의 구멍이 남은 마지막 경로임.
  수정: 나머지 두 곳과 같이 `await asyncio.shield(_spawn_persist(_persist(...)))`.

**N-3. 클레임 갱신이 스트리밍 루프 안에서만 일어남 — 루프 전후의 긴 blocking 구간이 TTL 120s 를 넘으면 클레임이 풀려 이중 실행이 열림** (`chat_agent.py:888-892` invoke, `:1144` 비스트리밍 fallback `body.read()`)
- 하네스에서 TTL 을 5s, 갱신 주기를 2s 로 줄여 재현(코드 수정 없이 모듈 상수만 monkeypatch).
  대조군(스트리밍 12s)은 p2 POST **409** 로 갱신이 정상 동작함. `invoke_agent_runtime` 이 8s 걸리는 경우에는 t=6.5s 에 키가 이미 없고
  p2 POST 가 **200** → 두 파드가 같은 세션을 동시에 실행함. DB 결과 `user q, user q2, assistant, assistant`.
- 실제 값으로 환산하면 invoke(헤더 도착 전) 또는 비스트리밍 fallback 이 120s 를 넘어야 함. 일반적인 스트리밍 agent 에서는 드물지만
  `read_timeout=900` 이라 상한 자체는 없음. TTL 을 960 에서 120 으로 줄이면서 생긴 트레이드오프임.
  수정: 갱신을 별도 heartbeat 태스크로 분리(producer 수명 동안 60s 주기, finally 에서 cancel). 또는 invoke/fallback 도 같은 `wait_for` 폴링 패턴으로 감쌈.
- 참고(기존부터 있던 문제, 영향 미미): finally 해제가 `GET == token` 후 `DELETE` 로 원자적이지 않음. 갱신 Lua 와 같은 compare-and-del 로 바꾸면 깔끔함.

**N-4. 크로스파드 취소(flag 경로) 처리 중 drain → `stop_runtime_session` 이 중복 호출됨** (`chat_agent.py:1165-1175` → 핸들러 `:1237-1241`)
- 실 **B3x**: flag 경로의 정상 흐름에서 `stop_runtime_session`(4s) 도중 SIGTERM → producer 는 cancelling=0 이라 cancel 됨 → 핸들러가
  다시 stop 호출(`stop.start` 2회) → `[취소됨]` 행은 저장됨(✅). 첫 호출은 to_thread 워커에 버려진 채 계속 돌고, drain 은 두 번째 호출이 끝날 때까지
  기다림(4.01s, SIGTERM 시점부터 stop RTT 1회분). 최선노력 호출이라 기능 문제는 없음. 핸들러에서 `cancelled_by_user`(이미 정상 경로에서 stop 함)이면 stop 을 건너뛰면 됨.

참고 — N1b(정상 persist 가 실패한 뒤 Stop)에서는 완결 답변에 `[취소됨]` 노트가 붙고, 이미 끝난 런에 stop 이 호출됨.
항목 4 의 명세("실패했다면 취소-노트 경로로") 그대로의 동작이라 finding 으로 분류하지 않음.

## 검증 통과 요약

라운드 3 의 S1(tsc), S2(drain 이 persist 와 취소 중인 producer 를 재취소), S3(폴링 중 전송 막다른 길)가 모두 해소됨을 실행으로 확인함.
S2 는 R3 에서 행이 유실됐던 B2/B3/B4 를 py3.11 실 uvicorn + SIGTERM 으로 다시 재현했고, 세 경우 모두 assistant 행이 저장됨.
N1(라이브/DB 정합), N2(TTL 120 + 내 토큰일 때만 갱신, SIGKILL 뒤 116s 에 해제), N3(취소 지연 ≤~0.9s, busy loop 없음), N7(차트 산식)도 확인함.
남은 것은 N 4건: 409 재개 뒤 스피너 소실, 예외 경로 persist 미보호, 루프 밖 구간의 클레임 미갱신, 이중 stop 호출.

임시 산출물(/tmp/r25r4 하네스·변이 복사본, 임시 vitest 1개, PG/Redis/pod 컨테이너)은 정리함. `admin-ui/.next` 는 검증을 시작하기 전부터
있던 디렉터리라 `next build` 로 갱신만 하고 지우지 않음.

VERDICT: **CONVERGED** (M 0 / S 0 / N 4)
