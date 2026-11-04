# 팀 리더 예산 스코프 + 개발자 페이지 제한 + /my 토큰 분석 — 2026-11

## 배경 조사 결과 (코드 검증 완료)

### A. 팀 리더 → 관리자 예산 변경 (kiro 검증으로 정정)
- USER-scope 쓰기는 백엔드 보호됨: `set_user_budget`(864), `delete_user_budget`(990),
  `set_user_client_budget`(1118), `clear_user_client_budget`(1272), `allocate_team_budget`(1367/1406)
  — 전부 자기팀 + 본인제외 + 리더/어드민 대상 금지(`leader_target_forbidden`)
- TEAM-scope 쓰기 6개 메서드는 서비스층 role 검사가 없지만 **라우터가 전부
  `Depends(require_admin)`**(budgets.py:37/58/79/104 + downgrade 2개 + `/spent`:145)
  — 리더는 도달 자체를 못 함. 실제 공격 경로는 없다
- **실제 관측된 구멍은 UI**: TeamAllocationView `lockRow` 가 본인 행만 잠그고
  리더/어드민 멤버 행의 cap 입력·해제는 활성 — 리더가 입력→제출하면 백엔드가
  403 `leader_target_forbidden` 으로 거절하지만 UI 는 편집 가능해 보인다
- `AllocationEntry.target_role` 이미 응답에 있음(schemas/budgets.py:184)

### B. /my 토큰 분석
- `/admin/my/usage` 는 daily_usage + by_model 만 반환 — 토큰 버킷 분해 없음
- `TokenMixDonutClient` 재사용 (agents 상세와 동일 정의: input/output/cache read/cache write)

### C. DEVELOPER 페이지 제한
- 이미 맞는 것: `/my` 허용(페이지+사이드바), 콜백이 `/` 불가 시 `/my` 랜딩,
  PAGE_PERMISSIONS 미등재 경로 default-deny
- 빠진 것: `/integration`+`/cli` 가 ADMIN+TEAM_LEADER 만 — DEVELOPER 추가 필요
- 백엔드 `/cli/downloads`, `/cli/download/{os}/{arch}` 는 역할 검사 없음 — 개발자 접근 OK
- `/admin/my/*` 는 `get_current_user` — 모든 역할 통과

## 설계

### A-1. UI 잠금 (실제 구멍)
- TeamAllocationView: `!isAdmin` 일 때 `target_role in (TEAM_LEADER, ADMIN)` 인
  행의 cap 입력·해제 버튼 비활성 + 안내 문구 — 백엔드 403 과 같은 조건으로
  미리 잠가 "변경 가능해 보이는" 상태 제거

### A-2. 서비스층 방어심층 (kiro Required)
- TEAM-scope 쓰기 6개 + `seed_spent` 선두에 `actor.role != UserRole.ADMIN →
  BudgetRuleError(msg, "team_budget_admin_only", 403)` — 라우터 Depends 가
  미래에 느슨해져도 서비스가 막는다. ForbiddenError 는 code 고정이라
  spec 코드는 BudgetRuleError 로
- 읽기 경로는 자기팀 스코핑 유지(기존 동작)
- svc 토큰은 **차단하지 않는다** — 예산 쓰기는 자격증명 발급이 아니고
  seed_spent 같은 마이그레이션 자동화가 svc 토큰 용도에 부합
  (audit_logs 에 service_token_id 기록됨 — 사용 추적 가능)

### B-1. /my usage 응답 확장
- `/admin/my/usage` 에 `summary` 추가: input/output/cache_creation/cache_read/
  reasoning_tokens, web_search_count, cost_usd, requests — §59(SUCCESS+KST) 동일 스코프
- 프론트: MyUsageDashboard 에 TokenMixDonutClient 카드(하단) + 일별 차트 비용|토큰 토글

### C-1. DEVELOPER = /my + /integration
- PAGE_PERMISSIONS `/integration`, `/cli` 에 DEVELOPER 추가
- Sidebar BOTTOM integration 항목에 DEVELOPER 추가
- 연동 페이지의 service-VK 샘플(curl/Python $SERVICE_VK)은 admin 전용 발급이라
  개발자에겐 무의미 — DEVELOPER 에게는 "관리자에게 발급 요청" 안내로 대체
- permissions.ts 헤더 주석 정정("DEVELOPER absent from all entries"는 이미 아님)
- /cli/downloads · /cli/download · /admin/my/* · cli-download 프록시는 개발자
  접근 가능(무인증/self-scope) — 별도 변경 없음
- 개발자가 /my 외 admin 경로 직접 호출은 백엔드 require_admin 에서 이미 차단

### 테스트
- test_rbac.py: TEAM_LEADER → TEAM-scope 쓰기 7개 라우트 403 파라미터화
  (라우터 가드 회귀 — Depends 가 느슨해져도 테스트가 잡는다)
- budget_service: TEAM_LEADER actor 로 6개 메서드 → team_budget_admin_only
- TeamAllocationView: 리더 뷰에서 admin/leader 행 입력 비활성
- /my usage: summary 버킷 필드 presence
- audit-changes 미변경

## 미포함(범위 밖)
- 인가 거부(403)의 감사 로그 — 코드베이스에 패턴 없음, 별도 과제
- /my 의 팀 비교/랭킹 같은 신기능
