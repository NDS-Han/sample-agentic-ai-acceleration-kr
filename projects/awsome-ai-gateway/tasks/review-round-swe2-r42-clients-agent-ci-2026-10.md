# R42 — 클라이언트 계약 + admin-chat-agent + CI 2차

날짜: 2026-10 · 대상: gateway-proxy 클라이언트 대면 라우터(messages·openai_compat·
usage·health·bedrock), admin-chat-agent 전체(agent·lambdas·tests),
gateway-clients/, .github/workflows/awsome-ai-gateway-ci.yml.
베이스라인: chat-agent unit 275, CI 워크플로 자체는 미실행(수동 디스패치 전용).

## 수정

### 1. `pytest.mark.unit` 미등록 마커 (admin-chat-agent/pyproject.toml)

tests/unit 전반이 `@pytest.mark.unit`을 쓰는데 markers 미등록으로 매 실행마다
PytestUnknownMarkWarning 8건. R38에서 admin-api에 적용한 것과 동일하게 등록 —
경고 0건으로 수렴(미등록 마커는 -m 선택자도 조용히 무시되므로 실질 결함).

### 2. CI 워크플로 에러 메시지의 stale 파일명 (awsome-ai-gateway-ci.yml)

lint-ratchet의 ::error/::notice 안내가 `.github/workflows/ci.yml`을 가리키는데
실제 파일은 `awsome-ai-gateway-ci.yml` — baseline 갱신 지시를 따라갈 수 없었다.
2곳 정정.

## 검증된 정상 (변경 없음)

### 클라이언트 계약

- **에러 봉투 계열별 정합**: `/v1/messages`는 `anthropic_error()` —
  `{"type":"error","error":{"type":"invalid_request_error",...}}`(Anthropic형).
  openai_compat은 `{"error":{"type":...,"message":...}}`(OpenAI형). 각 계열이
  기대하는 파서에 맞음.
- **SSE 이벤트**: message_start / content_block_start·delta·stop /
  message_delta / message_stop / ping — Anthropic 스트리밍 프로토콜 전 집합.
- **`/v1/models/{id}`**: Claude Code가 세션 시작 시 호출하는 모델 가용성 검증을
  지원(docstring에 계약 명시) — 없으면 클라이언트가 /v1/messages 자체를 안 보냄.
  DB-degraded 경로도 캐시 폴백으로 서빙.
- **`/v1/responses` (Codex)**: per-request 모델 해석 — untrusted `model` 필드의
  3종 이형 입력(non-str·lone surrogate·NUL 바이트)을 resolver 도달 전 차단,
  INACTIVE alias는 폴백 없이 거부(킬스위치 의미 보존), 미등록 이름은 프로필
  기본값 폴백(기존 클라이언트 무회귀), infra 장애(DBAPIError connection
  invalidated)는 503으로 구분 — 조용한 잘못된 모델 서빙 방지.
- **`/v1/usage/team/{id}`**: R37에서 DUAL auth로 수정됨 — TEAM_LEADER VK 접근 복원.
- **client 분류**: `codex_desktop` originator 헤더 → `startswith("codex")` 정상 분류
  (실측된 Codex Desktop config와 일치).
- **`count_tokens`**: 무료지만 budget 미들웨어 통과 — 예산 초과 시 카운팅도 차단되는
  의도된 게이트(예약/차감 없음 확인).
- **gateway-clients/**: claude-box·codex-box 컨테이너 래퍼 + gw.sh + 문서 —
  R34 범위, 이후 delta 없음.

### admin-chat-agent (R17 이후 delta = R18 경화 커밋뿐, 2차 재검증)

- **sql_guard.py**(정확도 안전망): 컬럼 한정자·TZ 앵커·SUM 전 fan-out 조인·
  GROUP BY 일관·대시보드 status 필터 정합 — 에러는 거부+LLM 자가수정 루프,
  경고는 fail-soft로 결과에 동봉.
- **lambdas/query_db**(실행 경계, 7단계): 단일 문장 강제·SELECT-계열 루트만·
  DDL/DML/세션/LOCK/SELECT INTO 거부·PostgreSQL 식별자 정규화·테이블/컬럼
  허용목록+금지컬럼·LIMIT 강제·EXPLAIN 비용 상한·statement_timeout 10s·
  읽기전용 역할.
- **main.py**(AgentCore 엔트리): LAMBDA_QUERY_DB/LAMBDA_GET_SCHEMA 부재 시
  fail-closed(교차환경 호출 방지), query_db는 모델 제공 session_id 무시하고
  contextvar 값 강제, boto3 람다 클라이언트 모듈 싱글턴(병렬 candidate 안전),
  specialist 에이전트는 호출마다 팩토리 생성(히스토리 누출·프롬프트 인젝션
  지속 방지).
- **candidate_select**: 컬럼 alias/순서 무시 값 multiset 비교 — 후보 합의의
  거짓 분열 감소.
- **S3 스테이징**: session/step ID sanitize + UUID 8자 suffix(정규화 충돌 방지).
- **compute_stats**: 전행 스캔으로 수치 컬럼 발견(첫행 NULL 오판 방지).
- **테스트**: unit 275 pass — scoring·후보선택·sql_struct·sql_guard·골든·
  heartbeat 스트리밍·응답 감사·차트 스펙·reconcile·few-shot·JSON 파싱·봉투.

### CI 워크플로 (전수 검토 — 최초 정식 라운드)

- **최소 권한**: `permissions: contents: read`, workflow_dispatch 전용(의도),
  `pull_request_target` 없음, AWS 자격증명 어디에도 없음.
- **재현성**: `uv sync --frozen` + `npm ci` — lock 고정.
- **실 PG 증명 잡**: pgvector 이미지(stock postgres는 0005 마이그레이션 실패),
  DB명 `proof` 가드 인지, **통과 수 하한**(silent skip 검출) — 실측 기준선 주석.
- **ruff ratchet**: baseline 초과 실패 + **파싱 붕괴 대조군**(COUNT==0인데
  baseline>0이면 실패 — 게이트 자체의 공허화 방지).
- **매트릭스 커버리지**: 6개 python 서비스 + db/tests + deploy 도구 + helm 렌더
  + admin-ui(vitest+tsc+eslint 하드게이트). 참조 경로·uv.lock 전부 존재 확인.
- **헤더 주석**: 각 잡의 존재 이유를 실제 사고 이력으로 문서화(모델×앱 게이트
  무력화 사건, 스위트 RED 방치 사건 등) — 삭제 시 맥락 유실 없음.

## 판정 보류

- **Actions 태그 핀**: 서드파티 액션이 major 태그(v4/v5) — SHA 핀이 더 엄밀하나
  major 태그는 표준 관행. 공급망 강화 원하면 별건.
- **compose 통합 테스트 CI 부재**: 의도된 skip — postgres:16-alpine에 pgvector
  없어 0005 실패하는 실제 이유가 주석으로 문서화됨. 별도 라이브 스택 잡은
  프로덕션 결정 사항.
- **`bi_insight` 플래그**: 세 렌더러 모두 미구현을 정직하게 노트로 보고.
  chat-agent 자체는 존재하나 NDS 배포 경로에 프로비저닝이 없음 — R43 갭 목록행.
- **chat-agent의 ARM64 전용 + us-east-1 AgentCore**: 리전 중립 목표와 충돌
  가능 — 배포 대상 결정 시 재평가.
