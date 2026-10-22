# R39 — admin-ui 2차 리뷰 + UX 표면

날짜: 2026-10 · 대상: UX 리뷰 #1/#2 이후 잔여 항목 추적 + 횡단 표면(i18n 패리티·권한표 정합·라우트 위생·테스트).
베이스라인: `tsc --noEmit` 0 error · `vitest` **452/452** · i18n ko/en **1095/1095 완전 패리티**.

## UX 리뷰 #2 잔여 항목 추적

| 항목 | 상태 | 근거 |
|---|---|---|
| H1 by_team 라벨 canonical | ✅ 반영 | `BreakdownChart.tsx:43` `teamDisplayName({team, dept_name})` |
| H2 팔레트/정렬 단일화 | ✅ 반영 | `SHARE_COLORS` 제거 — `CATEGORICAL_PALETTE` 단일 출처 |
| M1 모델 표시명 | ✅ 반영 | `modelDisplay` 통일 |
| M2 토큰 포맷터 | ✅ 반영 | 포맷터 단일화 |
| M3 라벨-집계 불일치 | ✅ 반영 | 라벨 정정됨 |
| M4 export 필터 불일치 | ✅ 반영 | group_by/scope 전파 |
| M5 추이 X축 TZ | ✅ 반영 | reporting TZ 적용 |
| L1 평균지연 0ms | ✅ 반영 | `avg_latency_ms > 0 ? …ms : '—'` |
| L2 비-compact ErrorState | ✅ 반영 | `ErrorState compact` 통일 |
| L7 `/chat` 비활성 노출 | ✅ 사실상 해소 | 사이드바 `chatEnabled` 게이트 + 직접 URL은 안내 카드(`unavailableTitle/Desc`) |
| M6 엔진 혼재 | ⏳ 잔존 | recharts(추이 2종) + Chart.js(막대·도넛 4종) — 중기 리팩터 항목 유지 |
| L3 by_user 팀 병기 | ⏳ 잔존 | LOW — 대시보드 Top과 다름 |
| L4 배지 렌더 이원화 | ⏳ 잔존 | LOW |
| L5 도넛 범례 줄바꿈 | ⏳ 잔존 | LOW |
| L6 lazy 차트 빈 플래시 | ✅ **이번에 수정** | `LazyCharts`에 `loading: ChartSkeleton` 추가 |

## 수정

| 파일 | 내용 |
|---|---|
| `components/analytics/LazyCharts.tsx` | dynamic import `loading:` 플레이스홀더 — 첫 진입 시 카드 공백(L6) 해소 |
| `app/not-found.tsx` (신규) | 404 페이지 — 기존 `errors.notFound` + `forbidden.backHome` 키 재사용, 루트 error.tsx 톤과 일치 |

## 검증된 정상

- **권한표↔사이드바↔백엔드 3층 정합**: `PAGE_PERMISSIONS` 11경로가 `Sidebar.NAV_ITEMS`와 정확히 일치, 백엔드 `require_admin` 표면과도 일치(chat 8엔드포인트 주석으로 근거 명시). `/my`의 ADMIN 제외는 의도된 역할 경계.
- **`/rate-limits`**: `/users` 이전 후 의도된 redirect + `?from=` 안내 배너 + PAGE_PERMISSIONS 유지(middleware default-deny 대응 주석).
- **`/chat` 미배포 경로**: `CHAT_ENABLED` env로 사이드바 숨김 + 페이지 안내 카드 이중 방어.
- **`/my` 셀프서비스**: period 선택기 + 예산 카드 + 월 경계 정합 — 빈 상태·에러 상태 각각 graceful.
- **테스트 위생**: `act()` 미래핑 stderr는 테스트 아티팩트(제품 코드 비결함), `cliDownloadProxy`/`chatProxyPathWhitelist`/`middlewareCsp` 등 BFF 경계 테스트 존재.

## 판정 보류 (UX 백로그 → R43 종합 반영)

- **M6 엔진 혼재** — recharts 통합 중기 리팩터(테마 소스 이원화 해소 필요).
- **모바일 대응** — 사이드바 `w-64` 고정, 반응형 없음. 관리 도구 특성상 데스크톱 전제로 LOW 보류(태블릿 대응은 햄버거 드로어 정도면 충분).
- **L3/L4/L5** — 표현 일관성 소형 항목, 다음 UI 작업 시 일괄.
- **`act()` stderr** — 테스트 하네스 위생(Async update 래핑), 제품 무관.
- **`not-found.tsx` 없음** → 이번에 추가.
