# R13 — Admin API 분석 경로 스코프/집계 리뷰

대상: `admin-api/src/app/services/analytics_service.py`, `routers/dashboard.py`, `routers/my.py`, export 경로.
리뷰어: swe-2 + kiro `claude-opus-5.5`. 날짜: 2026-10.

## Findings

### H1 — `_resolve_scope` 기본-허용 → TEAM_LEADER 전사 분석 누출 (수정)

임의 scope 문자열(`scope=x`, `scope=ALL`)이 catch-all 분기로 **GLOBAL** 해석 —
TEAM_LEADER가 `?scope=anything`으로 전사 KPI/유저별/트렌드 데이터를 조회 가능했다.
`team:not-a-uuid`는 파싱 실패로 동일하게 GLOBAL → 데이터 누출 + 500 혼재.

**수정**: 기본-거부 재작성 —
- `scope=None`/`"all"`만 유효; TEAM_LEADER는 소속 팀 + `auth.teams.leader_user_id`로
  리드하는 팀의 합집합으로 좁혀짐. 그 외 문자열은 400.
- `team:<uuid>` 검증(잘못된 UUID 400). TEAM_LEADER는 소속/리드 팀에 한해 허용,
  무관 팀은 403 — **인가 판정 전 데이터 질의 없음**.
- 리드 팀 목록 조회(`auth.teams` 단일 질의)는 인가 자체이므로 데이터 접근 아님.
- export/CSV 경로도 동일 `_resolve_scope` 사용 확인.

파일: `analytics_service.py::_resolve_scope`
회귀: `tests/regression/test_r13_analytics_scope.py` (6건)

### M1 — TEAM_LEADER 무팀 계정 전사 데이터 접근 (수정, 이전 세션)

`team_id=None`인 TEAM_LEADER가 `scope=all`로 전사 집계 조회 가능했음 → `ForbiddenError`.
회귀: `test_critical_teamleader_null_team_global_analytics.py` (12건) — auth.teams 인가
조회는 허용하고 usage 데이터 질의만 금지하도록 단정 정정.

### M2 — `get_model_cost_detail` 팀 필터 누락 (수정, 이전 세션)

모델 상세 질의가 actor 팀 격리를 적용하지 않았음 → usage 질의에 `team_id IN` 강제.
회귀: `test_model_cost_detail.py` — 인가 조회와 데이터 질의 구분해 단정.

### M3 — Export 경로 스코프 미적용 (수정, 이전 세션)

CSV export가 `_resolve_scope`를 거치지 않아 TEAM_LEADER 전사 export 가능했음 → 동일 해석 적용.

### M4 — 대시보드 KPI 예산 한도 per-app 이중 합산 (수정)

한도 합계 질의가 aggregate 행 + per-app 설정 행을 모두 합산 → 총한도 과대표시
(utilization 과소). 집계 시 per-app(`client IS NOT NULL`) 설정 행 제외.

파일: `routers/dashboard.py`
회귀: `test_high_dashboard_kpi_contract.py` — BudgetUsage는 한도/분모 컨텍스트에만
허용, 분자는 UsageLog+SUCCESS+리포팅 TZ 필터 유지하도록 계약 갱신.

### M5 — KPI 사용액(분자) 기준 불일치 + `/my` 버짓 표시 (수정)

- KPI 분자를 `BudgetUsage` 집계와 `UsageLog` SUCCESS-only를 용도별로 분리:
  집행 카드는 BudgetUsage, 사용량 KPI는 UsageLog+`cost_period_filter`(KST).
- `/my` 사용자 예산 소비는 `client IS NULL` 집계 행 기준으로 통일
  (per-app 행까지 합산하던 이중계상 제거).
- 총한도 0이면 utilization `None`(0% 오표시 방지).

## 수용 잔여 (accepted)

- TEAM_LEADER 리드 팀 해석이 `auth.teams` 요청당 1질의 — 인가 정확성 > 캐시.
- `team:<uuid>`는 ADMIN/소유·리드 TEAM_LEADER만 — DEVELOPER 임의 팀 지정 불가(의도).
- CSV export는 필터 동일하나 행 수 제한 없음 — 기존 동작 유지(별도 과제).

## 검증

- `admin-api` 전체: **946 passed, 50 skipped** (+ 회귀 6건 신규 → 952 pass 계열)
- `test_r13_analytics_scope.py` + `test_model_cost_detail.py`: 15 passed
- 경고(미등록 `unit` 마크, mocked scan_iter coroutine)는 기존 하네스 노이즈.
