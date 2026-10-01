# Plan: `/users` — "팀 정책 따라가기" 버튼 + 트리 개별설정 가시성

작성일: 2026-10-01 · 사용자 요청 3건. 선행: `/users` S1~S3, `/models` Phase 2 완료.

## 상태: ✅ 구현·배포 완료 (커밋 `68a10c5`, admin-api `b72309d` + admin-ui `1ef6215` 롤아웃)

**Opus 리뷰: CONDITIONAL SHIP** — 반영한 조건:
- H1: 버튼은 `policy?.*_source === 'user'`일 때만 표시, null(로드 실패)이면 숨김(fail-closed)
- M1: `custom_policy_count`는 **활성** 멤버 교집합(비활성은 트리 노드가 없어 개수 불일치 방지)
- M2: 저장 성공 시 `onSaved` 콜백으로 트리 재조회 → 점/카운트 stale 방지
  - 파생 버그 수정: `treeOverride`에 `overrideHasEmpty` 플래그 추가 — 저장 후 기본 트리가 override에 들어가도 "빈 팀 표시" ON이 정상 재조회
- L1/L2: 배지 문구 "앱/모델 개별 정책 적용 중"(범위 명시), `=== true` 조건으로 합성 노드 안전

**라이브 검증** (admin-dev): USER 노드 amber 점 1개, Developers 팀 "개별 1명" 배지, 버튼→staged→Apply 바 동작 확인.

## 요구사항

1. 개인 유저의 정책을 팀 정책으로 되돌리는 버튼
2. custom(개별 설정) 된 것을 team 정책으로 되돌리는 기능
3. 조직 트리에서 개별 설정이 있는 사용자를 한눈에 볼 수 있는 표시

→ 1과 2는 같은 기능(override 해제 = 상속 복귀)의 두 진입점이다.

## 백엔드 의미 (이미 지원 — 확인 완료)

- `user_allowed_clients` 0행 → team→org→none 폴백 (`effective_policy_service.py`)
- `user_allowed_models` 0행 → 팀 폴백, override 해제와 동일 (`model.py:172`)
- 즉 유저 스코프에서 `[]` 저장이 곧 "팀 정책 따라가기"다 — 별도 DELETE API 불필요
- `EffectivePolicy`의 `allowed_clients_source`/`allowed_models_source`가 `user`면 개별 설정 존재

## 작업

### T-A. 유저 패널 "팀 정책 따라가기" 버튼 (프론트만)

- 위치: UserPanel의 앱 접근/모델 섹션 헤더(또는 섹션 푸터) — `*_source === 'user'`일 때만 표시
- 동작: **즉시 저장이 아니라 staged** — 버튼이 해당 섹션의 선택을 빈 상태(`[]` 저장 = 상속 복귀)로 만들고 dirty를 켠다. 확정은 통합 Apply bar. 이유:
  - 통합 Apply 철학과 일치, 되돌리기(revert)로 취소 가능
  - 섹션별 staged 상태라 앱만 리셋하고 모델은 유지하는 조합도 가능
- 대안(즉시 저장 + ConfirmDialog): 버튼 한 번에 끝나지만 Apply 패턴과 어긋나고 실수 롤백 경로가 없음 → staged 채택 권고
- staged 후 힌트는 기존 `emptyHint*`가 "저장하면 상위 정책 적용"을 이미 설명

### T-B. 트리 개별설정 가시성 (백엔드+프론트)

**백엔드** (`user_team_service.py` `get_org_tree`):
- 트리 조립 전 member id 집합 수집 → `SELECT DISTINCT user_id FROM auth.user_allowed_clients` ∪ `user_allowed_models` 집계 쿼리 1방 (전체 행 나열 말고 in-절 or 전체 distinct — 테이블이 작아 전체 distinct로 충분)
- `OrgNodeMeta`에 `has_custom_policies: bool` 추가 (USER 노드만 true 가능)
- TEAM 노드에는 `custom_policy_count` (하위 멤버 중 개별설정 수) — 선택적, 트리에서 팀 단위 요약
- DEPARTMENT/ORG에는 합계 필요 시 `custom_policy_count` — 과하면 생략, 팀만

**프론트**:
- `entities.ts` OrgNodeMeta에 필드 추가
- `OrgTree` USER treeitem: `has_custom_policies`면 이름 옆 작은 amber 점/`개별` 배지 — 트리 폭이 좁으므로 점+title tooltip 수준
- TEAM 노드: `custom_policy_count > 0`이면 muted "개별 N" 표시
- 검색 결과(`OrgSearchBox` 합성 노드)에는 데이터 없음 — 트리 노드만 표시

### i18n
- `users.followTeamPolicy` ("팀 정책 따라가기" / "Follow team policy")
- `users.customBadge` ("개별" / "Custom"), `users.customCount` ("개별 {count}명" / "{count} custom")

## 검증
- 백엔드 pytest: include_empty 테스트 파일에 has_custom_policies 플래그 테스트 추가
- tsc/vitest/lint/build
- 시각: override 있는 유저 트리 배지 + 팀 카운트 + 버튼 동작(버튼→staged→Apply→상속 배지 전환)

## 결정 요청
1. staged(vs 즉시) — staged 권고
2. TEAM/ORG 집계 카운트 범위 — 팀만 vs 부서·조직까지
3. rate-limit override도 "개별 설정"에 포함할지 — Phase 3에서 /users로 이전 예정이라 지금은 앱·모델 두 축만, rate-limit은 이전 후 확장
