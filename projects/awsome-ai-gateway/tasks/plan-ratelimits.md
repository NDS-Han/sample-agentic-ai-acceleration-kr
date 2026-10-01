# Plan: Phase 3 — `/rate-limits` → `/users` 이전

작성일: 2026-10-01 · 선행: `/users` S1~S3 + 개별설정 가시성, `/models` Phase 2 완료.

## 배경 — 왜 이전하는가

정책 소유 구조 합의: `/users` = 조직·팀·유저의 모든 스코프 정책 편집 지점.
`/rate-limits`는 같은 트리를 한 번 더 그려 같은 대상을 편집하는 중복 페이지.
또한 아래 실버그가 있다:

- **inherit→save 굳음 버그 (T9)**: `RateLimitConfigPanel`은 USER 노드의
  `config`를 그대로 input `value`에 넣는다. 그런데 `get_rate_limit_tree`는
  유저 자체 설정이 없으면 **팀 설정을 effective로** 채운다
  (`rate_limit_service.py:150-160`). 즉 상속값이 input에 값으로 들어가 있어
  무심코 저장하면 상속값이 개인 override로 굳어 이후 팀 변경이 안 따라온다.
  — `/users` 앱/모델 패널의 "상속은 placeholder/프리필 + baseline 비교"와
  같은 버그 패턴.

## 현재 상태 요약 (소스 확인 완료)

- 백엔드: `PUT /admin/rate-limits/{user|team}/{id}`·`PUT /global/{alias}`만 존재.
  **단건 GET / DELETE 없음** → T8 신규.
- `get_rate_limit_tree`는 TEAM(+USER 자식)만 반환 — GLOBAL 노드가 트리에
  나오는 경로가 없음 → GLOBAL 분기·아이콘은 이미 dead code.
  모델별 GLOBAL rate limit UI는 Phase 5(T16)에서 별도 결정.
- 리포지토리에 `get_active(scope, scope_id)`·`deactivate_configs` 이미 존재.
- OrgTree 노드 id = user/team uuid — rate-limit scope_id와 동일 키.
- 프론트 통합 Apply 패턴: 자식 패널이 `save()` ref handle +
  `onDirtyChange`/`onSummaryChange` 콜백 (`ScopeAppAccessPanel` 모델).

## 작업

### T8 백엔드: 단건 GET + DELETE

- `GET /admin/rate-limits/user/{user_id}` / `team/{team_id}` →
  `RateLimitScopeStatus { own: RateLimitConfigItem|null, inherited: RateLimitConfigItem|null, inherited_scope: "TEAM"|null }`
  - USER: own 없으면 팀 active config를 inherited로 (팀명은 클라이언트가 트리에서 안다 — id만 반환하면 됨. 표시명은 tree meta.team_name)
  - TEAM: own만 (상위 상속 없음 — 트리에서도 TEAM은 inherited_from=None)
  - GLOBAL: Phase 5까지 UI 없음 — 엔드포인트는 user/team만
- `DELETE /admin/rate-limits/user/{user_id}` / `team/{team_id}` →
  `deactivate_configs` + Redis `ratelimit:config:{scope}:{sid}` 삭제 +
  `rl:config:{scope}:{sid}:*` 패턴 무효화 + audit `DELETE_RATE_LIMIT`
- 회귀 테스트: 상속 해제 후 GET own=null·inherited=팀값, 캐시 무효화 호출 검증

### T9+T10 프론트: `ScopeRateLimitSection` (공용)

`/users`의 UserPanel·TeamPanel에 "Rate limit" `PolicySection`으로 삽입:

- `lazyMount`(사용량 폴링이 있으므로) — 처음 펼칠 때 마운트, 이후 유지(dirty 소실 방지)
- 마운트 시 단건 GET → own/inherited 상태 배지(`policyState` 재사용: 상속/개별 설정/제한 없음)
- **입력 규칙**: inherited 중이면 필드는 비워두고 상속값을 `placeholder`로 표시(T9 수정). 필드 비움 = unlimited(null) 저장. "상속으로 되돌리기" 버튼 = staged `pendingDelete` 플래그 → Apply 시 DELETE 호출 → 성공 시 필드 비우고 배지 상속 전환
- 통합 Apply: `save()` ref + `onDirtyChange` + `onSummaryChange` 패턴 준수. Apply 순서: 앱 → 모델 → rate limit
- 사용량: 기존 `fetchRateLimitUsage` + `UsageTrendChart` 재사용, 폴링 deps는 `node.id`(객체 identity 아님 — Opus 지적사항)
- GLOBAL 스코프 UI는 없음(기존과 동일 — 모델별 GLOBAL은 T16에서 결정)

### T11 `/rate-limits` 정리

- `page.tsx` → `redirect('/users')` (쿼리 `?node=`가 있으면 `/users?node=`로 매핑 — id가 같은 uuid라 가능)
- Sidebar 항목 제거, `navigation.spec.ts` 갱신, `PAGE_PERMISSIONS['/rate-limits']`는 제거하되 middleware 통과 확인(리다이렉트 전 403이면 권한 유지 필요)
- RateLimitTree/TreeView/ConfigPanel 삭제; `UsageTrendChart`·`fetchRateLimitUsage`는 새 섹션이 재사용 → 이동만
- `setRateLimitAction` revalidatePath `/rate-limits` → `/users`
- GLOBAL dead 분기(RateLimitTree 아이콘 등) 컴포넌트와 함께 제거

## 검증

- admin-api pytest 신규(GET/DELETE/inherit) + 기존 전체
- tsc/vitest/lint/build + `/rate-limits` → `/users` 리다이렉트 e2e 갱신
- prod 빌드 시각 확인: 팀/유저 패널의 rate-limit 섹션, 상속→placeholder 표시, staged 삭제→Apply

## 결정 사항

1. ~~GLOBAL rate limit UI~~ → Phase 5(T16)에 위임(현재도 도달 불가, 회귀 아님)
2. rate-limit 섹션의 저장 = 통합 Apply에 합류(별도 저장 버튼 두면 혼동)

## 구현 완료 (배포 전)

- **T8 백엔드**: `GET/DELETE /admin/rate-limits/{user|team}/{id}` +
  `RateLimitScopeStatus{own,inherited,inherited_scope}` — USER 상속 규칙은
  트리와 동일(own 없으면 팀 config). DELETE는 `deactivate_configs` +
  `rl:config:{scope}:{sid}:*` 무효화 + `ratelimit:config:*` 대칭 삭제 + audit.
  회귀 테스트 `test_high_rate_limit_status.py` 6건 통과.
- **T9+T10 프론트**: `ScopeRateLimitPanel` (신규) — 상속 시 필드 비움+상속값
  placeholder(T9 수정), "팀 정책 따라가기"/"설정 해제"는 staged pendingDelete
  → Apply 시 DELETE. `PolicySectionOpenContext`(신규)로 접힌 섹션의 사용량
  폴링·트렌드 차트를 게이트. UserPanel·TeamPanel 모두 통합 Apply
  (앱→모델→rate limit 순) + 섹션별 되돌리기/실패 배지에 합류.
- **T11**: `/rate-limits` → `redirect('/users')`. Sidebar 항목·nav 키·
  RateLimitTree/TreeView/ConfigPanel·RateLimitTreeNode 타입 삭제.
  `PAGE_PERMISSIONS['/rate-limits']`는 ADMIN-only 유지(middleware가 redirect
  보다 먼저 default-deny). `/api/rate-limits/*` 프록시·`UsageTrendChart`·
  `rateLimitUsage` 유틸 유지. e2e·navigation.test 갱신.
- **검증**: tsc ✓ / vitest 317 ✓ / lint 기존 경고만 ✓ / build ✓ /
  admin-api 관련 테스트 25 ✓

## Opus 리뷰 결과 (CONDITIONAL SHIP) — 반영 조건

- **T11**: `PAGE_PERMISSIONS['/rate-limits']`는 **제거하지 않고 ADMIN-only 유지** — middleware가 page.tsx의 `redirect()`보다 먼저 돌고 미등재 경로는 default-deny(`/403`)라서 제거하면 리다이렉트가 실행조차 안 됨
- **T8 GET**: `inherited` 조립은 `get_rate_limit_tree`의 USER 상속 규칙(own 없으면 팀 config)과 같은 규칙 — 서비스에 공용 헬퍼로 뽑아 두 경로 불일치 방지
- **T8 DELETE**: 기능상 필요한 건 `rl:config:{scope}:{sid}:*` 패턴 무효화. `ratelimit:config:{scope}:{sid}` 키는 `_set_rate_limit`이 쓰지만 읽는 소비자가 없는 dead write(대칭 삭제는 위생 조치로 수행)
- **`/api/rate-limits/usage*` 프록시 라우트는 유지** — 새 섹션의 UsageTrendChart/fetchRateLimitUsage가 계속 호출(`/api/`는 middleware permission 무관)
