# R25 라운드 3 — 수정 검증 결과 (kiro/opus)

대상: `/tmp/r25-round2-full.diff`(5057줄). 시작 시점 `git diff` 와 byte 단위로 동일한지 확인함(cmp 일치).
라운드 2 처리 내역의 주장은 그대로 믿지 않고 실행해서 확인함. 리포지토리 코드는 수정하지 않음.

## 실행 환경 / 기준선

| 항목 | 결과 |
|---|---|
| admin-api `pytest` 전체 | 1010 passed, 50 skipped |
| admin-ui `vitest run` | 452 passed (39 files) |
| admin-ui `npx tsc --noEmit` (= CI 하드 게이트 `npm run typecheck`) | **실패 1건** → S1 |
| admin-ui `next build` | EXIT=0 |
| 차트 테스트 `deployment/charts/llm-gateway/tests` | 51 passed |
| 실 Postgres | pgvector:pg16 임시 컨테이너, `db/init/*.sql` 적용 후 `alembic upgrade head` 실행(=0048) |
| 채팅 하네스 | 실제 `chat_agent` 라우터 + 실 PG + 실 Redis(redis:7) + **uvicorn 0.44 프로세스 2개(=pod 2개)**. boto3 AgentCore 는 fake(청크 지연, stop/persist 지연 주입 가능), SIGTERM/SIGKILL 은 실제 시그널 |
| UI | 실제 `ChatLayout`/`useChatStream`/`ExportButton`/`analytics-export route`/`getRateLimitStatusAction` 대상 임시 vitest 15건. MessageList/api/next-intl 만 mock |
| Docker | Dockerfile 의 CMD 문자열을 그대로 기존 admin-api 이미지(uvicorn 0.44, py3.11)에서 실행 |

---

## 요청 항목별 검증

| # | 주장 | 결과 | 근거 |
|---|---|---|---|
| 1 | apps.py `.correlate(User)` | ✅ | 실 PG 에서 한 팀에 사용자 4명, 다른 팀에 3명, 팀 없는 사용자 1명. team/org/user 정책을 섞어 둠. `GET /admin/apps/{claude-code,codex,cowork}` 와 PATCH default-model, PATCH models 모두 **200** 이고 allowed_users 는 우선순위대로 나옴(cowork=[b2(user 행), n1(팀 없음)], codex=[org 정책 팀 t2 의 b1,b3 + n1]). `Select.correlate` 를 무력화한 변이에서는 `CardinalityViolationError` 로 500 → 수정이 실제로 효과가 있음 |
| 2 | dashboard D-cap 분모 | ✅ | 실 PG: 팀A $100/사용 $50, 팀B D-cap 만 있음/사용 $900, 팀 없는 사용자는 per-app config 만 있음/사용 $25 → `used=50 limit=100 util=50.0` (R2 실측값은 950%) |
| 3a | 동시 POST 200/409 | ✅ | 같은 pod 동시 2건 `[200, 409]`, pod 간 동시 2건 `[200, 409]`. 두 경우 모두 DB 에는 user+assistant 행 1쌍만 남음 |
| 3b | 409 가 소유권 확인 뒤 | ✅ | 타인 세션에 Redis 클레임을 걸어 둔 상태에서 POST 하면 **403**(409 아님). 같은 조건의 `/cancel` 도 403 이고 플래그를 세우지 않음 |
| 3c | 409 가 첫 요청의 relay 를 pop 하지 않음 | ✅ | Q1 진행 중 같은 pod 에 Q2 → 409, 그 뒤 `_active_relays == [sid]` 유지 |
| 3d | 크로스파드 cancel 플래그 + body.close + stop_runtime_session | ✅(지연 N3) | pod1 에서 실행 중 pod2 로 `/cancel` → 0.93s 안에 스트림 `error,done`. DB 에 `[취소됨] 사용자가 분석을 중단했습니다` 저장, 이벤트 로그에 `stop_runtime_session` → persist → `body.close()` 순서로 찍힘. 취소 뒤 pod2 에 Q2 → 200 |
| 4 | drain: 진입 스냅샷만 cancel·재취소 안 함·timeout | ❌ **S2** | timeout 상한은 지켜짐(2s 설정에서 2.00s), handler 가 snapshot **뒤에** spawn 한 persist 도 기다림(B8 OK). 그러나 snapshot 에 **이미 있던** persist 태스크와 이미 취소 처리 중인 producer 를 다시 cancel 함 → 3개 시나리오에서 assistant 행 유실 |
| 5 | 정상 persist `_spawn`+shield, 핸들러가 persist_task 재사용 | 부분 ✅ (N1) | Stop 이 정상 persist 도중에 오는 경우(drain 없음): INSERT 1회, 완결 답변이 **노트 없이** 저장됨 → 이중 INSERT 없음. 다만 라이브 스트림은 `done` 뒤에 `error(Cancelled)` 를 보내고, 끝난 런에 `stop_runtime_session` 도 호출함. drain 이 끼는 경우는 S2 |
| 6 | ChatLayout 폴링 상태머신 | ✅ (회귀 S3) | 확인됨: 마지막이 assistant 면 폴링 안 함 / user 면 pending 버블 표시 후 3s 폴링, 답이 오면 정지 / 새 대화 시 interval 해제·옛 결과 미반영 / 진행 중 tick 이 세션 전환 뒤 도착해도 sid 가드로 폐기 / 15분 상한에서 정지·`analysisTimeout`·pending 해제 / reattach 502 도 폴링. 단, 폴링 중 전송하면 막다른 길이 됨 → S3 |
| 7 | useChatStream | ✅ | 409 → `AnalysisInProgressError`, 410 → `SessionExpiredError`(둘 다 onError 미호출, 재전파). `cancel()` 은 POST `/cancel` 이 resolve 될 때까지 resolve 안 됨. Stop 클릭 시 `/S1/cancel` POST 확인. (403·`cancelled:false` 응답은 무시함 — 동작 자체는 정상) |
| 8 | ExportButton client + 필터 칩 | ✅ | `client=codex` 가 `/api/analytics-export?…&client=codex` → 상류 `/admin/analytics/export?…&client=codex` 까지 전달됨(실제 route 핸들러). client 가 null 이면 파라미터 없음. 백엔드 `export_analytics` 도 client 를 서비스로 넘김. 칩/해제 링크는 page.tsx 와 i18n 키(ko/en)가 있는지 코드로 확인함(렌더 테스트는 안 함) |
| 9 | Dockerfile / grace / probe | ✅ | `helm template` base/dev/prod/prod-loadtest 모두 rc=0. admin-api 값: `terminationGracePeriodSeconds=50`, preStop `sleep 10`, liveness=/health, readiness/startup=/health/ready 이며 `httpGet` 과 나머지 필드가 형제 구조로 정상 렌더됨. env `WORKERS=1`, `UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT=10`. CMD 를 실제 이미지에서 실행: PID1 이 uvicorn(exec 성공), env 미설정이면 `--timeout-graceful-shutdown 10`, `=3` 이면 3. SSE 연결이 열린 상태에서 SIGTERM → lifespan shutdown 까지 0.5s. 참고로 HEAD 의 readinessProbe 는 원래 정상이었음(오염은 WIP 안에서 생겼다가 복구된 것) |
| 10 | numOrNull / analytics-export | ✅ | `''→null`, `'  '→null`, `'5.00'→5`, `'abc'→null`, `0→0`, `'0'→0`, `null→null` |

---

## Should-fix (S)

**S1. admin-ui typecheck(CI 하드 게이트)가 깨짐 — 라운드 2 의 "tsc clean" 주장은 사실이 아님**
(`admin-ui/tests/unit/rateLimitsBffTraversal.test.ts:84`)
- `npx tsc --noEmit` →
  `TS2345: Argument of type '{ scope?: undefined; … } | …' is not assignable to parameter of type 'Record<string, string>'`.
- 라운드 1 트리(HEAD + round1 diff)에서는 tsc 오류가 0건. 라운드 2 에서 이 파일을 다시 쓰면서 생긴 회귀임.
  `.github/workflows/awsome-ai-gateway-ci.yml:375` 의 `npm run typecheck` 는 하드 게이트라서 CI 가 빨간불이 됨.
  vitest 와 `next build` 는 통과하므로 테스트 실행만으로는 드러나지 않음.
- 최소 수정: 배열을 `const cases: Record<string, string>[] = [{}, { scope: 'user' }, { scope_id: 'abc' }]` 로 명시 타입 지정.

**S2. drain 이 이미 진행 중인 persist 와 "취소 처리 중" producer 를 다시 cancel 함 → 롤아웃 시 assistant 행 유실 (R2 N-a/N-b 주장 불성립)**
(`admin-api/src/app/routers/chat_agent.py:336-344` drain, `:1141-1144` 정상 persist, `:1174-1203` 핸들러)
- 원인: `tasks = list(_bg_tasks)` 에는 producer 뿐 아니라 이미 `_spawn` 된 **persist 태스크**도 들어 있음. snapshot 전체를 `cancel()` 하므로
  shield 를 해도 inner 태스크 자체가 취소됨. 또 사용자 Stop 으로 이미 핸들러 안에 있는 producer 를 다시 cancel 하면 핸들러의
  `await to_thread(stop_runtime_session)` / `await shield(...)` 에서 두 번째 CancelledError 가 올라옴. 이건 `except Exception` 에
  잡히지 않아 핸들러가 그대로 빠져나감.
- 재현(실제 uvicorn + SIGTERM, 실 PG):
  - B2 `done` 발행 뒤 정상 persist commit 도중 SIGTERM → drain `0.00s` 에 반환, **assistant 행 없음**(user 만 남음). 완결된 답변이 사라짐.
  - B3 사용자 Stop → `stop_runtime_session` 진행 중 SIGTERM → persist 가 spawn 조차 안 됨, **행 없음**.
  - B4 사용자 Stop → 취소 노트 persist 진행 중 SIGTERM → **행 없음**.
  - 대조군: B1(스트림 도중 SIGTERM)과 B8(handler 가 persist 를 snapshot 뒤에 spawn)은 `[중단]` 노트로 저장됨. 회귀 테스트
    `test_drain_waits_for_persist_spawned_during_cancellation` 은 B8 형태만 검증해서 이 문제를 잡지 못함.
  - 실제 창 크기는 DB commit 지연(ms)과 stop_runtime_session RTT 이므로 작음. 하지만 라운드 2 가 "drain/취소가 write 도중에 와도
    유실 안 됨" 이라고 명시한 바로 그 경로임.
- 최소 수정(하네스에서 monkeypatch 로 실측 — B1·B2·B3·B4 모두 행 저장 확인):
  ```python
  for t in list(_active_tasks.values()):          # producer 만, persist 는 절대 cancel 안 함
      if not t.done() and t.cancelling() == 0:      # 이미 사용자 취소 중이면 건너뜀 (py3.11+)
          t.cancel()
  # 이후 기존 deadline 루프로 _bg_tasks 가 빌 때까지 대기
  ```
  회귀 테스트에는 "snapshot 시점에 이미 존재하는 persist" 케이스와 "cancelling()==1 인 producer" 케이스를 추가.

**S3. 크로스파드 폴링 중 전송하면 막다른 길 — 질문/답 유령 버블이 남고 Q1 답은 새로고침 전까지 안 보임 (R2 S10 수정 (c) 의 회귀)**
(`admin-ui/src/components/chat/ChatLayout.tsx:220` handleSubmit 의 `stopPolling()`, `:281-288` 409 분기, `:415` Stop 버튼 조건)
- 재현(실제 ChatLayout vitest): 저장된 세션의 마지막이 user 이고 reattach 가 404(다른 pod 가 실행 중) → 폴링 + pending 버블.
  이때 입력창은 활성 상태이고 **Stop 버튼은 없음**(`isStreaming=false`). Q2 를 전송하면 `stopPolling()` 이 실행되고 서버는 409
  (크로스파드 클레임 — 3a 에서 동작 확인됨) → 화면은 `["user:Q1","assistant:","user:Q2","assistant:"]`. 이후 9s 동안 getHistory 호출
  0회로 폴링이 재개되지 않음. Q1 의 답이 DB 에 저장돼도 화면에는 나타나지 않음.
- prod(replica 3, HPA 3-10, affinity 없음)에서는 이 경로가 기본 경로임. 라운드 2 가 고치려던 S10/S12 사용자 경험이 여기서 다시 깨짐.
  Q2 는 서버에 기록되지 않은 유령 버블임.
- 최소 수정: `AnalysisInProgressError` 분기에서 방금 넣은 user+pending 쌍을 제거(`prev.slice(0, -2)`)하고, 전송 직전에 폴링 중이었다면
  `pollForAnswer(sessionId)` 로 재개. 또는 폴링 중에는 전송을 막고 Stop(=`/cancel`, 이제 크로스파드로 동작함)을 노출
  (`isStreaming || pollRef.current` 기준).

## Nits (N)

- **N1. 정상 persist 도중 Stop 하면 라이브와 DB 가 어긋남** (`chat_agent.py:1174-1190`) — 항목 5의 "노트 없이 저장" 은 **맞는 동작**임
  (완결된 답변이므로). 하지만 핸들러가 `saw_done`/`persist_task` 여부와 관계없이 `error(Cancelled)` 프레임을 발행하고 이미 끝난 런에
  `stop_runtime_session` 을 호출함. 실측 프레임 `['text','text','done','error']` vs DB `c0 c1`(노트 없음). 화면은 "취소됨" 이고 새로고침하면
  정상 답변으로 보임. 수정: `persist_task is not None` 이면 error 발행과 stop 을 건너뛰고 persist 만 기다리기.
- **N2. 소유 pod 가 비정상 종료(SIGKILL/OOM)되거나 drain 이 timeout 되면 클레임이 960s 동안 남음** (`chat_agent.py:365`, finally 정리에 의존)
  — 실측: 소유 pod SIGKILL 뒤 생존 pod 에 `/cancel` → `{"cancelled": true}` 인데 POST 는 계속 409, TTL 959s. 소유자가 없어서 취소 플래그를
  읽을 주체도 없음. 해당 세션은 16분 동안 잠김("새 대화" 로는 우회 가능). drain timeout(B7)에서도 finally 가 안 돌아 같은 상태가 됨.
  수정: 클레임 TTL 을 짧게(예: 60s) 잡고 producer 루프에서 갱신(`SET … XX EX`). 또는 `/cancel` 이 클레임 heartbeat 가 오래됐으면 회수.
- **N3. 크로스파드 cancel 지연이 "~1s" 가 아니라 최대 10s** (`chat_agent.py:899,916`) — 플래그는 루프 맨 위에서만 확인하고, 청크 대기는
  `wait_for(…, HEARTBEAT_SECS=10)` 임. agent 가 침묵하는 구간(청크 간격 8s)에서 측정한 취소 지연은 **7.02s**. 그 사이 사용자가 다시
  전송하면 409(S3 와 겹침). 수정: `wait_for` timeout 을 `min(HEARTBEAT_SECS, _CANCEL_CHECK_SECS)` 로.
- **N4. `/cancel` 응답이 실행 중인 분석이 없어도 `cancelled: true`** (`chat_agent.py:660`) — Redis 가 있으면 `flag_set` 만으로 true 가 됨.
  유휴 세션에서 실측해도 true. 뒤이은 POST 가 플래그를 지우므로 기능상 해는 없지만, 응답 의미가 "플래그 기록됨" 이 됨. UI 가 이 값을
  쓰지 않으므로 이름이나 주석으로 명시하는 정도면 됨.
- **N5. 일반 409(Stop 직후 재전송 경합)에도 유령 Q2 와 빈 assistant 버블이 남음** (`ChatLayout.tsx:281-288`) — 실측
  `[…,"user:Q2","assistant:"]`. S3 와 같은 수정으로 해결됨.
- **N6. 처리 내역에 없는 변경** — `budget_service.py:1640-1648` TEAM_LEADER 예산 summary 범위를 소속 팀에서
  소속 팀 + `leader_user_id` 로 리드하는 팀으로 넓힘("R2-N11"). analytics/keys-count 와 같은 규칙이라 방향은 맞음. 다만 권한 범위를
  넓히는 변경인데 처리 내역 표에도, 회귀 테스트에도 없음. `audit_reconcile.py:213` 경계 `-1µs` 는 ms 로 절단되므로 µs 가 0이 아닌
  경계에서는 효과가 없음(근사치, 영향 미미).
- **N7. 차트 grace 테스트 산식에서 audit flush 가 빠짐** (`test_deployment_safety.py` `grace >= prestop+graceful+drain+5`) — 주석/values
  산식(…+audit 10)과 다름. 현재 50 ≥ 45 라 통과하지만, audit 을 포함한 산식(=50)으로 검사해야 한도를 지킬 수 있음.

## 검증 통과 요약

M1(apps 500) 해소와 S1-잔여(D-cap 950%) 해소를 실 PG 에서 확인함. 동시 POST TOCTOU 해소, 크로스파드 409/cancel,
소유권 확인 뒤 409, relay 보존, uvicorn graceful 상한, grace 50/probe 구조, ExportButton client 전달, numOrNull, typed 409/410,
폴링 상태머신의 개별 전이(P1–P8)도 모두 실행으로 확인함.
남은 문제는 drain 이 persist 를 재취소하는 S2(라운드 2 주장 자체가 틀림), 폴링 중 전송 막다른 길 S3(이번 라운드 회귀),
CI typecheck 실패 S1.

임시 산출물(/tmp/r25r3, 임시 vitest 2개, 임시 worktree, PG/Redis 컨테이너, `.next` 빌드 산출물)은 정리함.

VERDICT: NOT CONVERGED
