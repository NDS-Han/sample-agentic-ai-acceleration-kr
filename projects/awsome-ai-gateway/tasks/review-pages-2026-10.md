# 전체 페이지 리뷰 통합 (Devin 1차 + Opus 2차)

날짜: Phase 5 배포(b6af3ef) 직후. 범위: admin-ui 모든 페이지 + 미들웨어/권한.
판정 규칙: Opus가 코드를 직접 읽고 내 발견을 true/false 검증 + 신규 발견 추가.

## 발견 요약 (심각도순)

| # | 심각도 | 발견 | 위치 | 상태 |
|---|--------|------|------|------|
| A | blocker | `/cli` 페이지 미들웨어 공개 — `startsWith('/cli')`가 인증/권한 검사 전 pass-through. PAGE_PERMISSIONS 는 ADMIN+TEAM_LEADER인데 실제론 미인증 공개 | `middleware.ts:99` | 수정 완료 |
| B | high | `/keys` 소프트 네비 stale — `KeysListView`가 `useState(initialItems)`만 쓰고 `key` 없음. status pill/email 검색 시 이전 필터 목록이 그대로 남음 | `keys/page.tsx` + `KeysListView.tsx:39` | 수정 완료 |
| C | high | 예산 경보 임계값 3곳 불일치 — 백엔드 canonical `>=90/>=70`(budgetVisuals 동일). 그런데 `budgets/page.tsx`는 `>=100/>=80`, dashboard `calcAlertLevel`은 `>=95/>=80`. 같은 행에서 AlertBadge(잘못된 임계)와 UsageBar(올바른 임계)가 모순 표시 | `budgets/page.tsx:77`, `page.tsx:42`, `budgetVisuals.tsx:67` | 수정 완료 |
| D | medium | TopSpendTable 행 미링크 — `/budgets?team=`·`/users?node=` 딥링크 패턴 이미 존재 | `TopSpendTable.tsx` | 수정 완료 |
| E | medium | models 매핑 복붙 — `APIModelItem`+`mapToModelListItem`이 models/page.tsx와 budgets/page.tsx에 동일 중복 | 두 page.tsx | 수정 완료 |
| F | medium | 색-only 심각도 — `UsageBar`(BudgetGaugeRow 사용처)와 `KPICard` alertLevel이 색만으로 경보 전달. `aria-label`에 심각도·수치 추가 권장 | `budgetVisuals.tsx`, `KPICard.tsx` | 수정 완료 |
| G | low | KPI 카드 href 3/8개만 — 나머지(이번달 사용량/일평균/총요청/총토큰/유저당평균)는 `/analytics` 딥링크 자연스러움 | `page.tsx` | 수정 완료 |
| H | low | `/keys` resetSearch가 status도 리셋 — 같은 페이지가 email↔status 역방향은 상호 보존하는데 reset만 비대칭 | `keys/page.tsx:66` | 수정 완료 |
| I | low | budgets models fetch 실패 시 조용히 [] → SetBudgetDialog 모델 선택 빈 상태와 구분 불가 | `budgets/page.tsx:115` | 수정 완료 |
| J | low | `/keys` 내부 Suspense 무효 — 데이터를 await한 뒤 감싸므로 fallback 미표시(실제 로딩은 loading.tsx 담당) | `keys/page.tsx:96` | 수정 완료 |
| K | low | 에러/빈 분기 패턴 2종 혼재 — page await+ErrorState(keys/models/users/budgets/cli) vs 섹션 Suspense(dashboard/analytics/monitoring/my). 신규 페이지는 후자 권장 | 전반 | 수정 완료(문서화는 본문) |
| L | low | keys status pill에 `aria-current` 없음 — 현재 필터가 색으로만 전달 | `keys/page.tsx:84` | 수정 완료 |
| M | info | ClientDistribution이 `?client=` 미수신 — 앱 점유율 도넛에 단일 앱 필터는 무의미하므로 의도적이 맞음. "전체 앱 기준" 명시만 권장 | `page.tsx:402` | 유지 |

## 검증된 정상 영역 (Opus 확인)

- 권한 정합성: PAGE_PERMISSIONS ↔ Sidebar ↔ 백엔드 authz 일치 (A 제외).
- i18n: ko/en 각 1014키, 대칭 0 diff. 하드코딩 한국어는 BI agent 컨텍스트용이라 의도적.
- 섹션별 에러 degradation: dashboard/analytics/monitoring 패턴 우수.
- ClientFilter/PeriodSelector 상호 쿼리 보존 올바름.
- `compute_cells` 등 정책 합성 경로는 Phase 5에서 이미 검증.

## 구현 요약

- **A**: `middleware.ts` — `/cli` 공개 우회 제거(다운로드는 `/api/cli-download` 프록시). 회귀 테스트 4건.
- **B**: `KeysListView`에 `key={status:email}` — 필터 소프트 네비 시 remount.
- **C**: `alertLevelOf`를 `lib/utils/alertLevel.ts`(순수 lib, 서버 컴포넌트도 사용 가능)로 추출해 단일 출처화. budgets ≥100/≥80 · 대시보드 ≥95/≥80 → 모두 ≥90/≥70.
- **D**: `TopSpendRow.href` — 팀→`/budgets?team=`, 유저→`/users?node=`.
- **E**: `lib/utils/modelMapping.ts` 단일 출처화(APIModelItem+mapToModelListItem).
- **F**: `UsageBar`에 `role=progressbar`+`aria-label`, `BudgetGaugeRow`가 `정상/경고/위험` 텍스트 포함 aria 전달, `KPICard.alertLabel`로 심각도 텍스트.
- **G**: KPI 카드 8개 전부 href (이번달사용량/일평균→`/analytics?period=`, 유저당평균→`+group_by=user`, 총요청/총토큰→`/analytics?period=`).
- **H**: `/keys` reset이 status 유지 (`/keys?status=`).
- **I**: `modelsLoadFailed` 플래그 → `AutoDowngradeConfig`가 "모델 목록 로드 실패" 경고 표시.
- **J**: `/keys` 무효 Suspense 제거(데이터는 page-level await 후 렌더).
- **L**: status pill에 `aria-current="page"`.
- **M**: 도넛 미수신은 의도적 — 유지(문서화만).

검증: tsc ✓ / vitest 338 ✓ (middleware 15, alertLevel 2, modelMapping 3 신규) / lint 신규 경고 0 / build ✓.

## 기존 우선순위 (실행됨)

1. **A** — 미들웨어 `/cli` 우회 제거(레거시 `/cli/download` 라우트는 이미 없고 `/api/cli-download` 프록시가 담당). 보안.
2. **B** — `KeysListView`에 `key={`${status}:${email}`}` 추가(필터 변경 시 remount). 데이터 정확성.
3. **C** — `budgets/page.tsx`/`page.tsx`가 `alertLevelOf`(>=90/>=70)를 공유 사용하도록 통일. 과금 경보 정확성.
4. D·E·F·G — UX 일관성 개선.
5. H~L — 소규모 정리.

## Opus 구현 리뷰 (커밋 9980729)

**Verdict: SHIP** — 6개 검증 포인트 전부 직접 확인:
- 백엔드 `_alert_level` (`budget_service.py:1286`) ≥90/≥70 과 `alertLevelOf` 완전 일치
- `/api/cli-download` 공개 유지 확인, 레거시 `/cli/download` 라우트 부재 확인
- `key` remount가 cursor/hasMore와 충돌 없음 (KeysListView에 resync effect가 없어 remount가 유일한 리셋 경로)
- 딥링크 소비처 확인 (`/users?node=`는 OrgTreeView가 window.location.search로 소비)
- 전체 테스트 338 passed / tsc exit 0 실측 재현
- 비차단 관찰: UsageBar aria-valuenow(캡 100) vs aria-label(실제 %) 비대칭 — 결함 아님

## 배포 (admin-ui 만 — 백엔드 변경 없음)

- 이미지: `llm-gateway/admin-ui:9980729` (digest sha256:4d75a73…)
- 롤아웃: `llm-gateway-admin-ui` 완료
- 라이브 스모크:
  - `GET /cli` 미인증 → **307 /api/auth/login** (이전 200 — 우회 수정 확인)
  - `GET /cli/download/linux/x64` 미인증 → 307 (서브패스도 차단)
  - `GET /api/cli-download/...` 미인증 → 404 (공개 유지, 프록시 정상 도달)
  - `GET /` 미인증 → 307 (기존 동작 유지)
