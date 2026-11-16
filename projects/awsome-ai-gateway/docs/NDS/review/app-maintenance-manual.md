# 애플리케이션 정비 매뉴얼
# APPLICATION MAINTENANCE MANUAL — LLM Gateway

| 항목 | 내용 |
|---|---|
| 문서번호 | GW-AMM-001 |
| 개정 | A (초판) |
| 효력 기준 | commit `3b7a6c36` (diff 회귀검증 완료 상태) |
| 적용 시스템 | `projects/awsome-ai-gateway` 애플리케이션 전 컴포넌트 |
| 근거 기록 | `admin-api.md` · `gateway-proxy.md` · `admin-ui.md` · `workers.md` · `clients-agents.md` (개별 정비기록부) |
| 검사 형태 | 지상 정밀점검 (정적 코드 검사 + 단위 기능시험). **비행시험(라이브 AWS/EKS 검증) 미실시 — 9장 참조** |

---

## 제1장 — 문서 관리 및 사용법

### 1.1 목적

본 매뉴얼은 LLM Gateway 애플리케이션 컴포넌트에 대해 수행된 정밀점검 결과를
정비 매뉴얼 형식으로 통합 기록한다. 각 결함은 **정정조치기록(CAR)** 으로,
미해결 항목은 **보류결함(DD)** 으로, 검증은 **기능시험(FT)** 으로 관리한다.

### 1.2 심각도 분류 (정비 경고등급 대응)

| 등급 | AMM 대응 | 코드 심각도 | 기준 |
|---|---|---|---|
| **경고 WARNING** | 즉시 정비 없이 운용 불가 | HIGH | 인증 경계 우회·자격 탈취·권한 상승 |
| **주의 CAUTION** | 다음 비행 전 정비 권고 | MEDIUM/MEDIUM-LOW | 장애 오보고·자격 평문 노출·무음 실패·오감지 |
| **참고 NOTE** | 기록 후 주기 관찰 | LOW/INFO | 경계조건 크래시·오분류·정보 부족 |

### 1.3 기록 번호 체계

- `CAR-<구역>-<순번>` — 결함정정기록 (Corrective Action Record)
- `FT-<순번>` — 기능시험 (Functional Test)
- `DD-<순번>` — 보류결함 (Deferred Defect, MEL 상당)
- `VI-<순번>` — 검사합격 항목 (Verified Item — 결함 아님을 확인)

### 1.4 개정 이력

| 개정 | 날짜 | 내용 |
|---|---|---|
| A | — | 5인 독립점검 + 2인 diff 회귀검증 결과 통합 (커밋 `9450630d`·`2da88828`·`3b7a6c36`) |

---

## 제2장 — 적용성 (EFFECTIVITY)

| 구역 | 컴포넌트 | 규모 | 역할 |
|---|---|---|---|
| CH-01 | `gateway-proxy` | ~66k LOC | VK/JWT 인증·인가·레이트리밋·예산·회계·Bedrock 패스스루 |
| CH-02 | `admin-api` | ~61k LOC | 관리 API — 인증/인가·VK 라이프사이클·예산·STS 로그인·query_db |
| CH-03 | `admin-ui` | ~40k LOC | Next.js 15 관리자 콘솔 — OIDC/PKCE·BFF 프록시·CSRF |
| CH-04 | 워커군 | ~12k LOC | cost-recorder·notification·litellm-pricing-lambda·policy-helper |
| CH-05 | 클라이언트/에이전트 | ~24k LOC | gateway-cli(v1)·installer(v2)·cowork-installer·admin-chat-agent·loadtest·mock |

비적용 구역: `docs/NDS/deploy/` 배포 도구(별도 29라운드 정비기록 존재 — 본 매뉴얼 범위 외).

---

## 제3장 — 구역별 정비기록

═══════════════════════════════════════════════════════════════════
### CH-01 GATEWAY-PROXY — 추론 통과 구역
═══════════════════════════════════════════════════════════════════

**시스템 설명**: Pure ASGI 미들웨어 체인(StateInjection→OTel→MaxBodySize→
ClientIdentification→Auth→ClientAuthorization→Budget→Downgrade→Rate-limit→라우터).
Redis(ZSET·예약·스트림)+Postgres 이중 백엔드. 인증 실패는 401(자격 거부)과
503(인프라 장애)로 분기 — 이 분기 정확성이 본 구역의 핵심 정비 항목.

**설계 한계(명문 계약)**: 마스터키 없음·VK sha256 저장·JWT 비대칭 alg만·
클라이언트 Authorization 업스트림 미전달·예산 BOTH_DEGRADED=503.

#### CAR-01-01 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `services/rate_limit_enforcement.py` + `services/budget_service.py` |
| 증상 | `BudgetBackendUnavailableError`(PermissionError 서브클래스)가 범용 `except PermissionError`에 포 — 백엔드 장애가 "예산 초과" 429로 오보고 |
| 영향 | BudgetMiddleware 503 계약 모순·클라이언트 rate-limit 백오프 유발·`budget_exceeded` 메트릭 오염 |
| 정정조치 | sentinel reason `"budget_backend_unavailable"` 인식 → 503 복원 |
| 검증 | FT-02 (796 tests) 통과 |

#### CAR-01-02 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `middleware/auth.py` |
| 증상 | DUAL 경로(`/v1/messages`)의 VK 인증 실패가 JWT 버킷으로 오기록 |
| 영향 | 브루트포스 탐지 버킷 왜곡 — VK 공격이 JWT 카운터에 축적 |
| 정정조치 | `_is_jwt_token` 토큰 형태 분류 |
| 검증 | FT-02 |

#### CAR-01-03 · 참고 · [정정완료 — diff 스윕 발견]

| 항목 | 내용 |
|---|---|
| 위치 | `middleware/auth.py` 인증실패 기록 경로 |
| 증상 | `Authorization: Bearer `(스킴만 있고 토큰 부재)에서 `split(None,1)[1]` IndexError → 500 크래시 |
| 정정조치 | `split()` 후 마지막 파트 추출(부재 시 헤더 전체) |
| 검증 | 회귀 테스트 `test_auth_failure_malformed_header_no_indexerror` |

#### 검사합격 항목 (VI)

- VI-01-01 마스터키/인증 우회 없음 — ADMIN 은 JWT 경로만
- VI-01-02 클라이언트 `Authorization` 업스트림 미전달(Mantle/SigV4 자체 서명)
- VI-01-03 `allowed_models` 조회 실패 fail-closed 503 · Lua 다중 스코프 원자 검사
- VI-01-04 정산 소유권 `usage_fired`+pop · 보안이벤트 source IP=XFF 최우측 홉
- VI-01-05 raw Bedrock 스트림 await 우선순위 · BOTH_DEGRADED fail-closed

#### 보류결함 (DD 등록 — 8장 참조)

DD-03(예약경로 fail-open)·DD-04(VK 이중 DEL 계약)·DD-05(DB_DEGRADED 시
is_active 300s 유지)·DD-06(usage 파싱 실패 시 무기록)·DD-09(claim-based
앱 바인딩)·DD-10(`endpoint_url` SSRF 표면)

═══════════════════════════════════════════════════════════════════
### CH-02 ADMIN-API — 관리 제어 구역
═══════════════════════════════════════════════════════════════════

**시스템 설명**: 관리 API — VK 발급/폐기/이관·예산·사용량·STS 로그인·OIDC
exchange·query_db(채팅 SQL 게이트). Redis 쓰기는 전부 커밋 후, 예산 불변식은
advisory xact lock + savepoint 직렬화.

**설계 한계**: STS 호스트 정확 allowlist·Action 고정·allowlist 빈값=전량거부·
defusedxml·JWT 비대칭만·JWKS issuer 핀·TEAM_LEADER 스코핑 기본-거부.

#### CAR-02-01 · 경고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `services/cli_service.py` STS 요청 검증부 |
| 증상 | `sts_request.url` scheme 미검증 — `http://sts.{region}.amazonaws.com` 이 호스트 allowlist 통과 |
| 영향 | 서버 측 평문 fetch → 경로상 공격자가 위조 `GetCallerIdentityResponse` 로 허용 계정/롤 ARN 주장 → **무인증 VK 발급 + 자동 프로비저닝(STS 경계 전체 우회)**. 본 엔드포인트는 무인증이라 정상 클라이언트 없이 공격 가능 |
| 정정조치 | `scheme != "https"` 거부 + 응답 256KiB 상한 |
| 검증 | 회귀 테스트 2건 (FT-01, 15 tests) |

#### CAR-02-02 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `services/cli_service.py` 자동 프로비저닝 |
| 증상 | 미검증 STS `session_name` 이 신뢰 컬럼 `users.email` 에 적재 |
| 영향 | 공격자가 피해자 이메일 스쿼팅 — verified-email rebind 시 행+VK 통째 이관. 부수: email UNIQUE 충돌 미처리 IntegrityError → 500 |
| 정정조치 | `sts-{sha256(arn)[:16]}@unknown` 합성 주소(OIDC `{sub}@unknown` 동일 계약) — 어떤 실제 이메일과도 충돌 불가 |
| 검증 | 회귀 테스트 1건 (FT-01) |

#### CAR-02-03 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `services/budget_service.py` `/admin/budgets/summary?scope=` |
| 증상 | 무효 scope 값이 `BudgetScope(...)` ValueError → 500 |
| 정정조치 | 프로젝트 `ValidationError` 로 정규화 → 400 |
| 검증 | FT-01 |

#### 검사합격 항목 (VI)

- VI-02-01 `rl_usage_trend` f-string 컬럼 = 검증된 scope 화이트리스트 — 주입 불가
- VI-02-02 `ratelimit:config:*`/`rl:config:*` 양 패밀리 대칭 무효화
- VI-02-03 `seed_spent` savepoint+FOR UPDATE+delta — 멱등·증분 유실 없음
- VI-02-04 internal 엔드포인트 compare_digest+prod 게이트; 422 input 반사 마스킹
- VI-02-05 VK 발급 `expire_and_create` 원자 CTE + 부분 유니크 인덱스 + savepoint 재시도
- VI-02-06 query_db — sqlglot AST 전체 스캔 + 테이블 화이트리스트 + EXPLAIN 상한 +
  강제 LIMIT + 10s timeout + `gateway_chat_reader` 컬럼스코프 SELECT 롤 이중 게이트

#### 보류결함

DD-07(JWKS 30s unknown-kid 창)·DD-08(Cognito reconcile STS 행 제외 — 과거
squat 잔여)·DD-11(`sso_subject` rebind 시 VK 연쇄 폐기 미구현)·
DD-12(무인증/dev 표면 — INTERNAL_API_TOKEN 유출 시 키 발급 가능, 수용 계약)

═══════════════════════════════════════════════════════════════════
### CH-03 ADMIN-UI — 운영자 콘솔 구역
═══════════════════════════════════════════════════════════════════

**시스템 설명**: Next.js 15 App Router(~40k). 자체 OIDC Code+PKCE +
dev-login(환경 플래그). 위협 모델: **"서명 검증은 admin-api"** — UI 게이트는
UX, 인가 경계는 API. 발견 결함은 이 전제가 깨지는 단독 판정 지점에 집중.

**설계 한계**: 쿠키 httpOnly+SameSite=Lax+Secure(HTTPS 시)·CSRF Origin/Referer
호스트 일치+Lax·프록시 화이트리스트 경로·VK 1회 표시.

#### CAR-03-01 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `app/api/client-error/route.ts` |
| 증상 | 이 라우트는 admin-api 를 호출하지 않음 — "upstream 서명 검증" 전제 부성립. 형식+만료만 맞는 **위조 쿠키로 무제한 로그 쓰기**(8KB/req) |
| 영향 | 로그 볼륨 플러딩·저장 비용·관측성 오염 |
| 정정조치 | per-IP fixed-window 레이트리밋 20/min(XFF 우측 홉) + 기존 바디·필드 상한 유지 |
| 검증 | FT-03 (27/27) |

#### CAR-03-02 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `lib/auth.ts` `isSessionExpired` |
| 증상 | exp 파싱 불가 시 **fail-open**(미만료 판정) — 위조/손상 전용 분기가 통과 경로가 됨 |
| 정정조치 | 해석 불가=만료(fail-closed). dev 토큰은 ISO `expires_at` 이 항상 파싱되므로 무영향 |
| 검증 | FT-03 (의도 계약 변경으로 테스트 기대값 갱신) |

#### CAR-03-03 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `components/chat/ReportCard.tsx` |
| 증상 | `window.open(download_url)` 스킴 미검증 — upstream 조작 시 `javascript:` 실행 |
| 정정조치 | URL 파싱 + `https:` 화이트리스트(ChartRenderer 동일) |
| 검증 | FT-03 |

#### CAR-03-04 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `app/budgets/page.tsx` |
| 증상 | `parseJWT` 무방어 — middleware 선검사 의존의 암묵 전제 |
| 정정조치 | try/catch + `session=null` 폴백(403 page 동일 패턴) |
| 검증 | FT-03 |

#### 검사합격 항목 (VI)

- VI-03-01 CSRF — Origin/Referer+Lax 가 서버 액션·logout·dev-login POST 전면 커버
- VI-03-02 오픈 리다이렉트 — callbackUrl/next 부재, `//`·`/\` 거부
- VI-03-03 XSS — ReactMarkdown raw HTML 미사용·img 스트립·https 이미지·DOMParser sanitize
- VI-03-04 SSRF — 전 프록시 화이트리스트·`ADMIN_API_URL` env 전용·경로순회 이중검사
- VI-03-05 VK 1회 표시·sessionStorage 는 채팅 세션 ID 만 · zod 전 admin-mutation 입력

#### 보류결함

DD-13(x-forwarded-proto/Host 신뢰)·DD-14(dev-login prod 가드)·
DD-15(CSP unsafe-inline)·DD-16(서버 액션 ID 추정 가능 — 백엔드 require_admin 의존)

═══════════════════════════════════════════════════════════════════
### CH-04 WORKERS — 후방 처리 구역
═══════════════════════════════════════════════════════════════════

**시스템 설명**: cost-recorder(cost:stream 소비)·notification(알림 fan-out)·
pricing-lambda(모델 가격 수집)·policy-helper(VK 기반 정책 조회).
at-least-once 의미론: 커밋→XACK·request_id UNIQUE·PEL XAUTOCLAIM·
dead-claim 인수·sweeper 펜싱.

#### CAR-04-01 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `notification-worker/handlers/base.py` |
| 증상 | 수신자 전원이 비-인프라 오류로 실패해도 `status="success"` 오집계 |
| 영향 | 알림 계열 무음 사막화 — 발송 실패가 성공으로 집계되어 감지 불가 |
| 정정조치 | `status="failed"` + `all_recipients_failed_non_infra` 로그. 쿨다운은 유지(poison 페이로드 재시도 폭풍 방지) |
| 검증 | FT-04 (91 tests) |

#### CAR-04-02 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `handlers/base.py`·listeners 4곳 |
| 증상 | `str(exc)` 로깅에 SQLAlchemy 바운드 파라미터(수신자 이메일·payload) 노출 |
| 정정조치 | `mask_emails_in_text` 적용 — `dedup_slot_claim_failed`·`db_write_failed`·`event_buffered_infra_error` + diff 스윕 잔여 `channel_listener` 1곳 |
| 검증 | FT-04 |

#### CAR-04-03 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `litellm-pricing-lambda/lambda_function.py` |
| 증상 | `urlopen` 리다이렉트 추종(https→http 다운그레이드로 스킴 강제 우회) + `resp.read()` 무제한 |
| 정정조치 | `_NoRedirect` opener + 16MiB 상한 + 읽기 길이 명시 검사(diff 스윕). fail-soft 응답 유지 |
| 검증 | FT-04 (7 tests) — 스텁 `_OPENER.open` 패치 전환 + 리다이렉트 회귀 테스트 |

#### CAR-04-04 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `cost-recorder-worker`·`notification-worker` `redis_client.py` 모드 감지 probe |
| 증상 | probe 연결에 TLS/timeout 미적용 — ElastiCache TLS 에서 평문 probe 실패 → `standalone` 오감지 |
| 영향 | 클러스터 배포 시 모든 스트림 명령 MOVED 영구 크래시 |
| 정정조치 | probe 에 TLS 플래그 + connect/socket 5s timeout 적용 + 실패 경고 로그 |
| 검증 | FT-04 |

#### CAR-04-05 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `listeners/channel_listener.py`·`config_reload_task.py` |
| 증상 | `unsubscribe` 예외 시 `aclose()` 미도달 — 재시작 주기당 pubsub 연결 orphan |
| 정정조치 | 중첩 try/finally 로 `aclose` 보장 |
| 검증 | FT-04 |

#### CAR-04-06 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `policy-helper` `_NoRedirect` |
| 증상 | VK 전달 차단 로직 무테스트 — 30x 회귀 시 VK 유출 무방비 |
| 정정조치 | 실 로컬 HTTP 서버 리다이렉트 회귀 테스트(Authorization 미전달 검증) |
| 검증 | FT-04 (52 tests) |

#### 보류결함

DD-17(sweeper O(N×테이블) — 인덱스 후속)·DD-18(send→commit 갭 중복 메일 —
at-least-once 설계 한계)·DD-19(internal_api_sender timeout 하드코딩·3xx 영구
rejected)·DD-20(_PER_APP_CLIENTS 수동 동기)·DD-21(버퍼 초과·종료 중 재큐
메모리 유실)·DD-22(MAXLEN 백스톱 미배달 절단 — `stream_gap_detected` 가 유일 신호)

═══════════════════════════════════════════════════════════════════
### CH-05 CLIENTS/AGENTS — 단말·설치 구역
═══════════════════════════════════════════════════════════════════

**시스템 설명**: gateway-cli v1·installer v2(PyInstaller)·cowork-installer·
admin-chat-agent(AgentCore)·query_db 게이트·loadtest·mock. OTEL bearer
토큰의 저장 계층(0644 managed vs 0600 user)이 본 구역의 핵심 정비 사건 —
수정 자체가 마이그레이션 회귀군을 낳아 diff 스윕까지 요구됐다.

#### CAR-05-01 · 주의 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `installer/.../cli/managed.py`·`manifest.py` |
| 증상 | `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>` 이 0644 managed-settings.json + `managed-settings.d/50-gateway.json` 에 기록 — 로컬 전 사용자가 공유 수집 자격 열람 |
| 정정조치 | manifest 출력을 USER 전용으로 → user settings.env 에만 기록 |
| 검증 | FT-05 (202 tests) — 골든 테스트를 신계약으로 갱신 |

#### CAR-05-02 · 주의 · [정정완료 — diff 스윕]

| 항목 | 내용 |
|---|---|
| 위치 | `installer/.../cli/managed.py` merge 경로 |
| 증상 | **업그레이드 잔존**: pre-fix 빌드가 심어둔 managed bearer 는 env merge 가 덧붙이기만 하고 marker `envKeys` 재기록 없음 → 0644 파일에 영구 잔존 |
| 정정조치 | `merged_env` 에서 해당 키 명시 스크럽. site-extra `managed.env` 명시 핀은 운영자 선택으로 복원(주석화) |
| 검증 | 회귀 테스트 2건 (`test_write_scrubs_stale_otel_bearer`·site-extra pin) |

#### CAR-05-03 · 주의 · [정정완료 — diff 스윕]

| 항목 | 내용 |
|---|---|
| 위치 | `installer/.../cli/main.py:disable_cmd` |
| 증상 | bearer 가 user tier 로 이동하며 plain `disable` 이 자격을 남김 — 수정 전 계약(disable=자격 제거) 회귀 |
| 정정조치 | `teardown.remove_user_otel_header()` — 그 키만 선택 제거(토큰 캐시·나머지 user 키는 재활성을 위해 유지, 전체 정리는 `clear`) |
| 검증 | 회귀 테스트 5건 (test_teardown.py) |

#### CAR-05-04 · 주의 · [정정완료 — diff 스윕]

| 항목 | 내용 |
|---|---|
| 위치 | `gateway-cli/src/cli/managed.py` (legacy v1) |
| 증상 | v1 도 managed env 에 bearer 기록 — 같은 노출 결함 드리프트 |
| 정정조치 | managed env 제거 + user-tier 수동 설정 경고(v1 은 user-tier 라이터 없음 — tools/otel_config 의 settings.json 경로만 정상). v1 은 전체 재작성 방식이라 stale 잔존 없음 확인 |
| 검증 | FT-05 (v1 161 tests) |

#### CAR-05-05 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `gateway_cli_oidc/oidc_client.py`×2(installer·cowork)·`cli/login.py`×2 |
| 증상 | 토큰/VK 캐시 `O_TRUNC` 인플레이스 쓰기 — 크래시 시 잘린 파일 → 재인증 강제 |
| 정정조치 | tmp(pid 접미사)+flush+fsync+`os.replace` 원자 패턴 통일 |
| 검증 | FT-05 |

#### CAR-05-06 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `installer/.../cli/main.py`·`setup.py`·v1 `setup.py` |
| 증상 | OTEL 엔드포인트 자동 유도가 `http://host:80` 고정 — https 배포에서 bearer+프롬프트 로그 평문. 부수: `hostname=None` 시 `https://None:443` 생성 |
| 정정조치 | 게이트웨이 스킴 추종(https→443/http→80) + hostname None 가드 양쪽. site-extra·`--otel-endpoint` 명시 경로 불변 |
| 검증 | FT-05 |

#### CAR-05-07 · 참고 · [정정완료]

| 항목 | 내용 |
|---|---|
| 위치 | `admin-chat-agent/scripts/create_code_interpreter.py` |
| 증상 | 외부계정 dev role ARN(`123456789012`)이 기본값 — prod 무수정 실행 시 타 계정 role 로 인터프리터 생성 |
| 정정조치 | `--role` 필수화 |
| 검증 | FT-05 |

#### 검사합격 항목 (VI)

- VI-05-01 STS — 정확-호스트 allowlist·Action 고정·리다이렉트 미추적·defusedxml·fail-closed allowlist·IAM-user ARN 거부
- VI-05-02 OIDC — PKCE(S256)+state+localhost 콜백 · JWKS RS 계열·iss/aud·`email_verified`·exchange 10 RPM/IP
- VI-05-03 로컬 토큰 — `os.open(0o600)`+재chmod·logout 양쪽 삭제·로그 마스킹
- VI-05-04 Cowork managed — HKLM 정적자격 거부·스냅샷 소유/경로 검증·ProgramData ACL·hash-pinned wheel·오프라인 payload
- VI-05-05 query_db — 파서+DB 롤 이중 게이트, multi-statement·writable CTE·`SELECT INTO`·`FOR UPDATE`·확장함수·미커버테이블 우회 전부 차단
- VI-05-06 `seed_dev_data` import 시점 `ENVIRONMENT != "dev"` 거부 · `db/init/*.sql` NOLOGIN·컬럼스코프 SELECT·statement_timeout

#### 보류결함

DD-01(클라이언트 자격 URL 평문 — 제품 결정)·DD-02(unsigned installer)·
DD-23(`--api-key` argv 노출, 수용)

---

## 제4장 — 종합 정정조치 기록부 (CAR 로그)

| CAR | 구역 | 등급 | 결함 | 상태 |
|---|---|---|---|---|
| CAR-01-01 | proxy | 주의 | 백엔드 장애→429 오분류 | ✅ 정정 |
| CAR-01-02 | proxy | 참고 | VK 실패 JWT 오기록 | ✅ 정정 |
| CAR-01-03 | proxy | 참고 | `Bearer ` IndexError→500 | ✅ 정정 |
| CAR-02-01 | admin-api | **경고** | STS http 스킴 → 위조 VK 발급 | ✅ 정정 |
| CAR-02-02 | admin-api | 주의 | session_name 이메일 스쿼팅 | ✅ 정정 |
| CAR-02-03 | admin-api | 참고 | scope ValueError→500 | ✅ 정정 |
| CAR-03-01 | admin-ui | 주의 | 위조 JWT 로그 플러딩 | ✅ 정정 |
| CAR-03-02 | admin-ui | 참고 | exp 파싱불가 fail-open | ✅ 정정 |
| CAR-03-03 | admin-ui | 참고 | window.open 스킴 미검증 | ✅ 정정 |
| CAR-03-04 | admin-ui | 참고 | parseJWT 무방어 | ✅ 정정 |
| CAR-04-01 | workers | 주의 | 전원 실패→success 오집계 | ✅ 정정 |
| CAR-04-02 | workers | 주의 | 바운드 파라미터 이메일 로그 노출 | ✅ 정정 |
| CAR-04-03 | workers | 주의 | 리다이렉트 추종+무제한 read | ✅ 정정 |
| CAR-04-04 | workers | 주의 | probe TLS/timeout 미적용→오감지 | ✅ 정정 |
| CAR-04-05 | workers | 참고 | unsubscribe 실패 시 aclose orphan | ✅ 정정 |
| CAR-04-06 | workers | 참고 | _NoRedirect 무테스트 | ✅ 정정 |
| CAR-05-01 | clients | 주의 | bearer→0644 managed | ✅ 정정 |
| CAR-05-02 | clients | 주의 | 업그레이드 stale 잔존 | ✅ 정정 |
| CAR-05-03 | clients | 주의 | disable 자격 잔존 | ✅ 정정 |
| CAR-05-04 | clients | 주의 | v1 동일 결함 | ✅ 정정 |
| CAR-05-05 | clients | 참고 | 캐시 비원자 쓰기 | ✅ 정정 |
| CAR-05-06 | clients | 참고 | OTEL http 고정+hostname None | ✅ 정정 |
| CAR-05-07 | clients | 참고 | 외부계정 dev ARN 기본값 | ✅ 정정 |

**집계**: 경고 1 · 주의 11 · 참고 11 — **전량 정정 완료**

---

## 제5장 — 기능시험 기록부 (FT)

| FT | 구역 | 시험 | 결과 |
|---|---|---|---|
| FT-01 | admin-api | `tests/services/test_cli_service.py` | **15 pass** (회귀 +3) |
| FT-02 | gateway-proxy | `tests/unit/` 전체 | **796 pass** (회귀 +1) |
| FT-03 | admin-ui | `vitest middleware` + tsc | **27/27** + 타입체크 |
| FT-04 | workers | notification 91·cost-recorder 58·policy-helper 52·pricing 7 | **208 pass**(skip 16: 통합/실 AWS) |
| FT-05 | clients | installer-v2 202·v1 161·cowork 90 | **453 pass** (skip 6) |

---

## 제6장 — 보류결함 목록 (DEFERRED DEFECTS — MEL 상당)

> 다음 비행(운용) 가능 조건이 명시된 결함. 조건 위반 시 운용 제한 검토.

| DD | 결함 | 운용 조건 |
|---|---|---|
| DD-01 | 클라이언트 자격 URL `http://` 허용(issuer/admin-api/gateway) | 문서화된 내부 ALB 기본값과 충돌하는 정책 — **외부 노출 구간 사용 금지**, TLS 전환 후 https fail-fast+opt-in 검토 |
| DD-02 | unsigned installer 경고만 | 배포 정책으로 서명 필수화 시 해소 |
| DD-03 | 예약경로 비-Permission 예외 fail-open 통과 — 월 하드캡 check-only 회귀 창 | 로그·메트릭 감시, 동시성 초과 허용 수용 |
| DD-04 | VK revoke `key:vk:*`/`key:cache:vk:*` 이중 DEL 순서 = admin-api 책임 | 매핑 우선 삭제 계약 유지 — admin-api 변경 시 재점검 |
| DD-05 | DB_DEGRADED 시 캐시 VK `is_active` 미재확인 — 비활성 계정 최대 300s 인증 | 캐시 TTL 상한 내 수용 |
| DD-06 | 성공 2xx + usage 파싱 실패 → usage_logs 무기록 | `unparsed_usage` 플래그 행 도입 권장 — 청구 대조 공백 |
| DD-07 | JWKS 강제 리프레시 30s 쓰로틀 — IdP 롤오버 직후 30s unknown-kid 401 창 | IdP 롤오버 운영 절차로 수용 |
| DD-08 | Cognito reconcile STS 행 제외 — 과거 squat 행 자동 정리 안 됨 | 본 수정 이후 신규 생성 불가 — 운영자 수동 점검 필요 |
| DD-09 | 모델×앱 바인딩이 헤더 주장(`User-Agent`/`anthropic-client-platform`)에 의존 | **의도된 설계**(코드 위협모델 명시). 실경계로 쓰려면 VK 발급 시 `allowed_clients` 강제 — 제품 결정 |
| DD-10 | `model_aliases.endpoint_url` = admin 설정값 — admin-api 침해 시 SSRF 경로 | 엔드포인트 allowlist 권장 |
| DD-11 | `sso_subject` rebind 시 기존 ACTIVE VK 연쇄 폐기 미구현 | 다른 rebind 경로 도입 시 재검토 |
| DD-12 | `/test/issue-key`·`/cli/downloads` dev 표면 — prod 게이트 + INTERNAL_API_TOKEN 유출 시 발급 가능 | 수용된 계약 — 토큰 관리 절차로 커버 |
| DD-13 | admin-ui `x-forwarded-proto`/`Host` 신뢰 | 프록시 우회 도달 시 Secure 미부여 — env 명시 실패 시 거부 권장 |
| DD-14 | `DEV_LOGIN_ENABLED=true` prod 도달 시 비인증 ADMIN 발급 | 부팅 경고/prod 가드 권장 |
| DD-15 | CSP `unsafe-inline`/`unsafe-eval` | 신규 렌더 sink 추가 시 재점검 |
| DD-16 | 서버 액션 ID 빌드 해시 추정 가능 | 백엔드 `require_admin` 누락 방지 의존 — 리뷰 체크리스트 |
| DD-17 | sweeper 클레임 스캔 O(N×테이블) | `(status,created_at)` 부분 인덱스 후속 마이그레이션 |
| DD-18 | 알림 발송→finalize commit 갭 → sweeper 재발송 중복 메일 | at-least-once 설계 한계 — 하트비트로 축소 가능 |
| DD-19 | `internal_api_sender` timeout 10s 하드코딩·3xx→영구 rejected | 설정 튜너블 도입 권장 |
| DD-20 | `_PER_APP_CLIENTS` 수동 이중 유지 | 게이트웨이와 동기 — 어긋나면 예산 행 부재/고아 |
| DD-21 | 버퍼(1000) 초과분·종료 중 재큐 메모리 유실 | 유실 허용선 문서화 확인 |
| DD-22 | MAXLEN 백스톱 미배달 entry 절단 | `stream_gap_detected` 알람 모니터로 커버 |
| DD-23 | `--api-key` argv 노출 | 수용 리스크 — 로컬 전용 |
| DD-24 | staging 버킷 runtime role 전체 RW | 세션 prefix 격리가 앱 계층 — 버킷 정책 강화 권장 |

---

## 제7장 — 정기 점검 항목 (PERIODIC INSPECTION)

| 주기 | 점검 | 기준 |
|---|---|---|
| 커밋마다 | 전 컴포넌트 단위 스위트(FT-01~05) | 전량 통과 |
| 배포 전 | 신규 CAR 여부 확인 — 미등록 변경분은 diff 회귀검증 | 독립 리뷰 통과 |
| 운영 | DD 목록 운용조건 준수 확인 — 특히 DD-01(외부 노출)·DD-09(경계화 판단)·DD-17(인덱스 후속) | 분기별 |
| 마이그레이션 | pre-fix bearer 심은 0644 managed 파일 — 재setup 또는 disable 로 제거 확인 | 업그레이드 1회 |

---

## 제8장 — 비행시험 미실시 항목 (미검증 영역)

> ⚠️ **본 매뉴얼의 모든 정정은 지상 점검(정적·단위) 완료 상태다.**
> 다음 항목은 실제 AWS/EKS 환경에서의 왕복 검증이 별도로 필요하다 — 정적
> 검사로는 발견 불가능한 결함 계열(실 API 응답 형태·실 Redis/ElastiCache
> TLS·실 IdP 상호작용·설치→clear 왕복)이 잔존한다.

| 항목 | 시험 내용 |
|---|---|
| FT-LIVE-01 | bootstrap → apply → capture → doctor → teardown 왕복 |
| FT-LIVE-02 | STS CLI 로그인 왕복(실 STS + 허용 계정) · OIDC 실IdP · dev-login |
| FT-LIVE-03 | VK 발급→/v1/messages 호출→비용 기록→집계 왕복 |
| FT-LIVE-04 | ElastiCache TLS/Cluster 모드 — probe 정상 감지·스트림 소비·알림 발송 |
| FT-LIVE-05 | installer setup→disable→clear 왕복(managed·user tier·토큰 캐시) |
| FT-LIVE-06 | Bedrock Converse·Mantle·크로스계정 SigV4 실제 호출 |

---

## 검수 확인서 (CERTIFICATION)

| 항목 | 내용 |
|---|---|
| 정비 내용 | 애플리케이션 전 컴포넌트 정밀점검 — CAR 23건(경고1·주의11·참고11) 전량 정정 |
| 검증 | FT-01~05 전량 통과 · 독립 diff 회귀검증 2인 완료 |
| 잔여 | DD 24건(운용조건 명시)·비행시험 6건 미실시 |
| 판정 | **지상 검사 기준 정비 완료 — 비행시험(라이브 AWS/EKS 검증) 후 최종 출고 판정** |

*근거 문서: `admin-api.md`·`gateway-proxy.md`·`admin-ui.md`·`workers.md`·
`clients-agents.md` (본 디렉터리 개별 정비기록부) · 커밋 `9450630d`·`2da88828`·`3b7a6c36`*
