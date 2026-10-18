# R25 라운드 2 — 수정 검증 결과 (kiro/opus)

대상: `/tmp/r25-round1-full.diff`(3388줄, 51 파일) = 라운드 1 처리분. 처리 내역 표의 주장은 그대로 믿지 않고 직접 실행해서 확인함.
⚠️ 검증 도중(09:44:32 UTC) 작업트리의 `admin-api/src/app/routers/chat_agent.py` 가 외부에서 수정됨(`import time` 1줄 추가, 그 외 동일 —
diff 를 HEAD 에 적용한 워크트리와 51개 파일 전부 cmp 로 대조). 아래 chat_agent 라인 번호는 그 1줄이 반영된 현재 파일 기준.

## 직접 실행한 검증

| 항목 | 결과 |
|---|---|
| admin-api `pytest`(전체) | 1005 passed, 50 skipped (보고와 일치) |
| admin-ui `vitest run` / `tsc --noEmit` | 453 passed / clean |
| **실 Postgres**(pgvector:pg16 임시 컨테이너, init SQL + `alembic upgrade head`=0048) 에 실제 라우터/서비스 함수 호출 | S1·S3·N7·N13·N14 아래 |
| S3 `_build_app_policy` 를 실 DB 에서 호출 | 팀 소속 사용자 2명 이상이면 `CardinalityViolationError` → **M1** |
| S3 `.correlate(User)` 를 넣은 사본으로 384 (user,client) 조합(user×team×org 행 유무 4³×팀유무) 을 gateway `auth_service.py:136-176` 로직을 그대로 옮긴 오라클과 대조 | 불일치 0 — 우선순위 로직 자체는 동치 |
| S1 `dashboard_kpi` 실 DB | R1 재현 50% ✓, per-app 전용 config 50% ✓, **D-cap 전용 팀 config 950%** ✗ → `max_budget_usd IS NOT NULL` 추가한 사본은 50% |
| 실제 `_agentcore_producer`/`post_message`/`cancel_message`/`drain_chat_bg_tasks` + 실 DB(`AsyncSessionLocal` 교체) | 청크 랜덤 분할 200회 OK, TOCTOU 재현, cancel 영속화 OK, 이중 cancel·정상 persist 중 cancel 유실 재현 |
| uvicorn 0.44 + 실제 `_StreamRelay`/`_spawn`/`drain_chat_bg_tasks` 로 SIGTERM (SSE 연결 유지/해제 두 경우) | 두 경우 모두 lifespan drain → producer CancelledError 경로 실행(연결이 붙어 있어도 uvicorn 이 http.disconnect 로 스트림을 끊음) — S11 기본 경로 OK |
| 임시 vitest: 실제 `ChatLayout`(MessageList/api 만 mock) | 폴링 중 새 대화 → 옛 세션 대화가 덮어씀, 마운트 3초 안에 전송 → 낙관적 Q2/pending 버블 소거 재현 |
| 임시 vitest: 실제 `api-client`/`getRateLimitStatusAction` | S6 정확 경로만 fetch(유사 7경로 INVALID_PATH), S7 put/post/delete/patch 4종 모두 `details.warnings` 보존, 422 는 detailText 폴백, S8 정규화 OK |
| N13 회귀 테스트 변이 실험(productivity.py 일시 변경 후 원복, cmp 확인) | 분자 total_cost 변이 → 실패 ✓ / SQL 코호트 필터 삭제·union→dev_prod 변이 → **통과**(테스트가 mock) |
| N-UI5 actions/*.ts 메서드 카운트 HEAD 대조 + `withRetry(` 본문 괄호 매칭 스캔 + withRetry 래퍼 제거 후 정규화 텍스트 동치 비교 | 7파일 모두 메서드별 개수 동일, `await (` 0건, 뮤테이션 감싼 withRetry 0건(잔여 38건 전부 GET), 래퍼 제거 외 차이는 의도된 `numOrNull` 블록뿐 |
| 기타 | N7 실 DB(소속+리드 2팀=3, 팀·리드 없음=0, 소속만=1, `team_id` 파라미터 무시) ✓, N14 실 DB(앱별 행·다른 달·같은 uuid 다른 scope 제외) ✓, S4 `reconcile()` 직접 호출, drain 타임아웃 경로 TypeError 재현 |

임시 산출물(/tmp/r25r2, 임시 vitest 2개, 임시 워크트리, Postgres 컨테이너)은 삭제함. 리포지토리 코드는 수정하지 않음(N13 변이는 원복 후 cmp 확인).

---

## Must-fix (M)

**M1. S3 수정이 `/admin/apps/{client}` 를 500 으로 깨뜨림 — 스칼라 서브쿼리가 비상관(uncorrelated)**
(`admin-api/src/app/routers/apps.py:252-260` `team_org_id`)
- `select(Department.org_id).select_from(Team)...where(Team.id == User.team_id).scalar_subquery()` 가 `exists()` 안에 중첩돼서
  자동 상관이 바로 바깥 `EXISTS` select 까지만 적용됨. 그 select 의 FROM 에는 `auth.users` 가 없어서, 렌더된 SQL 이
  `FROM auth.teams JOIN auth.departments ..., auth.users WHERE auth.teams.id = auth.users.team_id` 로 **users 를 다시 FROM 에 넣음**
  → 팀을 가진 사용자가 2명 이상이면 서브쿼리가 여러 행을 돌려줌.
- 재현(실 DB): 팀 1개·사용자 1명이면 정상, 같은 팀에 사용자 2명이면 `asyncpg CardinalityViolationError: more than one row returned by a subquery used as an expression`.
  실제 조직에서는 항상 이 조건이 성립함. `_build_app_policy` 를 쓰는 세 엔드포인트가 모두 500: `GET /admin/apps/{client}`(:328),
  기본 모델 PATCH(:443), 모델 토글 PATCH(:514). 쓰기 두 개는 감사 로그 enqueue 후 500 이 나서 트랜잭션은 롤백됨
  (감사 행이 남는지는 확인하지 않음).
- 기존 `test_apps_router.py` 는 세션을 mock 해서 이 문제를 못 잡음.
- 최소 수정: `.where(Team.id == User.team_id).correlate(User).scalar_subquery()`. 이 한 줄을 넣은 사본으로 384조합 오라클 대조 시 불일치 0.
  회귀 방지는 실 DB 테스트 또는 컴파일된 SQL 에서 서브쿼리 FROM 에 `auth.users` 가 없음을 단정하는 테스트로.

## Should-fix (S)

**S1-잔여. D-cap 전용 팀 config(`max_budget_usd=NULL`)가 분자에는 들어가고 분모에는 0으로 들어감 → 사용률 950%**
(`admin-api/src/app/routers/dashboard.py:502-512`)
- 새 EXISTS 는 "활성·총한도(client IS NULL) config 존재"만 확인함. `budget_service.py:596-608` 은 기본 cap D 만 저장된 팀 config 를
  `max_budget_usd=None` 으로 만듦(정상 상태 `team_budget_unset`). 분모의 `SUM(max_budget_usd)` 는 NULL 을 무시함.
- 재현(실 DB): 팀 A 한도 $100·사용 $50, 팀 B D-cap $20 만 설정·사용 $900 → `used=950 limit=100 util=950.0`(정답 50%).
- 최소 수정: EXISTS 에 `BudgetConfig.max_budget_usd.isnot(None)` 추가. 사본으로 3시나리오 모두 50% 확인.

**S12-잔여. cancel/409 가드가 프로세스 로컬이고 TOCTOU 가 있음 — prod(3~10 replica)에서는 Stop 이 대부분 무효**
(`chat_agent.py:390` 체크 ↔ `:474-475` 등록, `:531-560` cancel, `useChatStream.ts:175-179`)
- (a) TOCTOU: 체크(:390)와 등록(:474) 사이에 await 가 6번 있음(SELECT·UPDATE·history SELECT·INSERT·UPDATE·commit). 실측으로 같은 세션에
  `post_message` 2건을 동시에 보내면 `['200','200']`, producer 2개가 실행되고 `_active_tasks` 에는 1개만 남음 → `/cancel` 후에도
  1개가 계속 실행됨(어디에서도 추적되지 않는 고아라 취소할 수 없음). 더블클릭이나 두 탭에서 재현됨.
- (b) 멀티 replica: `_active_tasks`·`_active_relays` 가 pod 로컬임(S10 과 같은 근본 원인). prod `adminApi.replicaCount: 3`
  (values-eks-fargate-prod.yaml:280) 이고 Service 에 affinity 가 없음 → `/cancel` 이 소유 pod 에 닿을 확률은 약 1/3. 나머지는
  `{"cancelled": false}` 가 반환되는데 UI 는 결과를 보지 않음(fire-and-forget). 그래서 R1 S12 의 증상(과금 지속, Q2 가 같은
  runtimeSession 의 `_ORCH_LOCK` 뒤에 줄 섬)이 prod 에서 약 2/3 확률로 그대로 남음. 409 가드도 같은 이유로 약 1/3 만 동작함.
- 최소 수정: Redis(admin-api 에 이미 있음)로 옮기기 — 등록을 `SET chat:active:{sid} <pod> NX EX 900` 으로 바꿔 체크와 등록을
  원자화(이걸로 TOCTOU 도 해소, 실패 시 409). cancel 은 `SET chat:cancel:{sid} 1 EX 900` 로 기록하고, producer 가 청크·heartbeat
  루프(10s)마다 확인해서 스스로 cancel. 당장은 최소한 체크와 등록을 await 없이 붙여(예약 placeholder) TOCTOU 만이라도 닫고,
  UI 가 `cancelled:false` 를 받으면 "다른 서버에서 진행 중" 을 표시할 것.

**S10-회귀. history 폴링 폴백의 상태 머신 결함 — 다른 세션이나 진행 중 전송을 덮어씀**
(`ChatLayout.tsx:77` 시작 조건, `:142-165` pollForAnswer, `:167` handleSubmit, `:245` handleNewChat)
- (a) 폴링 중 "새 대화"를 누르면 폴링이 멈추지 않음. 옛 세션의 답이 도착하면 `setMessages(옛 세션 history)` 가 실행돼, 화면 라벨은
  `session NEW` 인데 내용은 옛 대화(`["user:Q1","assistant:A1"]`)가 됨(실제 ChatLayout vitest). 이 상태에서 보낸 질문은 새 세션으로
  가므로 화면과 서버 상태가 어긋남.
- (b) 폴링은 `'no-stream'` 이기만 하면 무조건 시작됨. 완료된 세션(마지막이 assistant)에서도 시작돼서 첫 tick(3s)에 끝나는데,
  그 3초 안에 사용자가 전송하면 tick 의 `setMessages(history)` 가 낙관적 Q2 와 pending 버블을 지움(실측: before
  `[Q1,A1,Q2,PENDING]` → after `[Q1,A1]`). 이후 스트림은 질문 없이 답 버블만 새로 만듦.
- (c) 폴링 중에는 pending 표시가 없고 입력도 막지 않음. R1 S10 의 "답도 스피너도 없음" 중 스피너는 여전히 없고, 재질문이 다른
  pod 로 가면 이중 실행됨(S12-잔여 b).
- 최소 수정: history 마지막이 `user` 일 때만 폴링 시작 + 폴링 중 pending 버블 표시. `handleSubmit`/`handleNewChat`/언마운트에서
  `clearInterval`. tick 콜백에서 `sid !== 현재 sessionId` 이면 무시.

## Nits (N)

- **N-a. 정상 경로 `_persist` 도중 cancel 이 오면 완결된 답변이 통째로 유실됨** (`chat_agent.py:684` `persisted = True` 가 DB 쓰기 **전**)
  — `done` 발행 후 INSERT 를 기다리는 동안 cancel(Stop 버튼은 EOF 까지 보임)이나 drain 이 오면 INSERT 가 취소되고, CancelledError
  핸들러의 `_persist` 는 플래그 때문에 즉시 return 함. 실측 `persisted=[]`(INSERT 를 0.5s 로 지연시킨 하네스, 실제 창은 DB 지연 ms).
  수정: 플래그를 commit 성공 후에 세우기 + 정상 경로(:986)도 `asyncio.shield`.
- **N-b. 이중 cancel(사용자 Stop 직후 pod 종료) 시 drain 이 shield 된 persist 를 기다리지 않음** — 두 번째 cancel 이 바깥 태스크만
  끝내서 `asyncio.wait(_bg_tasks)` 가 반환됨. 실측: drain 반환 시점 rows=[], 1s 뒤에야 행이 생김 → 실제 종료 경로에서는 engine
  dispose·루프 종료로 유실 가능. 수정: shield 내부 태스크도 `_bg_tasks` 에 등록(`_spawn(_persist(...))` 후 `await asyncio.shield(t)`).
- **N-c. drain 타임아웃 경로가 TypeError 를 냄** (`chat_agent.py:343`) — stdlib `logging` 에 structlog 식 kwargs(`task=`)를 넘김.
  실측 `TypeError: Logger._log() got an unexpected keyword argument 'task'` → main.py 의 `chat_drain_failed` 로 뭉개져 타임아웃
  사실이 남지 않음. `logger.warning("chat_bg_task_drain_timeout task=%s", t.get_name())`.
- **N-d. grace 예산 주석이 실제 values 와 맞지 않음** (`main.py:226-228` "values 의 terminationGrace 와 정합") — values 는 바뀌지 않았음:
  admin-api `terminationGracePeriodSeconds: 30`(values.yaml:619), preStop `sleep 10`. 10 + drain 15 + audit 10 = 35 > 30. 정상
  persist 는 ms 라 실해는 작음. 주석을 고치거나 grace 를 45 이상으로.
- **N-e. cancel 해도 AgentCore 응답 body 를 닫지 않음** (`chat_agent.py` 에 `body.close()` 없음, 실측 `body.close_called=False`) —
  to_thread 워커는 다음 청크까지 블록되고, 상류 연결은 GC 에 맡겨짐. agent 쪽은 연결이 끊기면 `pump_task.cancel()` 로
  `_ORCH_LOCK` 을 풀도록 돼 있음(admin-chat-agent main.py:1583-1590). 하지만 AgentCore 가 끊김을 컨테이너까지 전파하는지는
  **확인하지 못함**. 그래서 "Stop = 상류 과금 중단"은 결정적이지 않음. finally 에서 `body.close()` 를 명시할 것.
- **N-f. S4 — captured_providers 필터가 NULL-id 행의 이유 분류보다 앞에 있음** (`invocation_log_service.py:230`) — 실측:
  `captured=None` 일 때 `{'mantle_plane': 3, 'not_bedrock': 1}` 이던 것이 기본 설정에서는 `{'other_region': 4, 'mantle_plane': 1}`.
  Mantle/OPENMODEL 행이 `other_region` 으로 바뀌어 이유 코드 계약("문자열을 바꾸면 대시보드/알람이 깨진다", :57)이 의미상 깨짐.
  수정: `bedrock_request_id` 가 있는 행에만 적용. BEDROCK native → other_region 제외, 절단 시 `core_end` 당김, 필터 시 orphan 억제는
  의도대로 동작함. (기존 결함: `codex-gpt-5.6-*` 를 쓰는 BEDROCK_RUNTIME_OPENAI 행이 alias 접두 규칙(:179) 때문에 `mantle_plane` 으로
  분류됨 — R25 범위 밖이라 참고만.)
- **N-g. S9 — TEAM_LEADER 가 403 페이지의 "홈"을 누르면 다시 403** (`admin-ui/src/app/403/page.tsx:26`) — DEVELOPER 만 `home='/my'`.
  TL 은 `/` → 미들웨어 → `/403` 막다른 길(HTTP 리다이렉트 루프는 아님, `/403` 은 public). `role !== ADMIN` 이면 `/my` 로.
  dev-login(`dev-login/route.ts:168`)도 역할과 무관하게 `/` 로 보냄(dev 전용). 그 밖의 permissions/Sidebar/콜백
  (`checkPagePermission('/')` 기반)/미들웨어 테스트는 일관됨. DEVELOPER 동작은 그대로.
- **N-h. N13 회귀 테스트가 SQL 을 검증하지 않음** — 세션 mock 이라 코호트 필터 삭제나 union→dev_prod 변이가 모두 통과함(실측).
  실 DB 실측으로 코호트 SQL 은 맞음(A prod $10 + B git $20, 비계측 C $1000 제외 → 0.3/7.5). 의미론 지적: 라인당 비용 분자에 git 만
  보낸 사용자 비용이, 커밋당 비용 분자에 prod 만 보낸 사용자 비용이 섞임(엄밀하게는 0.1/5.0). 지표별 코호트(dev_prod / dev_git)로
  나누거나 라벨로 명시.
- **N-i. N2 잔여** — `budget_service.py:97` `_PERIOD_RE = r"^\d{4}-\d{2}$"` + `.match`(:242, seed import)가 그대로 남음.
  `'2026-08\n'`(8자)는 varchar(7) 오류, 아라비아 숫자는 어떤 질의에도 안 잡히는 seed 행이 됨. 바뀐 패턴의 호출부(usage_filters 2곳,
  analytics_service 2곳)는 모두 `fullmatch` 라 깨진 곳 없음.
- **N-j. N-UI4 잔여** — `/analytics?client=` 드릴다운 상태가 화면에 표시되지 않고, `ExportButton`(:53-60)과 `/api/analytics-export` 가
  client 를 무시함 → 같은 화면에서 CSV 와 차트 수치가 다름. 필터 칩 + export 파라미터 전달.
- **N-k. 409 UX/순서** — UI 는 409 를 `HTTP 409: {"detail":"analysis already running…"}` 영문 원문 그대로 보여줌. Stop 직후 바로 Enter 를
  누르면(cancel POST 와 경합) 재현됨. 서버는 409 를 소유권/존재 확인보다 먼저 판정해서 타인 세션의 진행 여부가 드러남(관리자끼리라
  영향은 미미). 체크를 소유 확인 뒤로 옮기고 UI 에서 409 를 i18n 처리 + 짧게 재시도.
- **N-l. reattach `'failed'`(5xx/네트워크)이면 폴링 없이 조용히 끝남** (`useChatStream.ts:206`, `ChatLayout.tsx:77`) — 404 에만 폴백하고
  나머지는 전파하지 않음(이 부분은 요구대로). 다만 502 가 나면 R1 S10 증상이 그대로임. 오류 표시 또는 폴링 폴백.
- **N-m. 처리 내역의 테스트 주장이 과장됨** — "채팅 SSE 하네스 확장(iter_chunks/분할 프레임/EOF tail/고아 future)" 라고 했지만 실제로
  추가된 것은 iter_lines 폴백 1건뿐. 라인 중간 청크 분할·CRLF·EOF 꼬리·cancel/drain/409/`/cancel` 테스트가 없음(직접 200회 랜덤
  분할 실측은 통과). S1~S5·S10~S12·N7·N14 회귀 테스트도 없음 — M1 이 통과한 이유가 이것.
- **N-n. 소소** — budget_service 의 주석과 로그 hint 가 아직 "usage_logs 집계로 대체"(:1656-1660, :1696). `numOrNull("")` 이 0 을
  돌려줌(주석은 "0 으로 접지 않는다"). N-C11 테스트의 `a#b` 케이스는 fragment 절단 때문에 'missing params' 로 400 이 나서, 의도와
  다른 이유로 통과함. usage-trend 정상 경로 테스트 없음. N-C7 합성 에러 문구는 하드코딩된 한국어(i18n 아님).

## 검증 통과(요청 항목별)

- **S2** LIMIT 제거 → 실사용 전건 + seed 병합 → Python 재정렬 후 상위 50. 정렬 키 `(cost desc, User.id)`, 활성 사용자 수 기준 GROUP BY 라 규모 합리적.
- **S5** overview·models·users 세 곳 모두 `status IN (ERROR, TIMEOUT)`. UsageStatus 가 3값이라 분자 ⊆ 분모. EventLog abnormal 과 정의 일치. 기존 테스트 영향 없음(전체 통과).
- **S6** 정확히 `/cli/downloads` 만 허용(유사 7경로 거절, fetch 1회). **S7** 4개 메서드 모두 `error.details` 보존, top-level 422 는 detailText 폴백.
- **S8** 숫자·Decimal 문자열·null·파싱 불가 모두 처리(`"abc"→null`). 차트의 `Number()` 방어는 멱등이라 중복돼도 문제없음. 드롭되는 `target_id/scope` 는 소비처 없음.
- **S11** uvicorn 0.44 에서 SSE 연결이 붙어 있어도 lifespan drain 이 실행됨. 단일 cancel 이면 shield persist 완료(`[오류] 서버 재시작…`) 실측.
- **S12** 단일 pod 기준: 소유자 검증(404/403), `[오류] 사용자가 분석을 중단했습니다` 영속화, 메타(title/message_count) 갱신, 완료 세션 취소 시 `{"cancelled": false}`, 취소 직후 Q2 수락. chat-proxy 가 `/cancel` POST 를 그대로 통과시킴.
- **N-C5** 청크 분할 200회(CRLF, UTF-8 중간, `data:` 중간, 개행 없는 EOF 꼬리) 이벤트 순서와 본문 모두 일치. 탈출 시 pending cancel 확인. 프레임 원자성/`error_type` 계약은 기존 AST·파서 테스트와 일치(cancel 경로도 `_publish_error` 를 거침).
- **N7, N14** 실 DB 경계 모두 기대값. **N-UI1** 호출처 5곳 모두 서버 컴포넌트(`'use client'` 없음), 동일 키라 dedupe 성립. React 19.3 RSC `cache()`. 클라이언트에서 import 하는 곳 없음.
- **N-UI5** 위 기계 대조 결과 오염 없음. **N-C4** SELECT 만 하고 rollback 하므로 커밋될 트랜잭션 없음. **N-C9** 경로 파라미터 4곳 + `SqlExecuteRequest.session_id` 모두 `uuid.UUID`.
- **N-C10** FunctionError → sanitized 502, 그 외 invoke/파싱 실패 → 일반 502(원문은 로그에만). `ok:false` 는 의도대로 200. **N-C11** 실제 라우트 핸들러 실행, user/team/global·UUID 통과.
- **N-C8** post_message·`_persist` 의 UPDATE 는 SQL 증분이라 세션 경합에서 갱신이 유실되지 않음(실 DB `('Q', 2)`).

---

요약: M1 · S3(잔여 S1, S12, 회귀 S10) · N14. S3 수정은 로직이 맞지만(오라클 대조 0 불일치) 상관 누락 한 줄 때문에 앱 정책 화면과
쓰기 두 개가 운영 데이터에서 항상 500 이 남. 채팅 cancel/409 는 단일 pod 에서만 동작하고, 폴링 폴백은 다른 세션과 진행 중 전송을
덮어씀.

VERDICT: NOT CONVERGED
