# Plan: Phase 5 후속 — T14 팀 유효정책 + T15 Apply 섹션별 결과

T16 결정 완료(사용자): **USER 다운그레이드 편집 = 팀 단위만 유지**,
**모델별/전역 rate limit 편집 = 현재 스코프만 유지** — 구현 대상 아님.

## T14 — 팀 effective-policy (`/admin/teams/{id}/effective-policy` + TeamPanel 카드)

### 백엔드

`EffectivePolicyService.get_for_team(team_id)` 신규 — `get_for_user`와 같은
`compute_cells` 재사용, 스코프 의미만 다름:

- **apps 축**: `team_allowed_clients` 행 → 없으면 팀→부서→조직 경로의
  `org_allowed_clients` → 없으면 none (fail-open). source: team|organization|none
  (user 경로의 동일 폴백 로직에서 user 단계만 제거한 것과 동일)
- **models 축**: `team_allowed_models` 행 → 없으면 none. source: team|none
- **cells**: `compute_cells(allowed_clients, effective_models, model_rows)` 동일
- **budgets**: `scope_id == team_id` 만 (user 엔트리 없음)
- **rate_limits**: GLOBAL + TEAM scope (`scope_id == team_id`)
- **downgrade_rules**: TEAM scope 해당 팀만
- **web_search**: 동일 (앱별)
- 응답: `EffectivePolicyResponse` 재사용 — `user_id`/`email` 대신 팀 필드.
  스키마 확인 후 team 변형을 허용하도록 user 필드 optional 화 또는 별도 스키마.

엔드포인트: `GET /admin/teams/{team_id}/effective-policy` (users.py 라우터에 추가,
권한은 **ADMIN 전용 `require_admin`** — Opus 리뷰로 정정. TEAM_LEADER 자기 팀
열람은 별도 인가 설계가 필요해 이번 범위에서 제외)

### 프론트 (구현됨)

- `getTeamEffectivePolicyAction(teamId)` — `/admin/teams/{id}/effective-policy`,
  `EffectivePolicy` 스키마 재사용(`user_id`/`email` nullable 화).
- `EffectivePolicyCard`: `policy` prop을 받으면 fetch 생략(기존 계약) +
  `teamId` 자체 조회 경로 + `loadFailed` prop(부모 소유 fetch의 실패 표시).
- `TeamPanel`에 "유효 정책" PolicySection 추가(readonly 배지) — `teamPolicy`
  상태를 부모가 소유하고 Apply 성공 후 재조회해 카드가 stale 해지지 않게 한다.
- 축 라벨(`axis.user_app` 등)은 범용 표현이라 팀 뷰에서도 재사용.

### i18n

- 기존 `users.effectivePolicy.*` 키 재사용 — 추가 키 없음.

## T15 — Apply bar 부분 실패 시 섹션별 결과

현재: 첫 실패에서 `return` + `setFailedSection` + toast. 성공한 앞 섹션은 dirty만
해소될 뿐 "저장됐다"는 피드백이 없고, 실패 섹션 뒤의 dirty 섹션은 미시도 상태가
결과에서 구분되지 않는다.

### 변경 (코드 확인 후 확정)

- 섹션은 서로 독립 리소스(user_allowed_clients / user_allowed_models / rate_limit_configs)
  — **순서 의존 없음** 확인. early-return은 단순 구현일 뿐.
- `handleApply`/`handleApplyAll`: 실패해도 나머지 dirty 섹션을 계속 시도하고
  결과를 수집 — `{key: 'apps'|'models'|'ratelimit', status: 'saved'|'failed'}[]`.
- 기존 resync 로직 유지: UserPanel의 모델 실패 경로가 `savedClients`로 앱 섹션을
  재동기화하는 부분은 "모든 섹션 시도 후" 1회 유효정책 재조회 + 성공 섹션별
  baseline 갱신으로 일반화.
- 표시(구현됨): `failedSection`(첫 실패 1개) → `applyResults[]`(섹션별
  saved/failed)로 교체. sticky 스트립에 결과 칩 — "저장됨: X"(teal) /
  "저장 실패: X"(pink, `policyState.saved` 신규 키). 부분 실패 시에만 표시,
  dirty 전체 해소 시 클리어.
- 실패 섹션 자동 오픈+배지: dirty 가 유지되므로 자연히 열린 상태 유지 —
  복수 실패도 전부 배지+오픈(첫 실패만이 아님, 계획 대비 개선).
- 자식 `save()` 계약(Opus 조건 B) 확인됨 — 실패 시 baseline/dirty 미변경이
  구조적으로 보장되고, T15 테스트 스텁이 동일 계약을 모사.

## 검증 (완료)

- admin-api pytest 20 ✓ (get_for_team: team→org 폴백·모델·downgrade·rate limit)
- vitest 327 ✓ — 신규 `TeamPanel.test.tsx` 6건:
  팀 정책 조회 경로·매트릭스 ✗ 라벨·loadFailed·실패해도 전 섹션 시도·
  부분 실패 칩+dirty 잔존·전부 성공 시 바 소멸+정책 재조회.
- tsc ✓ / lint(기존 경고만) ✓ / build ✓
