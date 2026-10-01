# Implementation Plan: Admin UI 정책 편집 통합 (scope-keyed consolidation)

## Overview

관리자 정책 편집이 `/users`(대상 중심)와 `/models`·`/rate-limits`(리소스 중심) 양쪽에 분산·중복되어 있다.
정책의 DB 키 기준으로 소유 페이지를 통일한다:

- **스코프 키** (org/team/user): 허용 앱, 허용 모델, rate limit → `/users`
- **리소스 키** (model/app): 앱×모델 정책, 기본 모델, 웹서치, 모델별 전역 rate limit → `/apps`, `/models`
- **예산 키**: 예산, cap, 다운그레이드 → `/budgets`

조직 생성/배정은 AWS Cognito 외부 관리이므로 관련 dead code는 삭제한다.

## Architecture Decisions

- **수정된 A(대상 중심) 채택** — Opus 5.5 검토 결과: 스코프 키 정책의 편집자는 `/users`가 자연스러운 소유자.
  단 ① 다운그레이드는 예산 % 발화 규칙이므로 `/budgets`에 유지(노출만 개선), ② 예산 편집은 `/budgets` 유지
  (TEAM_LEADER가 편집 가능한 유일 페이지 — `/users`는 ADMIN-only).
- **`/users` 패널은 상태-우선 섹션 구조** — 각 정책 섹션이 접이식(`<details>`)이며 헤더에 상태 배지
  (무제한/상속/override N개)를 표시해 펼치지 않아도 현재 적용 상태를 알 수 있게 한다.
- **무거운 섹션은 lazy mount** — rate-limit 사용량 차트·10초 폴링은 `<details>` 내부에 두고
  펼칠 때만 마운트.
- **빈 상태 의미의 불일치 명시** — apps `[]`=무제한, team models `[]`=거부(min 1), user models
  `[]`=override 삭제(DELETE), rate limit 빈값=unlimited. 통합 Apply bar 아래에서 섹션별로
  저장 결과를 구분 표시하고, 각 섹션은 현재 결과 상태를 미리 보여준다.
- **딥링크는 search-param 기반** — `AppPolicyPanel`의 `?app=` 패턴을 복제. `revalidatePath`로
  인한 선택 소실도 함께 해결된다.

## 알려진 선행 버그 (통합 전에 수정 — Opus 5.5 소스 검증)

- `OrgDetailPanel`의 `<TeamPanel>`/`<UserPanel>`에 `key` 없음 → 노드 전환 시 A팀 편집이 B팀에 저장 가능.
- `/users` 트리에 dirty-guard 없음 (`/rate-limits`에는 있음).
- `TeamModelPermissionPanel`의 "제한 해제"가 Apply bar를 거치지 않고 즉시 실행·확인 없음.
- rate-limit 폼: 상속값이 value로 채워져 저장하면 상속값이 개인 override로 굳음.
- rate-limit에 DELETE 없음 → override를 inherit로 되돌릴 수 없음.
- `/admin/users/tree`가 멤버 0인 팀을 숨김 → `/models` 패널이 신규 Cognito 팀 정책의 유일한 진입점.

## Task List

`tasks/todo.md` 참조.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `/users` 패널 과대 (이미 ~950줄) | Med | 섹션 lazy mount + 상태 배지; 파일을 섹션별 컴포넌트로 분할 |
| 백엔드 변경 필요 (tree empty flag, rate-limit GET/DELETE, team effective-policy) | High | 프론트 단독 작업과 분리; 백엔드 먼저 배포 후 프론트 연결 |
| `/rate-limits` 페이지 제거 시 기존 링크 깨짐 | Med | `/rate-limits` → `/users` redirect 유지 |
| 통합 Apply bar 부분 실패 | Med | 섹션별 성공/실패 결과를 개별 표시 |
| 동시 편집 last-write-wins | Low | 별도 백엔드 과제로 문서화 (이번 범위 밖) |
| 딥링크가 로그인 후 유실 (`?next` 미지원) | Low | 현행 인증 동작 유지, 문서화만 |

## Open Questions

- USER-scope 다운그레이드 편집 UI를 `/budgets`에 추가할지 (현재 backend·컴포넌트는 지원하나 저장은 차단됨 — 의도된 상태 유지 여부)
- `/models`에 "N개 팀 제한" 카운트+링크(역방향 조회) 추가 여부
