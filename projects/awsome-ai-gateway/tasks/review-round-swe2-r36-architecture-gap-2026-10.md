# R36 — 아키텍처 재조망 + 프로덕션 기능 갭 분석

날짜: 2026-10 · 대상: `ARCHITECTURE.md` 전수 + feature 플래그 배선 상태 + 라우터/페이지 맵 대조.
목적: 결함 재점검이 아니라 **"프로덕션 LLM 게이트웨이에 없는 것"** 열거 — 후속 R37~R42의 코드 재리뷰와 분리된 관점.

## 아키텍처 정합 확인

- 미들웨어 체인·cost 파이프라인·인증 모델·3-client 라우팅·resilience 표는 코드와 일치(R20~R25 대조 재확인).
- `body_logging`은 "미지원"이 아니라 **compose만 미지원** — ecs/eks 경로는 `enable_body_logging`→terraform 모듈(Firehose/S3/IRSA)로 배선됨. renderer note가 정직.
- `bi_insight`는 NDS 배포 대상이 아니라 명시적 범위 제외 — chat-agent는 별도 경로. 플래그 존재 자체는 정직한 note로 표기됨.
- `/rate-limits` 페이지는 `/users` 이전 후 의도된 redirect + `?from=` 안내 배너 — orphan 아님.
- 사이드바 권한표가 백엔드 `require_admin`과 정합(analytics/cli/chat 권한 주석으로 근거 명시).

## 수정 — 문서 stale

| 파일 | 관찰 | 조치 |
|---|---|---|
| `ARCHITECTURE.md` ×3 | "현재 head `0022`" — 실제 head는 `0048` (0022는 라우팅 이력 서술이라 본문 체인은 유지) | head 표기 0048로 정정(§3.1·§9 라우팅표·§15) |

## 기능 갭 (우선순위)

### G1 [HIGH] 알림 룰 부재 — "볼 수는 있고 알림은 없다"

- `config/prometheus/prometheus.yml`에 **alert rule 0건**. Grafana 대시보드 5개(usage/operations/roi/claude-code×2)는 조회 전용.
- 알림 인프라는 이미 존재: `notifications:{budget,key,security,system}` pub/sub → notification-worker → SES/SMTP. **alert source만 비어있음**.
- 필요 룰 후보: 5xx 비율, `cost:stream` consumer lag/PEL 성장, `dead_letter_dropped`>0, Redis/DB DEGRADED 상태, pod restart, 무효 VK 폭주(security event), Bedrock 429/5xx 비율.
- compose/grafana alert contact point든 Alertmanager든 — 경로 선택 필요.

### G2 [HIGH] Routing profile 변경이 SQL 전용

- `routers/routing.py`는 `GET ""`(목록) + `PUT /{client}/web-search`뿐 — `account_role_arn`/`backend`/`default_model`/`region` 변경은 **손 SQL + 5분 캐시 대기**(operations.md §3이 정직하게 문서화).
- 프로덕션 요구: admin API CRUD + `routing_profile:{client}` 캐시 즉시 무효화 + audit_log 기록 + UI. 롤백(in-account 복귀)이 운영 절차의 일부라면 SQL 작업은 장애 대응 시간을 늘린다.

### G3 [MED] 관리자 액션 감사 로그 조회면 부재

- `audit.audit_logs`에 `audit_logger`로 기록되지만 조회 API/UI 없음 — `audit_reconcile`은 Bedrock invocation log 대조(별개 목적).
- "누가 언제 예산/모델/키를 바꿨나"를 볼 화면이 없으면 포렌식·내부 감사 요구에 SQL 대응만 남음. `GET /admin/audit/logs` + 필터(액터/액션/기간) 정도면 충분.

### G4 [MED] DEVELOPER 셀프 VK 관리 UI 부재

- `/my`는 사용량+예산만. VK 발급은 CLI(api-key-helper OIDC→exchange) 전용, `/keys`는 ADMIN 전체 관리용.
- 본인 키 목록·만료일·폐기를 UI에서 못 하면: (a) 만료 예측 불가 → 갑자기 401, (b) 유출 시 본인 폐기 불가 → admin 티켓. `GET/DELETE /admin/my/keys` 계열 + `/my` 섹션 추가가 자연스러움.

### G5 [MED] DR/복구 런북 부재

- `operations.md`에 백업 **체크리스트 항목**만 — restore 절차·RTO/RPO·Aurora PITR 검증·`.env` 시크릿 유실 시 복구 시나리오(VK 재발급 절차 등) 없음. 백업이 있어도 복구 리허설 문서가 없으면 백업은 미검증 상태.

### G6 [선택] 비용 이상 탐지

- 예산 threshold(80/90/100)만 — 일 사용량 급증·이상 패턴 탐지 없음. `daily_aggregates` 기반 간단한 spike detector(전주 대비 N배)로 시작 가능. 알림 채널은 G1과 공유.

### G7 [범위 결정] 콘텐츠 안전/PII

- Bedrock Guardrails·PII 마스킹 연동 없음. body_logging은 존재하므로 "로깅은 되는데 가리지 않음" — 요청 본문에 개인정보가 들어가는 조직이라면 body_log 활성화 자체가 리스크. 정책 결정 필요(마스킹 필드 allowlist 등).

## 갭 아님 — 이미 있는 것으로 오인하기 쉬운 것

- **서버사이드 웹서치**: routing profile `web_search_enabled` + AgentCore 연동으로 구현 완료(§8).
- **fallback**: cross-account 실패 시 in-account 투명 폴백 + 게이트웨이 자체 fallback loop 존재.
- **body_logging**: 게이트웨이 코드 + ecs/eks 배선 완료(compose만 해당 없음).
- **audit 기록**: `audit_logger` 존재 — 부재는 "조회면"이지 기록이 아님(G3).
- **서비스 토큰**: `service_tokens` 라우터 + 회전 상한 로직 존재.
- **export**: `/admin/analytics/export` 존재(group_by/scope 정합은 이전 라운드 수정 완료).

## 후속 라운드 지시사항

- R37~R42는 이 갭 목록을 **기능 추가 요구로 오인하지 말 것** — 코드 재리뷰(결함) + UX 표면만.
- G1~G7은 R43 종합 리포트에서 구현 우선순위·공수 추정과 함께 사용자에게 제안.
