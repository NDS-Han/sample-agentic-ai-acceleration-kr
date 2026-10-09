# 전체 페이지 UX/기능 리뷰 #2 — "렌더링·표현 일관성" 결함군 중심

날짜: 2026-10-07
브랜치: `nds/admin-ui-ux-review` (배포 태그 `6959840` 이후 상태)
방법: Devin 1차(스크린샷 전 페이지 + 코드 추적) → kiro-cli claude-opus-5.5 2차 → 통합

## 리뷰 배경 — 왜 다시 보는가

이전 두 번의 페이지 리뷰(`review-pages-2026-10.md`, `review-ux-all-pages-2026-10.md`)는
"보이는 것" 위주였다 — 레이아웃·라벨·빈 상태. 그래서 아래 결함군이 반복적으로 빠져나갔다:

- **같은 데이터, 다른 표현** — 추이 차트가 같은 `/admin/analytics` 응답을 Chart.js와
  recharts 두 엔진으로 그려 색·범례·툴팁이 어긋났다(이번에 발견·수정).
- **무증상 오답** — 에러 없이 나가는 0/빈값/placeholder (`tracked=false`인데 RPM 0 표시 등).
- **부분 적용 필터** — 화면에 필터가 있는데 일부 섹션·export에만 적용.
- **canonical 규칙 부분 적용** — 팀명 `{dept}_{team}` 통일이 by_team 차트엔 미적용.

이번 리뷰는 이 결함군을 체크리스트로 삼아 페이지·컴포넌트·엔드포인트를 훑었다.

---

## 1차(Devin) 발견

### A. 같은 데이터, 다른 표현

| # | 심각도 | 발견 | 근거 |
|---|--------|------|------|
| A1 | MED | **모델 색상 팔레트 불일치**: `/analytics`의 `모델별 비용` 막대(Chart.js)는 `CATEGORICAL_PALETTE` 순서 `[teal, pink, amber, sky, ...]`를 쓰지만, 같은 페이지 하단 상세 섹션의 비용 비중 바는 `SHARE_COLORS` `[teal, sky, pink, violet, amber, ...]` — **2번째 모델부터 색이 갈림**. 대시보드 도넛도 `CATEGORICAL_PALETTE`라 share bar만 이질적 | `ModelCostDetail.tsx:14-23` vs `chartTheme.ts:16-25` |
| A2 | MED | **모델 정렬 순서 불일치**: 상세 표는 `ORDER BY cost DESC`(service)인데 breakdown 막대는 `sum_usage_by_model` — **ORDER BY 없음**(dict 순서 = DB 반환 순서). 같은 페이지에서 같은 모델 목록이 다른 순서로 나올 수 있음 | `analytics_repository.py:sum_usage_by_model` (group_by만, order_by 없음) vs `analytics_service.py:476` |
| A3 | MED | **`by_team` 라벨 canonical 미적용**: `group_by=team`일 때 막대 차트 라벨이 raw `Team.name`("Developers") — `TeamBreakdown` 스키마에 `department_name` 없음. 같은 화면의 추이 차트 범례는 `NDS_Developers`(canonical) → 동명 팀 2개 실존하므로 구분 불가 재발 | `BreakdownChart.tsx:33` + `schemas/analytics.py:57` + `analytics_service.py:217` |
| A4 | MED | **라벨-집계 불일치 "총 요청 수"**: analytics ROI 카드는 `총 요청 수 / 기간 내 총 API 요청 수`이지만 실제 집계는 `cost_period_filter` = **SUCCESS 전용**. 대시보드는 같은 지표를 `성공 요청 수 / 이번달 SUCCESS 요청`으로 표시 → 같은 숫자가 페이지마다 다른 의미를 주장 | `analytics_repository.py:128` + `messages/ko.json:790` vs `:111` |
| A5 | LOW | **Export CSV가 group_by/scope/client 무시**: UI는 `group_by`·`scope`를 보내지만 `/admin/analytics/export`는 `by_model`만 CSV로 출력(`_to_csv`). `group_by=team` 화면에서내면 모델별 CSV가 나옴. client 파라미터는 아예 엔드포인트에 없음 | `analytics_service.py:844-851` + `routers/analytics.py:175` |

### B. 무증상 오답

| # | 심각도 | 발견 | 근거 |
|---|--------|------|------|
| B1 | LOW | 모델 상세 표 `평균 지연`이 `0ms`로 표시될 수 있음 — `avg_latency_ms or 0` → 데이터 없는 모델도 "0ms". monitoring은 같은 패턴을 `—`로 고쳤는데 여긴 잔존 | `ModelCostDetail.tsx:119` + `analytics_service.py:503` |
| B2 | LOW | 모델 상세 섹션 에러 시 `ErrorState`(비-compact, 큰 아이콘) — 형제 섹션은 전부 `compact`. 실패 시 레이아웃만 다르게 깨짐 | `analytics/page.tsx:142` vs 형제들 `:53` `:52` `:54` `:33` |

### C. 렌더링 엔진 혼용 (잔존)

| # | 심각도 | 발견 |
|---|--------|------|
| C1 | LOW | 추이만 recharts로 통합됨. `/analytics` 내 `모델별 비용` 막대·`토큰 분석` 도넛은 여전히 Chart.js — 툴팁 스타일·호버·테마 변수 처리가 카드마다 다름. `chartDefaults` 폰트 주입으로 한글은 해결됐지만 엔진은 혼재 | `LazyCharts.tsx`, `BreakdownChartClient.tsx`, `TokenMixDonutClient.tsx` |

### D. 기타

- 대시보드 도넛 범례에서 `Claude Code · Haiku 4.5`가 3줄로 감김(좁은 범례 열) — LOW
- `/chat` 비활성 상태는 의도된 빈 화면 — 사이드바에 그대로 노출돼 클릭하면 안내 페이지. 배지/숨김 고려 — LOW
- lazy(dynamic ssr:false) 차트가 첫 진입에 ~수 초 빈 카드로 보임 — 스켈레톤 없음 — LOW

## 확인된 정상 (회귀 없음)

- 대시보드↔분석↔모델 상세 수치 일치 ($0.26 / 14건 / 317.3K·317,272)
- 팀명 canonical: 추이 범례·top-teams·예산 모두 `NDS_Developers`
- `/analytics/models?period=X` → `/analytics?period=X` 리다이렉트 파라미터 보존
- models API custom range `period` 응답 에코 + 한쪽만 오면 400
- rate-limit `tracked` 구분, 모니터링 `—`, 예산 `차단됨` 배지
- 캔버스 한글 폰트(chartDefaults) 4개 클라이언트 전부 적용

## 2차(kiro-cli claude-opus-5.5) + 1차 통합 — 최종 발견 목록

kiro 8건 중 6건 검증 통과(전부 코드 근거 확인), 2건은 1차 발견과 중복. 아래가 통합본이다.

### HIGH — 같은 화면에서 같은 엔티티가 다르게 보임

**H1. `group_by=team` 막대 라벨이 canonical 규칙을 안 탐** (kiro 발견1 = 1차 A3)
- `BreakdownChart.tsx:33` `b.team` 원시값 → `Developers`. 같은 화면 추이 범례는 `NDS_Developers`.
- 근원: `schemas/analytics.py:57` `TeamBreakdown`에 `dept_name` 없음 + `analytics_service.py:217` by_team 쿼리가 `Department` 조인 누락(형제 `trends_by_team`은 조인함).
- 수정: 스키마에 `dept_name` 추가 + 쿼리에 OUTER JOIN + 프론트 `teamDisplayName` 적용.

**H2. 같은 모델이 위젯마다 다른 색 + 다른 순서** (kiro 발견2 = 1차 A1+A2 합침)
- 색: `ModelCostDetail.SHARE_COLORS` vs `chartTheme.CATEGORICAL_PALETTE` — index 1부터 순서 다름(2위 모델: 막대=pink, 비중바=sky).
- 순서: 표·비중바는 `cost DESC` 정렬인데 `모델별 비용` 막대는 `sum_usage_by_model`에 **ORDER BY 없음** → dict/DB 반환 순서. 색만 맞춰도 순서가 다르면 여전히 매칭 불가.
- 수정: `SHARE_COLORS` 삭제→`CATEGORICAL_PALETTE` 단일 출처(inline `style={{backgroundColor}}`), repo에 `ORDER BY cost DESC`.

### MED — 표현/필터 일관성

**M1. 모델 표기 "표시명 vs 원시 alias"** (kiro 발견3)
- 대시보드 도넛: `modelDisplay(alias, display_name)` → `Claude Opus 4.5`
- `/analytics` 표·막대, `/my` 표: raw `model_alias` → `claude-opus-4-5`
- 수정: 백엔드 모델 응답에 `display_name` 조인 추가, 소비처는 `modelDisplay` 통일.

**M2. 토큰 포맷터 4종 혼재** (kiro 발견4)
- `page.tsx` 로컬 `formatTokens`(B/M/K, 소수 2자리) / `fmtTokensCompact`(K/M, **B 분기 없음** — ≥10억이 `1050M`으로 나감) / `TokenMixDonutClient`의 `Intl compact` / 모니터링 `toLocaleString`.
- 수정: `fmtTokensCompact`에 B 추가 후 단일화, 정밀값은 `title` 툴팁으로.

**M3. "총 요청 수" 라벨 vs SUCCESS 전용 집계** (1차 A4)
- analytics ROI 카드 `총 요청 수/기간 내 총 API 요청 수` — 실제는 `cost_period_filter`(SUCCESS only). 대시보드는 같은 지표를 `성공 요청 수`로 표기.
- 수정: 라벨을 `성공 요청 수`로 통일 (또는 전체 요청 집계를 추가 — 실패 요청 포함 집계는 별도 설계).

**M4. Export가 화면 필터와 불일치** (1차 A5)
- `/admin/analytics/export`는 `scope`·`client` 파라미터 없음 + `_to_csv`가 `group_by` 무시하고 `by_model`만 출력. `group_by=team` 화면에서 내려도 모델별 CSV.
- 수정: 엔드포인트에 scope/client 추가 + `_to_csv`를 group_by 분기로.

**M5. rate-limit 추이 X축이 리포팅 TZ 아님** (kiro 발견6)
- `UsageTrendChart.tsx:51` `getHours()/getMinutes()` = **브라우저 로컬 TZ**. 앱 전역은 `useReportingTz()` — 백엔드 버킷도 KST.
- 수정: `Intl.DateTimeFormat`에 reporting TZ 주입.

**M6. `/analytics` 엔진 혼재 잔존** (1차 C1 = kiro 발견5)
- 추이=recharts 통합됐지만 막대·토큰 도넛은 Chart.js. 테마 소스 이원화(`useChartTheme` computed hex vs `hsl(var(--chart-N))`), 두 라이브러리 번들.
- 수정: 중기적으로 막대/도넛도 recharts 이전. 단기로는 전부 `useChartTheme` 경유 통일.

### LOW

- **L1.** 모델 상세 `평균 지연` `0ms` 가능 — `avg_latency_ms or 0`. monitoring처럼 `—` 처리 (1차 B1)
- **L2.** 모델 상세 섹션 에러가 비-compact `ErrorState` — 형제들은 compact (1차 B2)
- **L3.** `by_user` 막대에 팀 병기 없음 — 대시보드 Top은 subtitle에 팀 표기 (kiro 발견7)
- **L4.** 모니터링 배지 렌더 이원화 — `EventLog`는 CSS 클래스, `ModelHealthTable`은 `<Badge>` 컴포넌트 (kiro 발견8)
- **L5.** 대시보드 도넛 범례 `Claude Code · Haiku 4.5` 3줄 줄바꿈 (1차 D)
- **L6.** lazy 차트 첫 진입 빈 카드 플래시(스켈레톤 없음) (1차 D)
- **L7.** `/chat` 비활성 안내 페이지가 사이드바에 그대로 — 배지/숨김 고려 (1차 D)
- **L8(참고).** custom 기간에서 마이그레이션 seed 미포함은 **의도된 동작**(주석 근거 확인됨) — 다만 "custom 구간은 이관분 미포함" 힌트 추가 권고 (kiro)

### kiro가 확인한 "신규 결함 없음" 영역

- **무증상 오답**: KPI null→`—` 접지, 모니터링 latency `—`, rate-limit `tracked`, 섹션별 ErrorState 저하 — 전부 방어 확인.
- **권한 경계**: `permissions.ts` default-deny, analytics 캐시 ADMIN+scope=all 한정(회귀 테스트 존재), by_team/by_user/trends/seed 전부 `scope_ids` 격리 — 누출 없음.

### 권고 순서

1. **H1·H2** — `/analytics` 한 화면 내 모순, 수정 범위 좁음(백엔드 1필드+조인, 프론트 팔레트·라벨 교체)
2. **M5(TZ)·M3(라벨)·M4(export)** — 작고 명확한 단독 수정
3. **M1·M2** — 공용 유틸 이미 있음, 소비처 교체 위주
4. **M6** — 엔진 통합 중기 리팩터링
5. L1~L8 여유 시

