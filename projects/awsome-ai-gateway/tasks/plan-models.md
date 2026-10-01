# Plan: `/models` 페이지 — 중복 편집기 제거 (Phase 2)

작성일: 2026-10-01 · 선행: `/users` S1~S3 완료 · Opus 5.5 계획 리뷰 반영 (CONDITIONAL SHIP → 아래 수정 반영)

## 페이지 기능 정의 (현재)

| 영역 | 현재 기능 | 판정 |
|---|---|---|
| 모델 카탈로그 | 목록/등록/수정/비활성·활성, 가격 동기화 | **유지 — 이 페이지의 소유 기능** |
| 팀별 모델 접근 | TeamModelPermissionPanel (팀 드롭다운 + 모델 체크) | **제거** — `/users` TeamPanel이 소유 |
| 앱별 웹서치 | WebSearchTogglePanel | **제거** — `/apps` AppPolicyPanel이 동일 백엔드(`routing_profiles.web_search_enabled`)를 이미 커버 (`AppPolicyPanel.tsx:98-103, 273-282`) |

결과: `/models` = **모델 카탈로그 관리** 페이지로 단순화.

## T5 — WebSearchTogglePanel 제거 (S)

- `page.tsx`: `<WebSearchTogglePanel>` 섹션 + `routing-profiles` fetch(`routingRes`) + import 제거
- `WebSearchTogglePanel.tsx` 파일 삭제
- i18n: `models.webSearch.*` 만 제거. `setClientWebSearchAction`/`RoutingProfileItem`은 `/apps`가 사용 — 유지
- (선택) `constants/gateway.ts:143`의 WebSearchTogglePanel 언급 주석 정리

## T6 — 빈 팀 진입점 (M, 백엔드+프론트) — Opus 교정: Option B

**Option A(프론트 필터) 폐기 사유**: 빈 팀은 백엔드가 생략(`user_team_service.py:455`)
하므로 프론트 토글이 데이터를 만들 수 없고, 클라이언트 필터링은 선택된 빈 팀이
사라지는 경로에서 dirty-guard(`OrgTreeView.requestSelect` 미경유)를 우회한다.

- **백엔드**: `GET /admin/users/tree?include_empty=true` 쿼리 파라미터 추가
  - `get_org_tree(session, include_empty)`: `include_empty`일 때 `if not active_members: continue`(`:455`)만 건너뜀 → 빈 TEAM 노드 포함
  - 부서 제외 규칙(`:493` `if not team_nodes: continue`)은 유지 — 빈 팀을 포함하면 그 부서의 `team_nodes`가 비지 않아 자동 포함됨. **"팀이 하나도 없는 부서"만 제외**(노이즈). Opus 지적: "빈 팀만 있는 부서"는 반드시 포함되어야 T6 목적이 성립.
- **프론트**: `OrgTreeView`에 "빈 팀 표시" 토글 → 서버 액션 `getOrgTreeAction(includeEmpty)`으로 클라이언트 재조회. URL 네비게이션/라우트 리페치 없이 `root` state만 교체 → dirty-guard·선택 상태 보존.
  - 토글 OFF 복귀: props의 `root`(서버 원본, 이미 빈 팀 제외)로 복원 — 재fetch 불필요
  - 선택된 노드가 빈 팀인 상태에서 OFF → 트리에서 사라짐 → 기존 "not found → clear" 경로 사용. panelDirty면 ConfirmDialog로 경고(노드 전환과 동일 가드)
  - 빈 팀 노드 라벨에 "멤버 0" 표기 — 정책 미리 설정용임을 명시

## T7 — TeamModelPermissionPanel 제거 (M)

- `page.tsx`: `<TeamModelPermissionPanel>` 섹션 + `teamsRes`(`/admin/users/teams`) fetch 제거.
  dead 정리 체크리스트(Opus): `allTeams`/`teams` 가공(`:77-85`), `APITeamItem` 인터페이스(`:33-38`), import(`:9`,`:10`,`:12`)
- 컴포넌트 파일 **유지** — `/users` TeamPanel 임베드가 사용. 드롭다운 경로(pickedTeamId/teams/allTeams/showInactive)의 dead-code 정리는 **별도 커밋/후속**(임베드 저장 로직과 blast radius 분리 — Opus 권고)
- i18n: `teamModelAccess`(page.tsx h2 전용) 제거 가능. `selectTeam`/`selectTeamPlaceholder`/`includeInactiveTeams` 등 컴포넌트 참조 키는 **유지**(Opus 지적 — 키 참조가 코드에 남아있음)
- `models.ts`: **`setTeamAllowedModelsAction` 내부의 `revalidatePath('/models')` 한 줄만 `/users`로 정정**. 카탈로그 CRUD의 나머지 5곳(`:64,:110,:137,:155,:299`)은 `/models` 유지. `users.ts`에는 정정 대상 없음

## 제외 (후속)

- **T8 역방향 배지 "N개 팀 제한"**: 백엔드 신규 엔드포인트+쿼리 필요, 빈도 낮음 → Phase 5 후속 (Opus 동의)
- 드롭다운 경로 dead-code 정리: 별도 커밋

## 검증

- tsc / vitest / lint / build + admin-api pytest(해당 시)
- 수동: `/models`에 모델 테이블만 남는지 / `/apps` 웹서치 토글 동작 / `/users` 빈 팀 토글(ON→빈 팀 노드 표시·"멤버 0" / OFF→원복) / 선택된 빈 팀에서 OFF 시 dirty-guard
- 회귀: OrgSearchBox·`?node=` 복원·dirty-guard 정상
