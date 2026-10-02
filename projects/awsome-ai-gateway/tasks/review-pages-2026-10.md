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

## 3차 리뷰 — 페이지별 기능 검토 (구현 완료)

1차(내 감사) + Opus 독립 검증으로 발견된 기능 결함.

### F1 🔴 HIGH — 예산 다이얼로그 policy/thresholds 조용한 리셋

`SetBudgetDialog`가 기본값(`HARD_BLOCK`/`[80,90,100]`)으로만 열리고 `setBudgetAction`이 항상 전송 → 기존 `SOFT_WARNING`+커스텀 thresholds인 예산도 **금액만 수정하면 정책이 리셋**됐다.

수정 (prefill + optional-preserve 병행):

- 백엔드: `GET /admin/budgets/{scope}/{scope_id}` 신설 — DB `BudgetConfig`(policy·T·D) + Redis(`thresholds`, 유일한 저장소) 병합. `configured=false`(미설정)/`alert_thresholds=null`(Redis 미스)로 "값 불명"을 기본값과 구분.
- 백엔드: `SetBudgetRequest`의 `policy`/`alert_thresholds`를 `model_fields_set`으로 판별해 **미전송 시 기존값 보존** — `set_team_budget`/`set_user_budget`/`set_user_client_budget` 모두 적용 (`_resolve_policy_thresholds`). 기존 D 보존 패턴과 동일.
- 프론트: 다이얼로그 오픈 시 prefill. baseline=null(서버값 불명)인 필드는 관리자가 바꾸지 않으면 PUT 생략. 대상 전환 중 응답 도착 오염 방지 stale 가드.
- `_sync_redis_thresholds`/`_sync_redis_app_config`는 명시값 시그니처로 변경.

### N1 — per-app confirm 재시도 중복 PUT

409 확인 재시도가 성공한 총예산/이전 앱 PUT을 재전송 → `mainSavedRef`/`doneAppsRef`로 완료분 스킵.

### N2 — 다운그레이드 배지 stale

저장 후 팀 행 배지가 재조회 전까지 구값 → `AutoDowngradeConfig.onSaved` 콜백으로 로컬 배지 즉시 갱신.

### F2/F3/F4 — 소규모

- `/analytics/models` 백 링크 `?period=` 보존.
- `EventLog` 필터 변경 경로 `.catch` + 에러 토스트(폴링 경로와 구분 — 사용자 액션은 명시 알림).
- keys dead i18n 11키 제거 (createKey/copyKey/rotation* 등 — CLI 발급 경유가 의도된 설계).

검증: admin-api pytest 710 ✓ (신규 5: 보존 2 + get_config 3) / vitest 338 ✓ / tsc ✓ / lint 신규 경고 0 / build ✓.

## Opus 구현 리뷰 2 (커밋 d255291)

**Verdict: CONDITIONAL SHIP → 조건 반영 완료.**

- **B1 (차단, 수정됨)**: 신설 `GET /admin/budgets/{scope}/{scope_id}`에 팀리더 스코핑이 없어 크로스-팀 IDOR — 형제 읽기 경로(`get_user_app_budgets`, `get_team_allocation`)가 이미 막은 BR-BUD-03 회귀. 서비스에 `actor` 전달 + TEAM은 `scope_id != actor.team_id`, USER는 대상 유저의 `team_id` 비교로 거부 + 테스트 3건 추가.
- 통과: 라우트 섀도잉 없음(정적 라우트 뒤 등록), 비활성 행 policy 이어받기 의도 일치(§3-1), Redis 미스 폴백=Lua 기본값 `[80,90,100]` 일치, 프론트 baseline 판별 3경우 정상, mainSavedRef 스킵 데이터 무결성 문제 없음.

## 배포 (3차 리뷰 수정)

- 커밋: `d255291`(기능) + `8fab506`(B1 IDOR 조건)
- 이미지: `llm-gateway/admin-api:8fab506` + `llm-gateway/admin-ui:8fab506` — 롤아웃 완료
- 라이브 스모크: `GET /admin/budgets/team/{id}` 무인증 → **401** (라우트 존재·인증 필요 확인)

---

# 4차 — 서비스 목적 정합성 리뷰 (Devin + Opus 5.5)

## 배경

사용자가 `/analytics` 기간 선택 `<select>`의 화살표가 hover 시 여러 개로 깨져 보이는 스크린샷을 보고하고, "서비스 목적에 맞게" 페이지별 기능 재검토를 요청. 이번 리뷰 초점은 스타일이 아니라 **"운영자가 보는 화면이 게이트웨이 실제 상태와 일치하는가"**.

## 발견·수정 (커밋 2e18d08 + 조건 반영)

| # | 발견 | 처리 |
|---|------|------|
| 1 | 셀렉트 화살표: `appearance-none` + 인라인 SVG `background-image`가 일부 브라우저에서 세로 타일링되어 쌓여 보임 (`AnalyticsFilter`, `PeriodSelector`) | 인라인 SVG 오버레이(pointer-events-none, aria-hidden)로 교체. Opus 미용 지적 2건(chevron이 select의 활성 text 색을 못 따라감 — wrapper가 `currentColor` 제공, `bg-transparent`와 활성 `bg-primary/10` 순서 경쟁 — 비활성만 transparent)도 반영 |
| 2 | **`/monitoring` body logging 읽기 실패를 OFF로 표시** — 주석 논리와 반대로 프라이버시 최악 방향(실제로 수집 중인데 "꺼짐"으로 보임) | `initialEnabled: null` 추가 → "알 수 없음" 배지 + 스위치 비활성화. 테스트 갱신 |
| 3 | **모든 모델 편집이 새 가격 버전 생성** — 설명만 고쳐도 `effective_from=now` pricing PUT (이력 오염 + 비원자적 2-PUT) | `pricing_changed` 플래그(1e-9 float 비교)로 미변경 시 스킵. Opus 지적 추가: pricing PUT은 비멱등이라 `withRetry` 제거(재시도 시 중복 버전) |
| 4 | **optional 필드 삭제가 silent no-op** — description/display_name 비워도 null→백엔드 무시→성공 토스트만 | 백엔드 `model_fields_set`로 "생략=유지 / 명시적 null=삭제" 구별 구현(allowed_clients와 동일 규칙). endpoint_url은 의도적으로 제외(endpoint 필요 provider에서 지우면 런타임 파손) + UI에서 비BEDROCK는 required |
| 5 | **`/models`에 allowed_clients(모델×앱 제한)가 전무** — 제약 편집은 `/apps` AppPolicyPanel에만 존재(발견가능성 갭) | 테이블에 읽기 전용 배지(null=미표시/[]=전면 차단/목록=N개 앱) + `/apps` 링크. `ModelListItem.allowed_clients` + listActiveModelsAction 매핑 |
| 6 | create 다이얼로그 재오픈 시 이전 값/에러 유지 | `isOpen` 전이 시 폼 리셋 |
| 7 | 모델 생성 POST가 `withRetry` 안 — 타임아웃 재시도 시 409 오인 | retry 제거(비멱등) |
| 8 | `/my`가 `?period` 없을 때 최신 데이터 월 표시 — 미사용자는 과거 월 + 이번 달 예산 카드 병존 | 기본값=현재 월, 데이터 월 목록에 없으면 목록에 추가 |
| 9 | `toKoreanMonthLabel`이 로케일 무관 "YYYY년 M월" 하드코드 | `Intl.DateTimeFormat(locale, {month:'long', year:'numeric', timeZone:'UTC'})` |

## 반박된 제 발견

- `/models`의 `allowed_clients` 편집 UI 부재 → `/apps` AppPolicyPanel에 3-상태 confirm까지 이미 존재. 남은 건 발견가능성뿐이라 배지로 해소(#5).

## Opus 구현 리뷰 3 (2e18d08) — CONDITIONAL SHIP → 조건 반영

- 조건(완료): `model_service.update_model`의 allowed_clients 주석이 "다른 nullable 필드는 null=무시 유지"라고 쓰여 있으나 바로 아래 코드가 description/display_name을 null=삭제로 처리 — 주석 갱신. `schemas/models.py`의 display_name NOTE도 stale — 갱신.
- 반영: pricing PUT `withRetry` 제거(비멱등 — 위 #3에 병합).
- 확인됨: UI는 항상 4키를내므로 "생략"이 백엔드에 도달하지 않음 → 프리필 폼에서 비우기=삭제 의도와 일치. 외부 호출자의 `null="no change"` 관례는 계약 변경이므로 changelog 언급 필요.
- 통과: `/cli`·`/chat` 이상 없음, chevron/unknown/i18n/로케일 라벨 정상.
- 잔여(비차단): 편집 폼의 endpoint_url은 HTML required만(whitespace 통과), BEDROCK에서 endpoint 비워도 무시되어 UI-DB 불일치 가능, 두 admin 동시 편집 lost-update(전 필드 공통 기존 사양).

## 검증

- admin-ui: tsc ✓ / vitest 변경분 31 ✓ (이전 전체 342 + stale 테스트 갱신) / lint 신규 0 / build ✓
- admin-api: pytest 715 ✓ (model_service 26 + 회귀: 명시적 null 삭제/생략 유지)
- 라우트·스키마 계약 변경: `PUT /admin/models/{alias}`의 `description`/`display_name` explicit null이 이제 삭제를 의미

## 배포 (4차 리뷰 수정)

- 커밋: `2e18d08`(기능) + `9c8f90c`(Opus 조건 — stale 주석 + pricing retry 제거 + 본 문서)
- 이미지: `admin-api:9c8f90c` + `admin-ui:9c8f90c` — EKS 롤아웃 완료
- 라이브 스모크(admin-dev/admin-api-dev 호스트): `/` → 307 로그인 리다이렉트, `/analytics` → 307, admin-api `/health` → 200, `/admin/models` 무인증 → 401 ✓
  - ⚠️ `gateway-dev.*` 호스트로 admin 경로를 치면 전부 401 — 그 도메인은 LLM 게이트웨이(VK 필요)이며 정상 동작이다.
