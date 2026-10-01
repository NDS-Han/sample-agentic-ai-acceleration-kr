# Plan: 나머지 페이지 감사 (/apps /keys /analytics /dashboard /monitoring /my /cli /chat)

Phase 0~4 완료 후 남은 페이지 전수 감사 결과. 전반적으로 성숙 — 대규모 작업 없음.
결정론적 결함 2건 + 저비용 개선 2건.

## 발견 사항

### `/keys` — F-K1 (버그, 중간) revoke 후 행이 ACTIVE로 남음

- `KeysTable.handleRevoke` 성공 시 토스트만. `KeysListView`의 `items`는
  `useState(initialItems)` — 서버의 `revalidatePath('/keys')`가 서버 컴포넌트를
  갱신해도 클라이언트 state는 유지되어 revoked 키가 ACTIVE로 보이고
  Revoke 버튼이 다시 활성화됨(재클릭 시 백엔드는 멱등이어도 혼란).
- 수정 (Opus 교정): status 덮어쓰기 대신 **낙관적 제거** — ACTIVE 필터 화면에
  REVOKED 유령 행이 남는 것 방지. `onRevoked(keyId)` 콜백을 KeysTable에 내려
  KeysListView가 `items`에서 `filter`로 제거.

### `/keys` — F-K2 (버그, 낮음) 검색 submit 시 status 필터 유실

- 검색 form(`method="GET"`)에 status hidden input이 없어 email 검색하면
  `?email=…`만 남고 status가 기본 ACTIVE로 리셋. status pill은 email을 preserve하는데
  역방향이 안 됨 — 비대칭.
- 수정: form 안에 `<input type="hidden" name="status" value={currentStatus} />` 추가.

### `/apps` — F-A1 (개선, 낮음) 허용 사용자 행 → /users 딥링크

- `allowed_users` 테이블은 read-only. `/users?node=<user_id>` 딥링크 체계가 이미
  있으므로 행 이메일을 Link로 만들면 정책 편집 흐름이 이어짐.
- 수정: Td 내용을 `<Link href={/users?node=${u.user_id}}>`로 감쌈.

### `/` dashboard — F-D1 (개선, 낮음) KPI 카드 → 관련 페이지 링크

- activeKeys → /keys, budgetUtilization → /budgets, activeModels → /models.
- KPICard에 선택적 `href` prop 추가 (없으면 기존 렌더 유지).
- Opus 조건: 링크 변환 시 aria-label·alert 보더·포커스 링 유지 확인.

### `/apps` — F-A2 (버그, 중간 — Opus 발견) default_model이 허용 해제된 모델을 가리켜 고착

- `toggleAppModelAction`은 `routing_profiles.default_model`을 건드리지 않음(백엔드
  확인). 방금 해제한 모델이 default면: select의 `defaultModelInput`이 옵션 밖 값이
  되고, `defaultModelInput === policy.default_model`이라 Save도 disabled →
  콘솔에서 고칠 경로가 없음.
- 수정: 토글 성공 후 입력값이 새 allowed_models에 없으면 서버값으로 재동기화 +
  `default_model`이 허용 목록 밖이면 warning 문구 표시(`defaultModelNotAllowedHint`).

### 검토 후 기각 (의도된 동작)

- `/apps` 모델 토글 즉시저장: 단일-패널 페이지라 staged Apply 불필요 — 유지.
- `/monitoring` 섹션별 독립 Suspense/에러 저하, body logging 최상단 배치: 유지.
- `/analytics` custom 범위 역전 시 안내 문구: 이미 처리됨.
- `/my`, `/cli`, `/chat`: 구조상 이슈 없음.

## 구현 순서

1. F-K1 + F-K2 (같은 파일 묶음)
2. F-A1 + F-D1 (링크 추가)
3. tsc/vitest/build → 커밋 → Opus 구현 리뷰 → 배포
