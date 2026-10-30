# Fresh 전수 리뷰 — awsome-ai-gateway

> 기존 R2~R46 라운드와 무관하게 새 리뷰어 관점으로 서비스별 순회.
> 소스만 대상(테스트 제외), 실제 라인 수는 venv/node_modules 제외.

## 프로젝트 맵

| 서비스 | 파일 | 줄 | 성격 |
|---|---|---|---|
| gateway-proxy | 246 | 61,932 | 인증·라우팅·스트리밍·fallback — 핵심 hot-path |
| admin-api | 201 | 53,635 | 관리 REST API |
| admin-ui | 230 | 34,156 | Next.js 관리 화면 |
| gateway-cli | 51 | 5,996 | 클라이언트 CLI |
| db | 59 | 5,781 | 스키마 + alembic 마이그레이션 |

---

## gateway-proxy — 1차 스윕 결과

### 발견 1 (중) — `trace_extractor.py` 미배선 데드 코드, 209줄

`src/app/services/trace_extractor.py` — 응답 본문에서 reasoning/tool_use
trace를 추출해 cost:stream에 싣는 모듈. 완전히 구현됐고 config 플래그도 있음:

```python
# config.py:214
# trace_enabled: 응답 본문에서 trace 추출 자체를 켤지(미배선 — 추후 hot-path 배선용).
trace_enabled: bool = False
trace_mask_pii: bool = True
```

그러나 호출처가 **0개** — "나중을 위한 코드"로만 존재. 선택지:

- (a) 배선해서 쓰거나
- (b) 쓸 계획이 없다면 파일+config 필드 함께 삭제
- (c) 계획이 있다면 언제 쓸지를 주석에 명시 — "미배선"만으로는 언제 켤지 모름

### 발견 2 (경/정보) — `web_search_loop.py` 4,030줄 / 함수 89개

단일 파일 최대. 다만 helm values의 버전 주석(1.0.56~1.0.78)이 보여주듯
**실사용 장애를 고친 흔적이 누적된 파일** — 크기 자체를 곧장 bloat로 볼 수
없음. 다만 이 파일을 다음에 만질 때는 기능별(검색 루프/도구 정의/이력
재작성/다이제스트/캐시 전략)로 분리 검토 가치가 있음 — 지금 당장 건드릴
필요는 없음.

### 발견 3 (정보) — fallback 처리 비대칭

`routers/messages.py`(Anthropic 방언)에 fallback/retry/circuit 관련 참조
56곳, `routers/openai_compat.py`(OpenAI 방언)에는 6곳. 의도된 비대칭일 수
있으나(OpenAI 방언이 폴백 루프를 안 타나?) 확인 필요 — Codex 경로가
fallback 대상에서 빠져 있다면 조용한 비대칭.

### 발견 4 (정보) — `rate_limit_service.py:494` 미완 TODO

```python
rpm_limit=limit,  # TODO(Step 3): tpm_limit로 전환 + 토큰 가중치
```

RPM 기반 한도만 적용 중 — 토큰 단위 한도는 미구현 상태로 남아 있음.

### 확인된 것 (문제 아님)

- `thinking_normalizer`/`tool_filter` — 예전 "호출처 0" 기록 있었으나 현재
  배선됨(각 2·1 caller)
- `upstream_compat.py`(303줄), `health_checker.py` — 1 caller씩이나 정상 배선

### gateway-proxy 잔여 의문 — 다음 확인 대상

- **두 `budget_service.py`** (proxy 1,377줄 vs admin-api 2,320줄) — 같은
  도메인 로직이 양쪽에 있으면 중복 유지보수 대상. admin-api 라운드에서 대조.
- `messages.py` 1,631줄의 fallback 로직과 `services/fallback_loop.py`/
  `fallback_resolver.py`의 책임 분담 — 라우터에 얼마나 로직이 남아있나.

---

## 아직 안 본 서비스

- admin-api (특히 `budget_service.py` 2,320줄, `chat_agent.py` 1,509줄 — 범위 제외한 admin-chat-agent 관련 코드가 무게를 차지하는지)
- admin-ui
- gateway-cli
- deploy 도구 (docs/NDS/deploy)
- db/migrations

## 스윕에서 안 본 것 (한계 고지)

- 테스트 코드 — 소스만 봄
- 함수 단위 정밀 리드는 상위 파일 위주 — 1,000줄 미만 파일은 구조 스윕만
- Terraform/helm은 deploy 라운드에서

---

## admin-api — 2차 스윕 결과

### 발견 5 (경) — `repositories/audit_repository.py` 고아, 36줄

`AuditRepository`(list_unresolved_failures·list_audit_logs)가 호출처 0 —
실제 audit 기록은 전부 `app.core.audit.audit_logger`로 통한다. 레포지토리
패턴으로 감싼 조회 클래스가 만들어졌으나 서비스에서 안 쓰는 잔재.
삭제하거나, audit 조회 API(G3, 제외됨)를 만들 때까지 보류할지 결정.

### 의문 해소 — 두 `budget_service.py`는 중복 아님

- proxy(1,377줄): BudgetConfig/Scope/Usage 모델로 **hot-path 한도 검사**
- admin-api(2,320줄): seed_spent/check_budget/팀·사용자 예산 관리 + 캐시
  무효화 — **관리·집계 레이어**
역할이 달라 중복 유지보수 대상 아님.

### 확인된 것

- `chat_agent` 라우터는 main.py:696에서 정상 마운트됨(Phase 2 BI assistant)
  — 1,509줄은 코드가 아니라 기능 자체. 범위 제외 결정과 무관하게 배선돼 있음.


---

## admin-ui — 3차 스윕 결과

### 확인된 것 (문제 없음)

- **고아 컴포넌트 없음** — src/components 전 파일이 참조됨
- **console.log 0건** — 디버그 잔재 없음
- "deprecated" 매칭은 모델 폐기(deprecated) 기능 정상 코드

### 덩치 표시 (정보)

- `OrgDetailPanel.tsx` 1,417줄 — useState/useEffect 39개, 조직·팀·정책을 한 패널에서 다루는 무거운 컴포넌트. 결함 아니지만 다음 수정 시 분할 후보
- `chat/` (admin-chat-agent UI) — feature-flagged 기능, 데드코드 아님


---

## gateway-cli — 4차 스윕 결과

**문제 없음** — 총 3,541줄 소스(테스트 제외), 최대 파일 523줄(`oidc_client.py`),
TODO/FIXME 0건. 작고 깔끔한 CLI — 크기나 잔재 문제가 아닙니다.

