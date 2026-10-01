# Plan: Phase 4 — `/budgets` 딥링크 + 다운그레이드 배지

작성일: 2026-10-01 · 선행: `/users` 전체(S1~S3+개별설정 가시성), `/models`, `/rate-limits` 이전 완료.

## 배경

- `/users` 패널의 "예산 관리에서 편집" 링크가 현재 `/budgets` bare URL — 긴 표에서
  대상 팀/유저를 다시 찾아야 한다. 대상을 지정하는 딥링크로 바꾼다.
- 팀 행을 펼쳐야만 `AutoDowngradeConfig`가 보인다 — 다운그레이드 규칙이 있는 팀을
  접힌 상태에서도 알 수 있게 규칙 수 배지를 단다.

## 현재 상태 (소스 확인 완료)

- `page.tsx` (server): summary + models 조회 → `BudgetSummaryTable` (client) 에
  `items` 로 전달. TEAM_LEADER 는 `TeamAllocationView` (딥링크 대상 아님 — admin 전용).
- `BudgetSummaryTable`: `expanded` state 로 팀 행 토글, `handleOpenDialog(item)` →
  `SetBudgetDialog`. 미배정 유저는 `UNASSIGNED_KEY` 그룹.
- summary API는 예산 없는 유저/팀도 전수 반환 → `?user=` 대상은 항상 행이 있다.
- `OrgDetailPanel`: UserPanel 의 budgetInput 섹션 링크(`/budgets`)와 TeamPanel
  budgetSectionTitle 링크(`/budgets`) — 둘 다 bare.
- 다운그레이드 규칙: `budget.downgrade_policies` (is_active, scope, scope_id).
  `get_budget_summary`는 규칙 수를 반환하지 않음 → 추가 필요(N+1 회피용 1쿼리 집계).

## 작업

### T12 딥링크

1. `page.tsx` — `searchParams` 읽어 `focusTeam`/`focusUser` 로 `BudgetSummaryTable` 에 전달.
   UUID 형식(`^[0-9a-f-]{36}$` 정도)만 통과, 나머지는 무시.
2. `BudgetSummaryTable` — props `focusTeam?`, `focusUser?` 추가, 마운트 시 1회 적용:
   - `focusTeam`: 해당 팀이 items에 있으면 `expanded[id]=true` + 행 `scrollIntoView`.
     행에 `id={`budget-row-${team.target_id}`}` 부여.
   - `focusUser`: 해당 유저 item 탐색 → 소속 팀(또는 UNASSIGNED) expanded + 팀 행
     scrollIntoView + `handleOpenDialog(user)` 자동 오픈.
   - 적용은 1회만(후속 수동 토글을 덮지 않음) — `useRef` 플래그.
3. `/users` 링크 갱신:
   - UserPanel `budgetInput.editInBudgets` → `/budgets?user=${node.id}`
   - TeamPanel `budgetSectionTitle` 링크 → `/budgets?team=${node.id}`

### T13 다운그레이드 규칙 수 배지

1. 백엔드 `get_budget_summary`:
   - `downgrade_policies` 에서 `is_active=true AND scope=TEAM` 그룹집계 1쿼리 →
     `dict[scope_id → count]` (N+1 없음)
   - `BudgetSummaryItem` 에 `downgrade_rule_count: int | None = None` 추가 — TEAM 행만.
   - USER-scope 다운그레이드 규칙은 현재 UI 로 편집할 곳이 없고(T16 결정 대기)
     배지도 TEAM 만.
2. 프론트: `BudgetSummaryItem` 타입 + `RawBudgetItem` 파싱에 필드 추가.
   팀 행 이름 옆에 `badge badge-violet`(또는 neutral) `다운그레이드 N` — 0/None 이면
   미표시. i18n 키 `budgets.downgradeBadge` (ko/en).

## 검증

- admin-api: summary 회귀 테스트에 count 포함 케이스 추가 또는 기존 테스트 통과 확인
- tsc/vitest/lint/build
- 라이브 스모크: `/budgets?team=<id>` 행 펼침·스크롤, `?user=<id>` 다이얼로그 오픈,
  `/users` 링크가 파라미터 포함으로 이동, 다운그레이드 배지 표시

## 결정 사항

- 딥링크는 admin 뷰 전용 — TEAM_LEADER 는 `TeamAllocationView` 만 보고 팀 행 표가 없다.
- `?user=` 는 다이얼로그를 바로 여는 대신 "행 하이라이트"만 하지 않는다 —
  /users 링크의 의도가 "편집"이므로 다이얼로그 오픈이 맞다(ESC/취소로 닫으면 행 위치 확인 가능).

## Opus 계획 리뷰 (CONDITIONAL SHIP) — 반영 내용

- **(필수) 배지 카운트 = 최신 배치 기준** — `is_active` 집계가 아니라
  `get_current_rules` 와 같은 "max(created_at) 배치" 기준 + `bool_or(is_active)` 로
  enabled 여부. 꺼진 규칙도 펼친 패널에 보이므로 배지는 `다운그레이드 N`(활성=sky,
  꺼짐=neutral+tooltip)으로 일치시킴.
- **(필수) 비활성 대상**: `focus*` 대상이 `is_active=false` 면 `setShowInactive(true)`
  강제 — 필터에 걸려 무음 실패 방지.
- **(필수) Strict Mode**: `focusAppliedRef` 가드를 effect 최상단에서 설정.
  deps 는 `[items, focusTeam, focusUser]` 로 정직하게 — ref 가 1회를 보장(새
  eslint-disable 추가하지 않음).
- (권장 반영) `page.tsx` 시그니처에 `searchParams` prop 추가 + `items.map`
  매핑 라인 추가를 작업 항목으로 명시. UUID 표준 패턴(8-4-4-4-12 hex) 적용.
- 스크롤 대상: 유저 행은 펼침 후에야 렌더되므로 항상 렌더된 **팀/그룹 행**에
  `id="budget-row-<id>"` 부여해 그쪽으로 scrollIntoView.

## 구현 완료 (배포됨 — admin-api `eb960ea` + admin-ui `13873e5`)

- 백엔드 `get_budget_summary` — TEAM 스코프 `downgrade_policies` 최신 배치
  그룹집계 1쿼리(scope_id → count, bool_or(is_active)) → `BudgetSummaryItem`
  에 `downgrade_rule_count`/`downgrade_enabled` 추가. 회귀 테스트 2건 추가
  (badge fields / null).
- 프론트 `page.tsx` — `searchParams` 검증(UUID) → `focusTeam`/`focusUser`.
- `BudgetSummaryTable` — 마운트 1회 포커스(expand+scroll+dialog), 팀 행
  배지, `budget-row-<id>` 행 id, 미배정 그룹 행 id.
- `OrgDetailPanel` — `/budgets` 링크 2곳을 `?user=`/`?team=` 딥링크로.
- i18n `budgets.downgradeBadge`/`downgradeBadgeOff` (ko/en).
- 검증: tsc ✓ / vitest 317 ✓ / build ✓ / admin-api budget 64+2 ✓ / lint 클린.
- Opus 구현 리뷰: **SHIP** — 딥링크 effect(1회성·StrictMode·스크롤 대상)·
  집계 SQL(최신 배치 join)·스키마 하위호환·배지 오부착 없음 확인.
- 라이브 스모크: `/users` 팀 링크→`/budgets?team=`(행 펼침 확인),
  `?user=`→예산 다이얼로그 자동 오픈 확인.
