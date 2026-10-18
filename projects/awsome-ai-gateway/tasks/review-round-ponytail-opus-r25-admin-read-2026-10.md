# R25 — admin-api 읽기 경로 + admin-ui 전체 리뷰 라운드 1 (kiro/opus, ponytail)

범위: admin-api `routers/{dashboard,productivity,my,monitoring,analytics}.py`, `models/keys/apps/users.py` GET,
`chat_agent.py` 전체, `services/{invocation_log,analytics,effective_policy}_service.py`, `core/usage_filters.py`,
(+ `routers/audit_reconcile.py`) / admin-ui `src/` 전체(app 페이지, components, lib, middleware, `/api` 프록시).
R13(분석 스코프)·R17·R21(쓰기 경로) 수정 항목은 재보고하지 않음.

## 이번 라운드에서 직접 실행한 검증

| 항목 | 결과 |
|---|---|
| admin-api `pytest tests/unit` | 538 passed (베이스라인) |
| admin-ui `vitest run` / `tsc --noEmit` | 441 passed / clean (베이스라인) |
| 임시 vitest(실제 `api-client`/`UsageTrendChart`/`ChatLayout` 렌더) | S6(INVALID_PATH, fetch 0회), S7(`details null`), S8(`TypeError: v.toFixed is not a function`), N-UI1·N-UI2 재현 |
| pydantic 직렬화 (`RateLimitConfigItem(cph=Decimal('5.00')).model_dump(mode='json')`) | `'cph': '5.00'` (문자열) — S8 근거 |
| sqlite 동일 술어 재현 (KPI) | `limit=100 used=960 utilization=960%` (정답 50%) — S1 근거 |
| 실제 `_agentcore_producer` 를 블로킹 body 로 돌리고 0.5s 후 cancel | `persisted rows: []` — S11 근거 |
| 실제 `reconcile()` 순수함수 호출 | claude-code native 행 `missing`, alias 필터 시 `orphan` 오탐 — S4 근거 |
| `period_to_utc_range('2026-08\n')`, `('٢٠٢٦-٠٨')` | 둘 다 8월 범위 반환 — N3 근거 |
| FastAPI 0.136 dependency exit 시점 측정 | body 종료 **후** exit — N-C4 근거 |
| botocore `iter_lines(chunk_size=1)` 벤치 | 300KB 라인 9.9s vs 기본 0.017s — N-C5 근거 |
| 코드 대조(직접 확인) | dashboard.py:457-527 ↔ batch_flusher.py:559-605, api-client.ts:82-146 ↔ main.py:431-444, permissions.ts:17 ↔ dashboard.py `require_admin` 7곳, monitoring.py:33/76/217 ↔ gateway streaming.py:76, chat_agent.py:426/467/859 ↔ values-prod `adminApi.replicaCount: 3`, apps.py:221-243 ↔ gateway auth_service.py:136-176 |

임시 파일(/tmp)은 모두 삭제. 리포지토리 코드는 수정하지 않음.

---

## 라운드 1 — NOT CONVERGED (M0 + S12 + N26)

멀티테넌트 누출은 발견되지 않음(아래 "기각" 참조). 대신 잘못된 숫자/화면 붕괴/채팅 내구성 문제가 S급으로 남음.

### Should-fix (S)

**S1. 대시보드 KPI 예산 사용률 과대 — 분자·분모 스코프 불일치 (100% 초과 가능)**
(`admin-api/src/app/routers/dashboard.py:457-490` 분모, `:500-527` 분자 / `cost-recorder-worker/src/worker/batch_flusher.py:573-605`)
- 분모는 **활성 총한도 config 가 있는** TEAM + 팀없는 USER 만 합산. 분자는 `budget_usages` 의 **모든** TEAM + 팀없는 USER 행을 합산.
  워커는 config 유무와 무관하게 모든 사용자/팀의 `budget_usages` 를 UPSERT 한다.
- 재현: 팀 A 한도 $100·사용 $50, 팀 B 한도 없음·사용 $900, 팀없는 사용자(한도 없음) $10
  → `/admin/dashboard/kpi` = `budget_used_usd=960, budget_limit_usd=100, budget_utilization_pct=960.0`. 정답 50%.
- 최소 수정: 분자 쿼리에 `EXISTS(SELECT 1 FROM budget.budget_configs bc WHERE bc.scope=bu.scope AND bc.scope_id=bu.scope_id AND bc.client IS NULL AND bc.is_active)` 추가.
  더불어 `:414-419` docstring(“usage_logs SUCCESS 사용”)이 R13 M5 이후 코드와 반대이므로 갱신(N5).

**S2. 분석 `group_by=user` — 실사용 501위 이하 + seed 보유 사용자의 실사용분 누락**
(`admin-api/src/app/services/analytics_service.py:297` `.limit(500)`, `:371-388`)
- seed 잔차는 전 사용자에 대해 계산하지만 실사용 병합은 SQL 상위 500행에만 일어난다. 나머지는 `cost_usd=seed, requests=0` 의 “seed only” 행으로 상위 50 재정렬에 들어간다.
- 재현: 활성 600명, 사용자 X 는 실사용 550위($40, 30건) + seed $500 → 차트/CSV 에 `$500, 0건`. 정답 `$540, 30건`.
- 최소 수정: `seeded` 에 남은 user_id 에 대해 같은 `user_where` + `UsageLog.user_id.in_(leftover)` 로 2차 집계 후 병합(또는 LIMIT 제거 후 Python top-50).

**S3. `GET /admin/apps/{client}` `allowed_users` 가 팀/조직 allowed-clients 를 무시 — 게이트웨이 집행·effective-policy 와 모순**
(`admin-api/src/app/routers/apps.py:221-243` / 대조: `gateway-proxy/src/app/services/auth_service.py:136-176`, `effective_policy_service.get_for_user`)
- 앱 정책 화면은 `user_allowed_clients` 만 본다. 게이트웨이는 user → team → org 순으로 폴백한다.
- 재현: 팀 T `team_allowed_clients={claude-code}`, 사용자 U(개인 행 없음) → `/admin/apps/cowork` 가 U 를 허용(`explicit=false`)으로 표시. 게이트웨이는 U 의 cowork 를 거부하고 `/admin/users/U/effective-policy` 도 차단으로 표시.
- 최소 수정: 사용자별 유효 allowed_clients 를 같은 우선순위(user 행 → team 행 → teams→departments→org 행)로 SQL 해석하고, NULL/빈 집합이거나 client 를 포함할 때만 포함.

**S4. invocation-log reconcile 오탐 — claude-code native 행이 항상 `missing`, 필터/절단 시 `orphan` 부풀림** (현재 API 전용, UI 미사용)
(`admin-api/src/app/routers/audit_reconcile.py:141-160`, `services/invocation_log_service.py:212-255`)
- usage 측은 전 provider 행인데 로그 그룹은 단일 계정·리전(us-east-2 런타임). claude-code BEDROCK native 행은 `bedrock_request_id` 를 갖지만(gateway `messages.py:1038`) 333 계정 ap-northeast-2 에서 실행돼 이 그룹에 절대 없다 → client 필터 없이 돌리면 claude-code 전 행이 “진짜 알람”인 `missing`.
  `model_alias`/`client` 필터는 usage 측에만 걸려 다른 alias/client 의 로그가 전부 orphan. `row_limit` 절단 시 `core_end` 를 마지막 행으로 당기지 않아 그 이후 로그도 orphan.
- 최소 수정: 해당 그룹이 캡처하는 provider/리전 행만 대상으로 하고 나머지는 `skipped_null` 류 사유(`other_region`)로 분류. 필터가 있으면 로그 측(modelId)에도 적용하거나 orphan 계산 억제. 절단 시 `core_end = 마지막 requested_at`.

**S5. 모니터링 에러율이 TIMEOUT 을 무시 — 타임아웃 폭주가 0% 로 표시**
(`admin-api/src/app/routers/monitoring.py:33,76,217` / UI `components/monitoring/MonitoringOverview.tsx:36-40`, `ModelHealthTable.tsx:70-76`)
- `func.count().filter(UsageLog.status == UsageStatus.ERROR)` 만 셈. gateway 는 실제로 `TIMEOUT` 을 기록(`gateway-proxy/src/app/services/streaming.py:76`). 같은 화면 EventLog “abnormal” 은 ERROR+TIMEOUT 을 센다(`monitoring.py:139-143`).
- 재현: Bedrock 정체로 지난 1h 실패가 전부 `STREAM_IDLE_TIMEOUT` → overview `error_rate_pct=0`, 모델 헬스 녹색.
- 최소 수정: 세 곳 모두 `UsageLog.status.in_([UsageStatus.ERROR, UsageStatus.TIMEOUT])`(필요 시 `timeout_count` 별도 노출).

**S6. `/cli` 페이지 다운로드 목록이 항상 ErrorState — api-client 의 `/admin/` 접두 가드가 차단**
(`admin-ui/src/app/cli/page.tsx:44-47` `adminAPI.get('/cli/downloads')` / `admin-ui/src/lib/api-client.ts:82-88` / `admin-api/src/app/routers/cli.py:25` `prefix="/cli"`)
- R17 경로 가드가 `/admin/` 외 경로를 네트워크 호출 전에 400 `INVALID_PATH` 로 거절. 백엔드 라우트는 `/cli/downloads`.
- 재현: ADMIN/TEAM_LEADER 가 `/cli` 진입 → 어느 OS 버튼도 렌더되지 않음. 임시 vitest: `400 INVALID_PATH`, fetch 0회.
- 최소 수정: 가드에 명시 허용 목록(`/admin/` 접두 + 정확히 `/cli/downloads`) 추가, 또는 이 조회를 전용 fetch 로 분리. CLI 페이지 렌더 회귀 테스트 추가.

**S7. 409 `confirmation_required` 의 영향 목록(`error.details.warnings`)이 항상 유실 — ConfirmImpactBox 가 누가 차단되는지 표시 못 함**
(`admin-api/src/app/main.py:431-444` `{"error": {..., "details": exc.details}}` / `admin-ui/src/lib/api-client.ts:125,141-146` 은 top-level `detail` 만 파싱 /
`lib/actions/budgets.ts:253-254`, `lib/actions/users.ts:362-363` → `components/budgets/ConfirmImpactBox.tsx:52-53` `details?.warnings ?? []`)
- 재현: 팀 기본 cap 을 멤버 현재 사용액보다 낮게 저장(budget_service.py:483 등 8개 raise 지점) → 다이얼로그는 일반 문장만 표시, 영향받는 사용자/금액 목록 없음. 관리자는 영향 없이 “즉시 차단” 변경을 확정한다. 임시 vitest: `status 409, code confirmation_required, details null`.
- 최소 수정: `const details = errorBody.error?.details ?? detailText;` 로 `APIError` 에 전달(타입에 `error.details` 추가) + `err.details.warnings` 단위 테스트.

**S8. `/users` 레이트리밋 추이 차트가 CPH 한도 설정 시 TypeError 로 패널 붕괴**
(`admin-api/src/app/schemas/rate_limits.py:42-43` `cph: Decimal` → JSON `"5.00"` / `admin-ui/src/components/users/ScopeRateLimitPanel.tsx:360-366` → `components/rate-limits/UsageTrendChart.tsx:78-79,210-221`)
- `"5.00" > 0` 은 강제변환으로 true, 이어서 `fmtValue("5.00")` → `"5.00".toFixed` TypeError. 라우트 에러 바운더리까지 올라가 `/users` 상세 패널 전체가 죽는다(RPM/TPM 은 int 라 무관).
- 재현: 사용자/팀에 CPH 한도 설정 → `/users` 해당 노드 Rate-limit 섹션 → CPH 탭 클릭. 임시 vitest 로 `TypeError: v.toFixed is not a function` 재현.
- 최소 수정: `getRateLimitStatusAction` 에서 `own/inherited` 의 `cpm/cph` 를 `numOrNull` 로 정규화(+ 차트에서 `Number(limits[metric])` 방어).

**S9. TEAM_LEADER 랜딩(대시보드 `/`)이 거의 전부 에러 — 페이지는 허용, 백엔드는 전부 admin 전용**
(`admin-ui/src/lib/permissions.ts:17` `'/': [ADMIN, TEAM_LEADER]`, `app/api/auth/callback/route.ts:434` 랜딩 /
`admin-api/src/app/routers/dashboard.py:85,145,219,271,337,393,566` 전부 `Depends(require_admin)`, `/admin/users/teams` 도 require_admin)
- 재현: TEAM_LEADER 로그인 → KPI 8장 '—'+fetch failed, 모델/클라이언트 도넛·랭킹 ErrorState. 비용 추이 카드만 동작(`/admin/analytics` 는 TL 허용). `app/page.tsx` 에 역할 분기 없음.
- 최소 수정(권한 확대 없이): `PAGE_PERMISSIONS['/']`·Sidebar 대시보드 항목에서 TEAM_LEADER 제거 → 콜백이 `/my` 로 랜딩. (대시보드 TL 스코핑은 dashboard 캐시 키에 actor 가 없어 별도 과제.)

**S10. chat §56 핸드오프 릴레이가 프로세스 로컬인데 prod admin-api 는 3~10 pod — 재구독이 ~2/3 확률 404, UI 는 조용히 무시**
(`admin-api/src/app/routers/chat_agent.py:304,426,467-469` / `deployment/charts/llm-gateway/values-eks-fargate-prod.yaml:279-280` `replicaCount: 3`(HPA 3~10), Service 에 `sessionAffinity` 없음 /
`admin-ui/src/components/chat/useChatStream.ts:153` `if (response.status === 404) return;`)
- R17 은 “admin-api replica=1” 전제로 수용했으나 prod 는 그 전제가 거짓(dev 도 HPA 1~3 이라 스케일 시 동일).
- 재현: deep 질문 → 다른 메뉴 → 분석 중 복귀. history 는 질문만, reattach 는 다른 pod 로 가 404 → 답도 스피너도 없음. 사용자가 재질문하면 deep 분석이 이중 실행(Bedrock/AgentCore 이중 비용).
- 최소 수정: (a) chat 전용 단일 replica Deployment/Service 로 chat-proxy 라우팅, 또는 (b) reattach 404 이면서 history 마지막이 `user` 면 assistant 행이 생길 때까지 history 폴링. 정석은 Redis Streams 릴레이.

**S11. pod 종료(롤아웃/HPA 스케일인) 시 진행 중 분석의 부분 답변이 영속화되지 않음**
(`admin-api/src/app/routers/chat_agent.py:859` `except Exception`, `:308-314` `_bg_tasks` 를 lifespan 이 drain 하지 않음)
- 루프 종료 시 남은 태스크는 cancel 되고 `CancelledError`(BaseException)는 `except Exception` 에 안 잡혀 `_persist` 가 안 돈다. `finally` 는 `relay.finish()` 만.
- 재현: deep 분석 중 `kubectl rollout restart deploy/<admin-api>` → 새로고침 시 질문만 남고 assistant 턴(부분 텍스트·`[오류]` 노트)이 없음. 실제 producer 시뮬레이션: `persisted rows: []`.
- 최소 수정: `except asyncio.CancelledError: await asyncio.shield(_persist(error_note="서버 재시작으로 분석이 중단되었습니다")); raise` + lifespan shutdown 에서 `await asyncio.wait(_bg_tasks, timeout=N)` (grace ≥ N).

**S12. 채팅 Stop 은 브라우저 스트림만 닫음 — 서버 분석은 끝까지 과금, 다음 질문이 그 뒤에 줄 서고 history 순서가 뒤섞임**
(`admin-ui/src/components/chat/useChatStream.ts:131-133`, `admin-api/src/app/routers/chat_agent.py:422-432`(cancel 엔드포인트 없음), `:620-624`(완료 시점 INSERT, `created_at DEFAULT now()`), `admin-chat-agent/src/agent/main.py:1471` `_ORCH_LOCK`)
- 재현: Q1(deep) 5초 후 Stop → Q2 전송. Q2 는 같은 `runtimeSessionId` 에서 Q1 오케스트레이터가 끝날 때까지 `thinking` 만 표시. Q1 답이 T3>T2 에 저장 → 새로고침 시 `Q1, Q2, A1, A2`. 취소한 Q1 도 Opus 토큰을 끝까지 소비.
- 최소 수정: 소유자 검증된 `POST /admin/chat/sessions/{id}/cancel` 로 producer task 취소 + `[취소됨]` 부분 영속화(session_id→task 맵), `cancel()` 이 이를 호출. 최소한 진행 중 릴레이가 있으면 `post_message` 를 409 로 거절.

### Nits (N)

백엔드 읽기 경로
- **N1.** `/admin/monitoring/models` `status` 하드코딩 `"ACTIVE"`(`monitoring.py:92`) — INACTIVE 모델도 ACTIVE 배지(`ModelHealthTable.tsx:65`). `ModelAlias.status` outer join. `/overview` total_cost 는 ERROR/TIMEOUT 포함이라 §59 SUCCESS 기준과 다름 — 필터하거나 라벨링.
- **N2.** period/day 정규식이 `$`·`\d` 사용(`core/usage_filters.py:70,181`, `analytics_service.py:37-38`) — `'2026-08\n'`·아랍-인도 숫자 허용(실행 확인). `/kpi?period=2026-08%0A` 는 사용 카드는 채우고 `budget_used_usd=0`, 캐시 키 분열. `re.fullmatch(r"[0-9]{4}-[0-9]{2}")`.
- **N3.** 잘못된 cursor 가 500(`key_service.py:708`, `user_team_service.py:634` `uuid.UUID(cursor)`) — ValueError → 400.
- **N4.** `/admin/dashboard/model-share` 잘못된 `team_id` 에 200 + `error` 필드(`dashboard.py:166-175`) — 400 으로.
- **N5.** `dashboard_kpi` docstring(`dashboard.py:414-419`)이 코드(budget_usages 사용)와 반대 — 되돌리기 위험. 갱신.
- **N6.** top-N 동률 tiebreak 없음(`dashboard.py:307,366`), `by_team` ORDER BY 없음(`analytics_service.py:253`) — `, id` 보조 정렬.
- **N7.** `/admin/keys/count` 가 TL 의 리드 팀(`teams.leader_user_id`)을 무시(`keys.py:84-88`) — 과소집계(누출 아님).
- **N8.** 팀 귀속 기준 불일치: `/usage/by-user*` 는 사용자 **현재** 팀(`analytics_service.py:665,753`), top-teams/by_team 은 요청 시점 `usage_logs.team_id` — 월중 이동 시 롤업 불일치.
- **N9.** 계약: analytics 금액은 Decimal→JSON 문자열(실행 확인 `"total_cost_usd": "1.234567"`), `avg_cost_per_user_usd` 미반올림. UI 타입은 `number`(`lib/actions/dashboard.ts:184-210`, `trendSeries.ts:21` 등). 현재 소비처는 전부 `Number()` 로 감싸 정상이나 `fmtUsd` 직사용 시 `$0.00`. 타입을 `string` 으로 하거나 fetch 헬퍼에서 1회 정규화.
- **N10.** `/admin/dashboard/periods` 가 매 로드마다 `usage_logs` 전체 DISTINCT 스캔·무캐시(`dashboard.py:579`) — TTL 캐시 또는 min/max 기반.
- **N11.** `pricing/sync-preview?source=<오타>` 가 조용히 AWS 로 폴백(`models.py:41`) — `Literal["aws","litellm"]`.
- **N12.** 같은 대시보드에서 30s 캐시(summary/kpi/model-share)와 무캐시(top-users/top-teams/client-share) 혼재 — 최대 30s 합계 불일치. 수용 또는 문서화.
- **N13.** `code_acceptance_rate_pct` 는 라인이 아닌 이벤트 수 비율, `cost_per_generated_line` 은 전사 비용 / 계측 사용자 라인(`productivity.py:278`) — 의도 확인 필요(품질 메모).
- **N14.** budgets summary 의 Redis miss 폴백이 usage_logs SUCCESS-only(`budget_service.py:1703-1735`) — R13 M5 와 같은 불일치가 `/budgets`·대시보드 랭킹 `usagePct` 에 잔존. `BudgetUsage(client IS NULL)` 폴백.

admin-ui 페이지/계약
- **N-UI1.** `/analytics` 가 화면당 동일 `/admin/analytics` 5회 호출 — `api-client.ts:95-97` 이 항상 `signal` 을 넘겨 Next fetch dedupe 가 꺼짐(`next/dist/server/lib/dedupe-fetch.js:81-89`). `analyticsQuery.ts:9-13` 의 “dedup” 주석은 R17-S6 이후 거짓. React `cache()` 래퍼.
- **N-UI2.** `/my` 개인 한도 $0 이면 `usage_pct=null`(`my.py:87`) → `MyBudgetCard.tsx:151-176` 이 `0.0%` NORMAL 표시(차단 사용자에게 정상 게이지). `BudgetSummaryTable.tsx:212` 처럼 `limit===0` 특수 처리.
- **N-UI3.** KPI 사용률 카드가 한도 0(contract 상 null)일 때 “fetch failed” 표시(`app/page.tsx:122`) — `!kpi` 와 `null` 구분.
- **N-UI4.** 대시보드 client 필터가 KPI 드릴다운 href 에서 유실(`page.tsx:114,130,141,157,164`), 랭킹의 `usagePct` 는 비필터 `/admin/budgets/summary`(`page.tsx:232`) — href 에 client 전달, 컬럼 라벨 명시.
- **N-UI5.** 감사 로그가 남는 PUT 이 `withRetry` 로 감싸짐(`budgets.ts:51,114,152,176`, `rate-limits.ts:31`, `models.ts:418`, `users.ts:35,62`) — 커밋 후 연결 리셋/503 재시도 시 감사 행 중복. `settings.ts:45-49` 는 의도적으로 회피. 재시도 제거 또는 idempotency key.
- **N-UI6.** 소소: `ModelCostDetail.tsx:109-110` <1% 세그먼트 누락으로 바 미충전, `MonitoringOverview` `error_rate_pct` 미포맷, `ClientShareDonutClient` 0 합계 빈 상태 없음, `syncCognitoAction` 이 15s `UPSTREAM_TIMEOUT_MS` 에 묶여 대형 풀에서 백엔드는 진행 중인데 실패 토스트.

chat / 프록시 / 인증
- **N-C1.** `useChatStream` 의 중단된 호출 `finally` 가 새 스트림의 `isStreaming`/`abortRef` 를 지움(`useChatStream.ts:123-126,183-186`) — reattach 대기 중 전송하면 Stop 버튼 없음·`cancel()` no-op·동시 전송 가능. 실제 ChatLayout vitest 로 재현(`stop button present: false`). `if (abortRef.current === controller)` 가드.
- **N-C2.** 백엔드가 항상 `error` → `done` 을 보내 UI 가 `done` 에 빈 assistant 버블을 하나 더 생성(`chat_agent.py:700-711,842-855` ↔ `ChatLayout.tsx:79,457-466`). vitest 재현. 마지막이 non-pending assistant 면 in-place 적용.
- **N-C3.** `/chat` 이탈 시 SSE fetch 미중단(언마운트 cleanup 없음) — 복귀마다 reattach 누적, dev(HTTP/1.1) 에서 6연결 고갈로 UI 정지 가능(브라우저 사실, 라이브 미재현). 언마운트 abort.
- **N-C4.** `reattach_stream` 의 소유자 SELECT 가 autobegin 한 트랜잭션을 스트림 종료까지 유지(`chat_agent.py:455-462`, CommittingRoute 는 StreamingResponse 커밋 생략) — pool 슬롯 1개 idle-in-transaction 고정. 반환 전 `commit()`/`rollback()`.
- **N-C5.** `iter_lines(chunk_size=1)`(`chat_agent.py:667`) O(L²) — 300KB 라인 9.9s. 큰 tool_result 시 단일 워커 정지. 청크 읽기 + 자체 `\n` 분할.
- **N-C6.** 마운트 시 `getHistory` 실패(5xx/401)면 저장 세션 id 삭제(`ChatLayout.tsx:62-71`), `listSessions` UI 없음 → 이전 대화 영구 접근 불가, 401 은 로그인 리다이렉트도 안 됨. 404/403 에서만 삭제, 401 은 리다이렉트.
- **N-C7.** `done` 없는 EOF: reattach 경로는 스피너 영구, send 경로는 부분 답변이 완결처럼 보임. `sawDone` 추적 후 합성 error.
- **N-C8.** `chat_agent.sessions.title/message_count/updated_at` 를 갱신하는 코드 없음 — `list_sessions` 가 의미 없는 값 반환.
- **N-C9.** 잘못된 `session_id` 가 500(`CAST(:sid AS uuid)` 5곳) — 파라미터를 `uuid.UUID` 로 타입 지정(422).
- **N-C10.** `/sql/execute` 가 Lambda `FunctionError` 페이로드를 200 으로 통과, 502 메시지에 raw boto 예외(ARN) 포함(`chat_agent.py:933,938`). 매핑·일반화.
- **N-C11.** rate-limits BFF(`app/api/rate-limits/usage/route.ts`, `usage-trend/route.ts`)가 `..` 세그먼트 미거절(`encodeURIComponent('..')==='..'` → `/admin/`). 본인 쿠키·GET 뿐이라 권한상승 없음. `cli-download` 처럼 거절.
- **N-C12.** 소소: `ReportCard.tsx:41` await 후 `window.open`(Safari 팝업 차단 가능, 미검증), 서비스 토큰 전부 동일 synthetic user 로 chat 세션 네임스페이스 공유, 콜백에서 whoami 실패 시 이전 로그인의 `admin_role` 쿠키 잔존(UI 게이팅만 영향).

### 검토 후 기각(오탐)

- 멀티테넌트: analytics 캐시는 `ADMIN && scope=="all"` 에서만(`analytics:global:` 키), dashboard 캐시는 전 라우트 `require_admin` 이라 actor 미포함이 안전. TL `_resolve_scope` default-deny(R13) 유지, `by_user` seed 쿼리 team 스코프, `month_actual` 은 차감값으로만 사용. `/my/*` 는 user_id 파라미터 없음.
- chat: 타 admin 세션 history/messages/stream/sql 은 403, 비관리자는 8개 엔드포인트 모두 `require_admin`. chat-proxy 세그먼트 검사·`/admin/chat/` 접두 재검증·`admin_jwt` 만 전달·응답 헤더 재구성·64KB 바디 상한. SSE 파서는 분할 프레임·multi-line data·tail flush·4MiB 상한 처리(CRLF 는 admin-api 가 `\n` 만 내보내 도달 불가). `post_message` 는 스트리밍 전 commit.
- 인증: login/callback 에 `next`/`callbackUrl` 없음(open redirect 없음), dev-login 3중 가드, state+PKCE S256+nonce, mutating Origin/Referer 검사, `NEXT_PUBLIC_*` 비밀 없음·client 컴포넌트의 server-only import 없음. `admin_role` 쿠키 위조는 UI 셸만(API 가 매 요청 재인가).
- 정합: KST 경계는 gateway→worker→읽기 경로 일관, cursor float 정밀도(20만 샘플 0 불일치), 스코프당 활성 config 중복은 partial unique index 로 불가, `usage_logs.team_id` NOT NULL, `/users/search` 라우트 순서 정상, keys/users 페이지네이션 파라미터·EffectivePolicy 필드·budget Decimal 파싱 일치, ModelShare/OrgSearchBox stale 가드 존재.

VERDICT: NOT CONVERGED

---

## 라운드 1 — 처리 내역 (2026-10-17, 이후 kiro 라운드 2 검증 대상)

### S — 전부 수정 완료

| ID | 수정 |
|---|---|
| S1 | 대시보드 budget utilization 분자/분모 스코프 정합 — `budget_usages` 행 중 활성 total budget 설정이 있는 스코프만 합산하도록 분모 필터 일치(`dashboard.py`). |
| S2 | `group_by=user` 실사용 쿼리의 `LIMIT 500` 제거 — top-N 절단 후 seed 병합이 일어나면 N위 밑 사용자가 "seed only" 틀린 행이 되던 것을 전건 집계로 수정(`analytics_service.py`). |
| S3 | `apps.py` `allowed_users` 를 user→team→org allowed-clients 우선순위와 동일한 SQL 조인으로 미러링 — 유저 allowed_clients 만 보던 것을 수정. |
| S4 | `audit_reconcile` — Claude Code native(BEDROCK) 레코드는 다른 계정/리전에서 실행돼 설정된 로그 그룹에 없으므로 provider·account·region 기준으로 대상 필터/절단 반영. |
| S5 | monitoring error-rate 계산에 TIMEOUT 포함 — 에러율 과소 표시 수정(`monitoring.py`). |
| S6 | api-client 경로 가드에 `/cli/downloads` 계열 허용 — CLI 다운로드가 BFF 경로 검사에 걸리던 것 수정. |
| S7 | `APIError`가 `error.details`(confirmation_required warning 포함)를 보존 — 관리 액션의 확인 페이로드가 유실되던 것 수정(`api-client.ts`). |
| S8 | rate-limit `cpm`/`cph` Decimal→문자열 wire 값을 경계에서 숫자 정규화(`numOrNull`) + 차트측 방어 — `v.toFixed` TypeError 수정(`rate-limits.ts`, `UsageTrendChart.tsx`). |
| S9 | TEAM_LEADER 랜딩을 `/` → `/my` 로 — `/` 는 하부 API 전부 `require_admin` 이라 ADMIN-only 가 맞음(403 으로만 채워지던 대시보드 진입 수정). `permissions.ts`/`Sidebar`/`auth` 콜백 + 스테일 테스트 3파일 갱신. |
| S10 | reattach 404(프로세스 로컬 릴레이, 다른 파드) 시 history 폴링 폴백 — 영구 스피너 수정(`useChatStream.ts`, `ChatLayout.tsx`). |
| S11 | `except asyncio.CancelledError` 명시 + `asyncio.shield(_persist(...))` 로 파드 종료/롤아웃 시 부분 답변·`[중단]` 노트 영속화 + lifespan `drain_chat_bg_tasks` 추가. |
| S12 | `POST /admin/chat/sessions/{id}/cancel` 서버측 취소 + `_active_tasks`/`_cancelled_sessions` 추적 + 활성 분석 중 `post_message` 409 — Stop 이 브라우저만 닫고 과금 계속되던 것 수정. |

### N — 처리한 항목

- N1: `/admin/monitoring/models` `status` 를 `ModelAlias` 조인으로 실값 반영.
- N2: period/date 검증을 `[0-9]`+`fullmatch`+달력 검증으로(`usage_filters.py`, `analytics_service.py`) — `\n`·아랍숫자 통과 차단.
- N3: 잘못된 cursor `uuid.UUID()` ValueError → 400(`key_service.py`, `user_team_service.py`).
- N4: model-share 잘못된 `team_id` 200+error 필드 → 400(`dashboard.py`).
- N5: `dashboard_kpi` docstring 을 실제 구현(budget_usages)에 맞게 정정.
- N6: top-N 동률 tiebreak `, id` + `by_team` ORDER BY 결정화.
- N7: `/admin/keys/count` 가 TL 의 리드 팀 집합(`teams.leader_user_id`)을 스코프로 사용(`team_ids` 파라미터 추가).
- N9: analytics 응답 숫자 필드를 `analyticsQuery.ts` 에서 1회 정규화 — Decimal 문자열이 그대로 차트에 가던 것 차단.
- N10: `/admin/dashboard/periods` DISTINCT 스캔에 TTL 캐시.
- N11: `pricing/sync-preview?source` 를 `Literal["aws","litellm"]` 로 — 오타 시 AWS 조용한 폴백 차단.
- N13: `code_acceptance_rate_pct` 를 라인 단위로(구형 0-라인 데이터는 이벤트 수 폴백), `cost_per_generated_line`/`cost_per_commit` 분자를 계측 코호트(prod∪git 이벤트 사용자) 비용으로 — 전사 비용/계측 라인 불일치 수정(`productivity.py` + 회귀 테스트 `test_r25_productivity_metrics.py`).
- N14: budgets summary Redis-miss 폴백을 `usage_logs` 재집계 → `budget_usages` 조회로 — 분모 의미론 불일치(R13-M5 잔여) 수정(`budget_service.py`).
- N-UI1: `fetchAnalyticsOnce` = React `cache()` 래퍼로 `/admin/analytics` 5중 호출 dedupe(`analyticsQuery.ts`) + 5개 호출처 교체.
- N-UI2: `/my` 한도 $0 일 때 게이지를 NORMAL 0% 가 아닌 "한도 없음/차단" 구분 표시(`MyBudgetCard.tsx`).
- N-UI3: KPI 카드가 한도 0(contract null)을 "fetch failed" 로 표시하던 것 구분(`app/page.tsx`).
- N-UI4: client 필터를 KPI 드릴다운 href 에 전파 + 랭킹 `budgetSpend` 컬럼에 "전체 앱 기준" 툴팁(`page.tsx`, `TopSpendTable.tsx`).
- N-UI5: 감사 로그가 남는 PUT/POST/PATCH/DELETE 에 `withRetry` 제거 — budgets/models/users/routing/keys/apps/rate-limits 의 뮤테이션 액션 전수. 잔여 `withRetry` 38곳은 전부 read-only 확인(스크립트 대조).
  - ⚠️ 변환 스크립트가 `withRetry(() => adminAPI.delete(...))` 한 줄 표현을 `await (url)` 로 오염시킨 사례 4건(users 3 + rate-limits 1)과 `put`→`delete` 오복원 1건(`setUserClientBudgetAction`)을 메서드 카운트 대조로 발견·복원. "빈 배열 → 전체 삭제" 의미론이 조용히 사라질 뻔함.
- N-C1: `useChatStream` `finally` 에 `abortRef.current === controller` 가드 — 중단된 호출이 새 스트림 상태를 지우던 것 수정.
- N-C2: `done` 후 기존 pending assistant 메시지를 in-place 갱신 — 유령 버블 제거.
- N-C3: `/chat` 언마운트 시 SSE abort cleanup 추가 — 재진입마다 reattach 누적 방지.
- N-C4: `reattach_stream` 반환 전 `session.rollback()` — 스트림 수명 동안 autobegin 트랜잭션+pool 슬롯 점유 수정.
- N-C5: `iter_lines(chunk_size=1)` O(L²) → `iter_chunks` + 자체 `\n` 분할 + EOF 꼬리 처리 + 탈출 시 prefetch future 정리. 회귀 하네스에 두 경로 모두 커버.
- N-C6: `getHistory` 실패 시 404/403 에서만 저장 세션 삭제, 401 은 로그인 리다이렉트, 나머지는 세션 유지+복구 가능 오류(`ChatLayout.tsx`, i18n 키 추가).
- N-C7: `done` 미수신 EOF 를 합성 error/truncation 으로 표면화 — 부분 답변이 완결처럼 보이던 것 수정.
- N-C8: `chat_agent.sessions.title/message_count/updated_at` 를 `_persist` 에서 함께 갱신.
- N-C9: `session_id` 파라미터를 `uuid.UUID` 타입으로 — `CAST` 500 대신 FastAPI 422(`SqlExecuteRequest` 등 5곳).
- N-C10: Lambda `FunctionError`(HTTP 200 래핑) 감지 → sanitized 502, boto 예외 텍스트(ARN 포함) 비노출(`_LambdaFunctionError` + 'query execution failed').
- N-C11: rate-limits BFF 두 라우트에 `SAFE_SEGMENT` 문자셋 거절 + 회귀 테스트 `rateLimitsBffTraversal.test.ts` 12건.

### 미처리 (의도적 보류/수용)

- N8: 팀 귀속 기준 — by-user 는 사용자 현재 팀(조직 관리 뷰), top-teams/by_team 은 요청 시점 `usage_logs.team_id`(비용 귀속). 월중 이동 시 합계 불일치는 의미론 차이로 수용 — 두 기준을 섞으면 오히려 팀 롤업과 사용자 행이 어긋난다.
- N12: 30s 캐시/무캐시 혼재 — 위젯 간 최대 30s 합계 불일치는 새로고침 주기 내 허용으로 수용.
- N-UI6 / N-C12 소소 항목: 별도 라운드 유보(미검증·영향 미미).

### 검증

- admin-api: **1005 passed**, 50 skipped.
- admin-ui vitest: **453 passed** (39 files).
- admin-ui tsc: clean. lint: 기존 경고만.
- admin-ui `next build`: 성공.
- 회귀 테스트 신규: `test_r25_productivity_metrics.py`(3), `rateLimitsBffTraversal.test.ts`(12), 채팅 SSE 하네스 확장(iter_chunks/분할 프레임/EOF tail/고아 future).


---

## 라운드 2 — kiro/opus 검증 결과 (tasks/...-round2-result.md)

실 Postgres + 실 uvicorn + 실 ChatLayout vitest 로 실행 검증. 결과: **M1 + S3 + N14** → NOT CONVERGED.

## 라운드 2 — 처리 내역

### M — 수정 완료

- **M1**: `apps.py` `team_org_id` 스칼라 서브쿼리에 `.correlate(User)` 추가 — exists() 중첩 시 자동 상관이 바깥 EXISTS 까지만 닿아 `FROM teams JOIN departments, users` 로 users 가 FROM 에 다시 들어가 같은 팀 사용자 2명+ 에서 `CardinalityViolationError`(500) → `/admin/apps/{client}` 3개 엔드포인트 전부 장애. 회귀 테스트: 컴파일된 SQL 의 dept 조인 이후 영역에 `, auth.users` 콤마-조인이 없음을 단정(변이 실측 — correlate 제거 시 실패 확인).

### S — 잔여 처리 완료

- **S1-잔여**: dashboard 예산 분모 EXISTS 에 `max_budget_usd.isnot(None)` 추가 — D-cap(기본 하향)만 저장된 팀 config 는 한도 NULL 이라 분모에서 빠지는데 분자 사용액은 들어가 950% 부풀기 수정(실 DB 재현값 → 50% 정답).
- **S12-잔여**: (a) 로컬 클레임을 동기 삽입으로 옮겨 TOCTOU 닫음 + (b) Redis `SET chat:active:{sid} <token> NX EX` 크로스파드 클레임 + `/cancel` 은 `chat:cancel:{sid}` 플래그를 세워 소유 파드 producer 가 청크 경계(~1s)에서 읽고 스스로 취소(상류 `body.close()` + `stop_runtime_session` 으로 과금 전파). 409 판정은 소유권 확인 뒤로 이동(타인 세션 진행 여부 누설 차단).
  - ⚠️ 재배치 중 발견한 회귀 자체수정: 로컬 클레임 409 가 try 안으로 들어가며 except 가 첫 요청의 릴레이를 pop 하던 문제 — 해제를 `relay is not None and _active_relays.get(sid) is relay` 소유자 검증으로 수정 + 회귀 테스트.
- **S10-회귀**: 폴링 폴백 상태 머신 정비 — (a) 마지막 메시지가 user 일 때만 폴링 시작, (b) 폴링 중 pending 버블 표시, (c) handleSubmit/handleNewChat/언마운트에서 clearInterval, (d) tick 응답이 `sid !== sessionIdRef.current` 이면 폐기(옛 세션 덮어쓰기 차단), (e) 상한을 서버 read_timeout(900s)보다 긴 15분 + 타임아웃 안내, (f) reattach `'failed'`(5xx) 도 폴링 폴백.

### N — 처리한 항목

- N-a: 정상 경로 persist 도 `_spawn`+`asyncio.shield` 로 보호 + `persisted` 플래그를 commit 뒤에 — drain/취소가 write 도중에 와도 완결 답변이 '중단' 재기록/유실되지 않음.
- N-b: CancelledError 핸들러가 새 persist 를 시작하지 않고 진행 중 `persist_task` 를 await(이중 INSERT 창 제거) + drain 은 진입 스냅샷만 cancel 하고 `_bg_tasks` 가 빌 때까지 대기 루프(새 persist 는 재취소하지 않음 — shield 보호 유지).
- N-c: drain 타임아웃 로그의 structlog 식 kwargs 제거(stdlib `task=%s`) — TypeError 재현 확인됨.
- N-d: grace 예산 정합 — `adminApi.terminationGracePeriodSeconds` 30→50(preStop10+uvicorn10+drain15+audit10+여유5), Dockerfile CMD 에 `--timeout-graceful-shutdown ${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-10}` 추가(열린 SSE 가 lifespan 진입을 무한 지연시키던 것), main.py 주석 동기화. **readinessProbe 들여쓰기 오염(fields 가 httpGet 안으로) 발견·복구. WORKERS env 는 2→1 — Dockerfile 이 --workers 1 고정이라 오인 방지.**
- N-e: 취소/finally 경로에 `body.close()` + 사용자 취소 시 `stop_runtime_session` — 상류 과금 전파.
- N-f: provider 필터를 null-id 이유 분류 뒤로 이동(id 있는 행에만 적용) — Mantle/OPENMODEL 의 `other_region` 오분류 수정 + captured_providers 는 설정값으로.
- N-g: 403 페이지/dev-login 역할별 홈 — `role !== ADMIN` 이면 `/my`.
- N-h: 지표별 코호트 분리 — total_commits(표시)와 cohort_commits(분모) 분리, cost_per_line/commit 분자를 각 코호트 비용으로.
- N-i: `budget_service._PERIOD_RE` `[0-9]`+fullmatch + seed import 경로 동일 적용.
- N-j: analytics client 드릴다운 — 화면에 필터 칩 표시(해제 링크) + ExportButton 이 client 전달(백엔드/BFF 는 이미 전달 지원).
- N-k: 409 를 `AnalysisInProgressError` typed error 로 — i18n 메시지(원문 `HTTP 409: {json}` 노출 차단). 서버는 소유 확인 후 409 판정.
- N-l: reattach `'failed'` 시 폴링 폴백.
- N-n: budget_service 로그 hint `usage_logs`→`budget_usages` 정정, `numOrNull("")` 가 0 을 반환하던 것 null 로(한도 미설정→$0 오인 방지).
- N-11a(세션 삭제 목록): history 실패 시 저장 세션 삭제 대상에 422 추가(형식 정책 변경 대비).

### 회귀 테스트 추가

- `test_r25_round2_admin_read_fixes.py`(5): M1 컴파일 SQL 상관 단정(변이 실측), S1 분모 필터, drain-persist 대기, drain 타임아웃 상한, 409 가 타인 릴레이를 뜯지 않음.
- `test_r25_productivity_metrics.py`(3) 재구성 — 라인 기반 수용률, 코호트별 비용 분자, cohort_commits 분모.
- `rateLimitsBffTraversal.test.ts`(11) 재구성 — 문자셋 거절·누락·전달·401/fail-soft·window 정규화.

### 검증

- admin-api pytest 전체: **1009 passed**, 50 skipped.
- admin-ui vitest: 38 files — traversal suite 포함 전부 통과. tsc clean.
- 차트 안전 테스트: **51 passed**(grace 예산/probe 렌더 포함).
- 스테일 테스트 수정: `test_chat_agent_session_expiry.py` 에 request 인자(redis=None degraded) 추가.

VERDICT: 라운드 3 검증 대기

## 라운드 3 — kiro/opus 검증 결과 (tasks/...-round3-result.md)

NOT CONVERGED — S3 + N7. 실 uvicorn 2프로세스(=pod2) 하네스 + 실 PG/Redis + SIGTERM/SIGKILL 실측으로
라운드 2 주장 중 "drain 이 persist 를 안 다치게 한다" 가 **틀렸음** 이 드러남.

- **R3-S1**: `rateLimitsBffTraversal.test.ts` 재작성분이 `tsc --noEmit`(CI 하드 게이트)에서
  TS2345 — vitest/build 는 통과해 드러나지 않았음. `as Record<string,string>[]` 명시로 해소.
- **R3-S2**: `drain_chat_bg_tasks` 가 진입 스냅샷 전체를 cancel → (a) 이미 진행 중인 persist
  태스크 재취소로 완결 답변 유실, (b) 사용자 Stop 처리 중인 producer 재취소로 두 번째
  CancelledError 가 `except Exception` 을 뚫고 persist 없이 빠짐. 실 SIGTERM 재현 3건 모두
  assistant 행 없음. 수정: persist 는 `_persist_tasks`(`_spawn_persist`)로 분리 추적해 cancel
  제외 + `cancelling() > 0` 인 태스크는 건너뜀.
- **R3-S3**(신규 회귀): 크로스파드 폴링 중 전송 → stopPolling 후 서버 409 → 유령
  user+pending 쌍이 남고 폴링 재개 안 됨 → Q1 답 새로고침 전까지 미표시. 수정: 409 분기에서
  `prev.slice(0,-2)` 로 유령 쌍 제거 + `wasPolling` 이면 `pollForAnswer` 재개(N5 동시 해소).

### 라운드 3 — 처리 내역 (N)

- R3-N1: 정상 persist(`persist_task` 존재) 중 Stop 시 `error` 프레임·`stop_runtime_session`
  생략하고 그 태스크 완주만 기다림 — 라이브/DB 어긋남 해소.
- R3-N2: 클레임 TTL 960→120s + producer 가 60s 마다 `GET==token → EXPIRE`(Lua)로 갱신.
  소유 파드 SIGKILL 시 세션 잠금이 16분→최대 2분으로.
- R3-N3: 청크 대기 `wait_for` 상한을 `_CANCEL_CHECK_SECS`(1s)로 — 크로스파드 취소 지연
  최대 10s→~1s.
- R3-N4: `/cancel` 응답 의미("취소 신호 기록됨") 주석 명시.
- R3-N6: budget summary TEAM_LEADER 스코프 확장(소속+리드 팀)은 analytics/keys-count 와
  동일 규칙으로 **의도된 변경** — 본 처리 내역에 명시 기록.
- R3-N7: 차트 grace 테스트 산식에 audit flush(10s) 포함 — `grace >= preStop+graceful+drain+audit+5`.
- 회귀 테스트: `test_r25_round2_admin_read_fixes.py` 에 drain 케이스 2건 추가
  (진입 전 이미 진행 중인 persist 보호, `cancelling()==1` producer 재취소 금지) — 총 7건.

### 검증

- admin-api pytest 전체: **1012 passed**, 50 skipped.
- admin-ui vitest: **452 passed**, `tsc --noEmit` clean.
- 차트 테스트: **51 passed**.
- 잔여 공백: S3 UI 수정은 kiro 의 임시 렌더 하네스로만 실측됐고 커밋된 ChatLayout
  렌더 테스트는 없음(상태 전이는 코드 리뷰+임시 15건으로 커버).

VERDICT: 라운드 4 검증 대기

## 라운드 4 — kiro/opus 검증 결과 (tasks/...-round4-result.md)

**CONVERGED (M 0 / S 0 / N 4).** 라운드 3 의 S1(tsc)·S2(drain 재취소)·S3(폴링 중 전송)
모두 실 uvicorn 2pod + 실 PG/Redis + 실 SIGTERM/SIGKILL 하네스로 해소 확인.
B2/B3/B4 재재현 시 assistant 행 저장, drain 10건 프로브 전부 PASS(py3.11 이미지 +
py3.14 venv), 크로스파드 취소 지연 ≤0.9s, SIGKILL 뒤 클레임 116s 해제 확인.
회귀 변이(drain 구방식 복원)에 새 테스트 2건이 실패함 — 테스트가 실제로 잡음.

### 라운드 4 — N 잔여 처리 (수렴 후 잔여 정리)

- R4-N1: 409 재개 시 `return` 으로 공통 pending 해제를 건너뜀 — 진행 버블 스피너 유지.
- R4-N2: 예외 경로 persist 를 `shield(_spawn_persist(...))` 로 — 그 commit 도중
  SIGTERM/Stop 이 와도 행 유실 안 됨(실측 재현됐던 마지막 구멍).
- R4-N3: 클레임 갱신을 스트림 루프에서 `_claim_heartbeat` 태스크로 분리 — producer
  수명 전체(invoke 최대 900s·비스트리밍 fallback 포함)를 커버, finally 에서 cancel.
  120s 넘는 비스트리밍 구간에서 이중 실행이 열리던 창을 닫음.
- R4-N4: `runtime_stopped` 플래그 — 크로스파드 취소 경로가 이미 stop_runtime_session
  을 호출했으면 핸들러가 재호출하지 않음(호출 전에 세워 cancel 창도 커버).

### 최종 검증

- admin-api pytest: **1012 passed**, 50 skipped.
- admin-ui: vitest **452 passed**, tsc clean, lint clean(기존 baseline 경고만).
- 차트 테스트: **51 passed**.
- mutation action 메서드 카운트 HEAD 대비 일치(put/delete/post 변형 없음).
- 잔여 유보: R25-N-UI6(ReportCard await 후 window.open 팝업 차단 가능성),
  서비스 토큰 synthetic user 네임스페이스, whoami 실패 시 stale admin_role 쿠키 —
  영향 미미로 별도 트랙 유보.

VERDICT: R25 CONVERGED — 커밋 진행
