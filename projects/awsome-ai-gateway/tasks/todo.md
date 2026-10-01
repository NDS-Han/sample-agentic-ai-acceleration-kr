# Task List: Admin UI 정책 편집 통합

Plan: `tasks/plan.md` · `/users` 페이지: `tasks/plan-users.md`

## 진행 현황 (2026-10-01)

- Phase 0 완료 — `0cd0f62`
- `/users` 개별설정 가시성 완료 — `68a10c5` (팀 정책 따라가기 버튼·트리 개별 점/카운트,
  저장 후 트리 재조회) + 툴팁 네임드그룹 수정 `b563683` — 배포됨
- Phase 3 완료 — `3026167`+`8ce0b80` (rate-limit → /users 이전, 배포+라이브 스모크 확인)
  - Opus 구현 리뷰: CONDITIONAL SHIP → 조건(forward-ref 정리) 반영 완료
- `/users` S1 안전장치 완료 — `9a8873f` (T2·T3·T4 상당: `?node=`는 router.push 대신
  `window.history.replaceState` 사용 — Opus 검토로 서버 라운드트립·remount 회피)
- `/users` S2 섹션 구조 완료 — `3147c9e` (PolicySection+배지+통합Apply+무제한표현)
- `/users` S3 수정 완료 — `tasks/plan-users-s3-fixes.md`의 F1~F15 반영
  - 의미 결정: **Option A 확정** (전체 체크 = 명시 목록 저장, 빈 선택 = 해당 레벨 정책 없음/상속)
  - 팀 앱 접근이 조직 정책을 조회해 상속 배지 표시 (`orgScopeId` prop)
  - ⚠ dev 전용 이슈: 유저 노드 클릭 시 가끔 트리가 재서스펜드 상태로 멈춤
    (Next.js dev RSC 스트리밍 — prod 빌드에서 재현 안 됨을 확인, deploy 후 재검증 필요)

## Phase 0 — 정리 (dead code)

- [x] **T1** 죽은 조직관리 코드 삭제 (`0cd0f62`)
  - 삭제: `createDepartmentAction`, `createTeamAction`, `assignUserTeamAction` (`lib/actions/users.ts`), `lib/actions/index.ts` (barrel, 미사용), `getRoutingProfilesAction`, types/api.ts의 DepartmentCreate*/TeamCreate*/UserTeamAssign*, 미사용 i18n 키(createDepartment, createTeam, assignUser, departmentCreated, teamCreated, userAssigned 등 — grep 확인 후)
  - 주석 정정: users.ts 헤더(TEAM_LEADER는 admin UI에서만 설정 — Cognito sync 아님), gateway.ts, toggleAppModelAction의 revalidatePath('/models') 근거
  - 검증: tsc, vitest, build
  - 규모: M

## Phase 1 — `/users` 안전장치 (선행 필수) — 완료 `9a8873f`

- [x] **T2** OrgTreeView 선택을 `?node=<uuid>` URL state로 전환 — replaceState 방식으로 구현
- [x] **T3** 패널 리마운트 + dirty-guard — `key={node.id}`, 트리/검색 선택 시 ConfirmDialog, id 기반 선택(root 재조회)
- [x] **T4** TeamModelPermissionPanel "제한 해제" — 즉시 DELETE 액션 삭제, staged 저장으로 대체 (S2에서 완결)

### Checkpoint: Phase 1 — 완료
- [x] tsc/eslint/vitest/build 전부 통과

## Phase 1.5 — `/users` S2 후속 수정 — 완료

- [x] **T17** Opus 구현 리뷰 버그 15건 수정 — 상세는 `tasks/plan-users-s3-fixes.md`
  - F1~F4 높음(잘못된 저장): 카탈로그 게이트·멤버십 정규화·로드실패 분리·전체체크 의미(Option A 확정)
  - F5~F10 중간(배지/의미): 조직 상속 배지·0체크 통일·disabled·실패 경로·inherit 배지
  - F11~F15 낮음: failedSection 해소·ref null·join·문서
  - ★ 결정: 전체 체크 = 명시 목록 저장 (Option A)
  - 규모: L

### Checkpoint: Phase 1.5 — 완료
- [x] tsc/eslint/vitest/build 전부 통과
- [x] 시각 검증: 팀 패널(배지 2종)·유저 패널(4섹션+출처 배지) prod 빌드에서 확인

## Phase 2 — 중복 편집기 제거 — 완료 `e44827e` (상세: `tasks/plan-models.md`)

- [x] **T5** `/models`에서 WebSearchTogglePanel 제거 (웹서치는 `/apps` 소유)
- [x] **T6** `GET /admin/users/tree?include_empty=true` + `/users` "빈 팀 표시" 토글
  - Opus 교정: 프론트 필터 불가(서버가 생략) → 백엔드 플래그 + 서버 액션 재조회
  - 빈 팀 노드 "멤버 0" 배지, 딥링크 자동 include_empty 재조회, OFF 시 dirty-guard
  - ⚠️ admin-api 배포 전까지 토글은 no-op(구 API가 쿼리 무시)
- [x] **T7** `/models`에서 TeamModelPermissionPanel 제거
  - `setTeamAllowedModelsAction`의 revalidatePath 1줄만 `/users`로 정정
  - (제외) "N개 팀 제한" 배지 → Phase 5 후속 (Opus 동의, 신규 엔드포인트 필요)
- [ ] 후속: TeamModelPermissionPanel 드롭다운 경로 dead-code 정리 (별도 커밋, Opus 권고)

### Checkpoint: Phase 2 — 완료
- [x] 팀 모델 편집이 /users에서만 가능 (prod 빌드 시각 확인)
- [x] 신규 빈 팀 설정 가능 — include_empty 포함 admin-api 배포됨 (dev 조직에 빈 팀이 없어 표시 자체는 미확인)

## Phase 3 — rate limit을 `/users`로 이전 — 완료 `3026167`+`8ce0b80` (상세: `tasks/plan-ratelimits.md`)

- [x] **T8** 백엔드: `GET /admin/rate-limits/{user|team}/{id}` (own/inherited 분리,
  USER→TEAM 상속은 트리와 동일 규칙) + `DELETE` (deactivate + rl:config:* 무효화 + audit, 멱등)
- [x] **T9** inherit→save 버그 수정 — 상속값은 placeholder만, 필드 value 비움
- [x] **T10** `ScopeRateLimitPanel` — UserPanel·TeamPanel 통합 Apply 합류,
  staged "팀 정책 따라가기/설정 해제"(pendingDelete→Apply 시 DELETE),
  `PolicySectionOpenContext`로 접힌 섹션 폴링·차트 게이트
- [x] **T11** `/rate-limits` → `redirect('/users')` + 정리
  - Sidebar 항목·nav 키·구 트리 컴포넌트·RateLimitTreeNode 타입 삭제
  - `PAGE_PERMISSIONS['/rate-limits']` ADMIN 유지 (middleware가 redirect보다 먼저 default-deny)
  - `/api/rate-limits/*` 프록시·UsageTrendChart 유지, e2e·navigation.test 갱신

### Checkpoint: Phase 3 — 완료
- [x] /rate-limits → /users 리다이렉트, 라이브에서 유저 패널 Rate limit 섹션
  (제한 없음 배지·실시간 사용량·트렌드 차트·무제한 placeholder) 확인 — 배포됨

## Phase 4 — `/budgets` 딥링크 + 다운그레이드 노출 — 완료 `8546663` (상세: `tasks/plan-budgets.md`)

- [x] **T12** `/budgets?team=<uuid>` 팀 행 자동 펼침·스크롤, `?user=<uuid>` SetBudgetDialog 오픈
  - UUID 검증, 비활성 대상 시 includeInactive 강제, /users 링크 2곳 딥링크화
- [x] **T13** 팀 행 다운그레이드 배지 — 최신 배치 규칙 수+활성 여부 (get_current_rules 동일 기준)

### Checkpoint: Phase 4 — 완료
- [x] 라이브 스모크: ?team= 펼침·?user= 다이얼로그 오픈 확인 — 배포됨

## Phase 4.5 — 나머지 페이지 감사+수정 — 완료 `c953e5c`+`449104e` (상세: `tasks/plan-remaining-pages.md`)

- [x] F-K1 /keys revoke 후 stale 행 — 낙관적 제거 (Opus 교정)
- [x] F-K2 /keys 검색 시 status 필터 유실 — hidden input
- [x] F-A1 /apps 허용 유저 → /users?node= 딥링크
- [x] F-A2 /apps default_model 허용 해제 고착 — 재동기화+경고 (Opus 발견)
- [x] F-D1 KPI 카드 → /keys·/budgets·/models 링크
- [x] 라이브 스모크 통과, 배포됨 (admin-ui `4b4e570d`)

## Phase 5 — 후속

- [x] **T14** 팀 effective-policy — `GET /admin/teams/{id}/effective-policy`
  (require_admin) + `get_for_team` 서비스 + TeamPanel "유효 정책" 읽기 전용 카드.
  `EffectivePolicyResponse.user_id` nullable 화, `EffectivePolicyCard`에
  `teamId`/`loadFailed` 추가, 저장 후 팀 정책 재조회.
- [x] **T15** 통합 Apply 섹션별 결과 — `failedSection` → `applyResults[]`,
  dirty 섹션 전부 시도 + sticky 스트립에 saved/failed 칩(`policyState.saved`
  신규 키). UserPanel·TeamPanel 모두 적용.
- [x] **T16** 결정됨 — USER-scope 다운그레이드 편집 없음(팀 단위만 유지),
  모델별 전역 rate limit UI 없음(현재 GLOBAL/TEAM/USER 스코프만 유지).

### 검증
- [x] admin-api pytest 20 ✓ (`get_for_team` 회귀 포함)
- [x] vitest 327 ✓ (`TeamPanel.test.tsx` 신규 6건)
- [x] tsc / lint(기존 경고만) / build ✓

### Checkpoint: Complete
- [x] tsc/eslint/vitest/build/admin-api pytest 전부 통과
- [x] dev 배포 + 스모크 확인 (b6af3ef — admin-api `bebb73f` + admin-ui `afc26d5`,
  팀 패널 유효 정책 카드: 다운그레이드 다이어그램 + 모델×앱 매트릭스 라이브 확인)
