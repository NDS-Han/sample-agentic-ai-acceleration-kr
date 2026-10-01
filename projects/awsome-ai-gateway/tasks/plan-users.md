# Plan: `/users` 페이지 — 스코프 정책 허브 + 안전장치 + 섹션 UX

## 목표

`/users`가 스코프 키(org/team/user) 정책의 단일 편집지가 된다. 이전에
발견된 실버그(노드 전환 상태 carryover, dirty 소실, stale 로드)를 먼저
고치고, 그다음 "펼치지 않아도 상태가 읽히는" 섹션 구조를 입힌다.

## 구현 슬라이스

### S1 — 안전장치 (실버그 우선)

- `OrgTreeView`: 선택 노드를 `?node=<uuid>` URL state로 (AppPolicyPanel `?app=` 패턴)
- `OrgDetailPanel`: `<TeamPanel key={node.id}>`, `<UserPanel key={node.id}>`
  — 팀 A→B 전환 시 리더 선택/다이얼로그/dirty가 새 팀으로 새는 버그 수정
- `TeamModelPermissionPanel`: teamId 변경 시 `loaded=false` 리셋 — 이전 팀
  목록이 새 팀에 저장되는 경로 차단
- dirty-guard: 미저장 편집 중 노드 전환·검색 선택 시 ConfirmDialog
  (RateLimitTreeView 패턴 이식)
- "clear restriction" 즉시 실행 → staged 선택지(Apply bar 경유) 또는 최소
  ConfirmDialog
- 리더 지정 후 `router.refresh()` 시 selectedNode 구 객체 stale → root에서
  id로 재조회

### S2 — 섹션 구조 (상태-우선 아코디언)

- 모든 정책 섹션 기본 collapsed, `<summary>`에 `aria-hidden` chevron
- 헤더 2토큰: 상태(무제한/제한 N개/설정 N개) + 출처(자체/팀/조직/기본)
- 최초 펼침 시 lazy mount, 이후 unmount 금지(dirty 보존)
- dirty 또는 마지막 실패 섹션은 자동 펼침 + "수정됨" 마커
- 노드 이름 아래 sticky 스트립: 노드 타입 · dirty 개수 · 마지막 실패 섹션
- 섹션별 "빈 선택 의미" 결과 문구 (전체 해제 시: 전체 차단 등)

### S3 — 통합 Apply bar

- 순서 고정: 앱 → 모델 → (rate limit 추가 시) rate limit, 첫 실패에서 중단
- bar에 dirty 섹션 목록 + 섹션별 되돌리기
- 실패 귀속: 자식 save()가 {ok, error} 반환 → 부모가 섹션명 포함 토스트,
  실패 섹션 자동 펼침
- "원자적 저장 아님 — 앞 섹션은 저장된 상태로 남습니다" 문구
- 저장 중 편집 차단: disabled prop 하향 또는 apply 시점 스냅샷

### S4 — 유효 정책 + i18n

- 섹션 헤더에 1줄 resolved 상태; 전체 EffectivePolicyCard는 마지막 collapsed
- 저장 후 refetch, 저장 전엔 "pending" 표시 (기존 UserPanel 동작 유지)
- `policyState.*` 공용 네임스페이스 + ICU plural (ko: "개별 설정", en: "custom")
- 모델 라벨 통일: display_name 우선, 없으면 alias (양쪽 그리드 동일)
- 배지 `whitespace-nowrap`, ko 길이 대응

## 백엔드 의존 (이번 슬라이스 밖)

- `/admin/users/tree` include_empty 플래그 (T6 — /models 패널 제거 전 필수)
- rate-limit 단건 GET/DELETE (T8 — rate-limit 섹션 이전 시)
- 팀 effective-policy 엔드포인트 (T14 — 팀 유효정책 카드)

## 검증

- tsc / eslint / vitest / next build
- 수동: 팀 A 편집→B 전환 확인 다이얼로그, 새로고침 후 ?node 복원,
  섹션 collapsed 상태에서 배지만으로 상태 파악, Apply 부분 실패 시
  실패 섹션명 토스트
