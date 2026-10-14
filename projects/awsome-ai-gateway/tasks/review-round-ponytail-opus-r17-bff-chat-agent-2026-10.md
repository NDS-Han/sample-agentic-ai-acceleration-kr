# R17 — admin-ui BFF / 미들웨어 / 서버액션 + admin-chat-agent 심층 리뷰 (2026-10)

리뷰어: kiro `claude-opus` (read-only) + 자체 코드 추적 및 sqlglot 실측 검증.
대상: admin-ui BFF 프록시 라우트(chat-proxy/teams-proxy/dashboard/model-share/
client-share/analytics-export/cli-download/client-error/auth callback),
middleware(보안 헤더/CSRF/인증 게이트), 서버 액션 경로 보간, admin-chat-agent
(query_db Lambda SQL 검증, S3 staging, Code Interpreter, sub-agent 수명),
admin-api chat_agent 라우터(SSE 릴레이, 세션 소유/만료), admin-ui SSE 클라이언트.

## 발견 및 조치

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| M1 | MUST | **Next.js 14.2.29** — 2026-05 보안 릴리스에서 13.x/14.x 전 버전이 취약으로 분류, 백포트 없음 | Next **15.5.27** + React **19.3.0** 업그레이드. @next/codemod async-request-api 변환(21파일) 후 남은 동기 접근 수동 수정(analytics page parseFilter, 라우트 테스트 Promise 래핑). `cookies()` unwrapping 캐스트 제거하고 `await` 정식 사용 |
| M2 | MUST | **비인증 경로 무제한 body 버퍼링** — `/api/*` 는 middleware 인증 게이트를 통과하고 라우트가 `req.text()/json()` 으로 무제한 버퍼링 → 비인증 대용량 POST 로 파드 OOM 가능 | `lib/utils/requestBody.ts` 신설 — Content-Length 선차단 + 스트림 누적 바이트 상한(초과 시 cancel+413). chat-proxy/client-error 는 인증 쿠키 확인 **후에만** body 읽기. client-error 는 필드 화이트리스트 + 로그 인젝션 방지로 재작성 |
| S1 | SHOULD+ | **validate_sql 검증 로직 반전** — `if not isinstance(ast, SELECT류)` 안에서만 DML 스캔해 `WITH x AS (DELETE…) SELECT`(root=Select) 우회, `SET`/`DISCARD`/`LISTEN` 등 비-SELECT 문 묵인 통과, `FOR UPDATE` 행 잠금 허용, `SELECT INTO` 테이블 생성 허용, `query_to_xml('select …')` 문자열-SQL 우회(chat_agent GRANT 와 결합 시 타 관리자 채팅 노출), UNION 은 첫 브랜치에만 LIMIT | validate_sql 재작성 — root 엄격 화이트리스트 + DML/DDL 무조건 전체 스캔 + locks/into 거부 + 위험 함수 denylist(pg_*/set_config/dblink/lo_/*to_xml*/xpath/http_*). `exp.Anonymous` 의 sql_name() 함정(실이름은 .name) 수정. forbidden 컬럼 이름 기준 매칭(별칭/비수식 안전). force_limit 은 전체를 서브쿼리로 감싸 총량 상한. CTE 별칭은 테이블 화이트리스트 검사에서 면제. `08_create_chat_reader.sql` 에서 chat_agent 스키마 GRANT/스키마 USAGE 회수(whitelist 와 권한 정합) |
| S2 | SHOULD | Code Interpreter 생성 스크립트 `networkMode: "PUBLIC"` — 나중에 기능 켜는 순간 샌드박스 외부망 개방 | 기본값 `SANDBOX` |
| S3 | SHOULD | 서버 액션 동적 경로 비인코딩 보간 — `../` 정규화로 admin-api 의 임의 경로 호출 가능(사용자 쿠키 첨부된 채) | 41곳 `encodeURIComponent` 적용 + api-client 에 정규화 후 `/admin/` 접두 어설션(chat-proxy 와 동일 계약, 심층 방어) |
| S4 | SHOULD | `window.open(url,'_blank','noopener,noreferrer')` 는 spec 상 null 반환 → fallback 항상 실행; 리포트 HTML 이 외부 img/링크를 그대로 노출 | `window.open('','_blank')` 후 `location.href` 대입 + narrative HTML 살균(외부 href/src/action 제거) + 생성 HTML 에 제한 CSP meta |
| S5 | SHOULD | sub-agent(orchestrator/validator/critic/auditor) 모듈 싱글턴 — invoke 간 history 누적·세션 교차 주입·동시 호출 ConcurrencyException | invoke 별 팩토리(`_new_orchestrator*` 등)로 전환; 테스트는 팩토리 패치로 갱신 |
| S6 | SHOULD | BFF fetch 에 timeout/상류 실패 매핑 없음 — admin-api 행 시 무한 대기 또는 비정형 오류 | `lib/utils/upstream.ts` 공통 헬퍼(timeout + 502)를 teams-proxy/dashboard/rate-limits/analytics-export/chat-proxy 에 적용. api-client 에도 15s 타임아웃 |
| S7 | SHOULD | CSP 이중 정의(middleware + next.config) — 브라우저 교집합 적용으로 실효 정책 추적 불가, img-src 에 S3 presigned 누락 | middleware 단일 소스화 + `img-src https://*.amazonaws.com`(presigned 차트/리포트) + `frame-ancestors 'none'` |
| N1 | NIT | api-client 가 전체 쿠키 전달(oidc_verifier/state 등) | `admin_jwt` 만 전달 |
| N2 | NIT | cli-download os/arch 무검증, 응답 파일명 미살균, 전체 버퍼링 | SAFE_SEGMENT 화이트리스트 + filename sanitization + 스트리밍 |
| N3 | NIT | auth callback 이 token 엔드포인트 예외 상세(내부 DNS/URL)를 브라우저에 노출 | 일반 메시지 + 서버측 로그 |
| N4 | NIT | S3 staging 키 `staging/{session}/{step}.jsonl` 이 LLM 제공값 신뢰 — 타 세션 오염 가능 | agent 측 contextvar session_id 강제 주입 + Lambda 측 sanitization 이중 방어 |
| N5 | NIT | `post_message` 가 expires_at 조회만 하고 미검증 — 만료 세션 사용 계속 가능 | 만료 검사 추가 |
| N6 | NIT | SSE 파서 멀티라인 `data:` 미지원(줄 단순 연결), 버퍼 무상한 | 스펙대로 `\n` join + 4 MiB 버퍼 상한, reattach() 401 리다이렉트 + tail flush |

## 검증

- `validate_sql` sqlglot 실측 26케이스: CTE-DML/SET/LISTEN/DISCARD/FOR UPDATE/
  SELECT INTO/query_to_xml/pg_read_file/lo_import/set_config/dblink/금지 컬럼
  (수식·비수식·별칭)/chat_agent 비화이트리스트/멀티문장 전부 거절. 정상
  SELECT·CTE·UNION(총량 LIMIT)/금지 컬럼 외 전부 통과. CTE 별칭 false-positive
  추가 발견·수정.
- admin-ui vitest **418** 전부 통과(+7 신규: requestBody 5, SSE 멀티라인 2,
  cli-download 차단). `tsc --noEmit` 클린. `next build` 성공(28 페이지).
- admin-chat-agent **183** 통과(테스트는 팩토리 패치로 갱신).
- admin-api **968** 통과.

## kiro 검증 라운드

1라운드 **NOT CONVERGED** — 출력이 잘려 must-fix 1·2번은 복구 불가였으나
(재실행 시 월 사용량 한도 소진), should-fix/잔여 발견 3~22번 전수 조치:

| # | 발견 | 조치 |
|---|---|---|
| 3 | CTE 면제가 스코프 무시(전역 이름 집합) — 스코프 밖 실테이블 우회 | `traverse_scope` 기반 실테이블 열거로 교체 — CTE 는 Scope 로, 실테이블만 Table 로 나타남. 스코프 해석 실패 시 fail closed |
| 4 | 함수명 해석 순서 오류 — 타입드 함수의 `.name` 은 첫 인자(`concat('http_',x)` 오탐) | `Anonymous → .name / 기타 → sql_name()` 분기 |
| 5 | Code Interpreter SANDBOX 가 S3 를 막을 수 있다는 우려 | AWS 문서 실측 — SANDBOX 도 S3 허용(공식 S3 통합 가이드가 SANDBOX 사용). presigned URL exfil 잔여 위험은 execution role 로 상한 |
| 6 | denylist 공백 — ts_stat/ts_rewrite(문자열 SQL), lo_* 쓰기 계열, nextval/setval, txid_* | denylist 확장 |
| 7 | `SELECT u.*`/`row_to_json` 은 Column 노드 없이 우회 | 주석 명시 — 최종 차단선은 DB 컬럼 GRANT |
| 8 | force_limit 서브쿼리 래핑이 ORDER BY 의미를 깸 | Select root 는 제자리 LIMIT, 집합연산만 래핑 |
| 9 | session_id contextvar 미전파 시 LLM 값으로 조용히 fallback | fail closed |
| 10 | expires_at 미연장(활성 대화가 TTL 에 죽음) + 프론트 410 미처리 | post_message 슬라이딩 갱신 + `SessionExpiredError` → 새 세션 자동 생성 후 1회 재전송 |
| 11 | `AbortSignal.timeout` 이 스트림 본문을 중간에 끊음(analytics-export, cli-download) + 호출자 signal 무시 | `fetchUpstream` 을 헤더-전용 타임아웃(setTimeout+clearTimeout)으로 재작성, `AbortSignal.any` 로 호출자 signal 병합(api-client 동일) |
| 12 | client-error "인증"이 쿠키 존재만 확인 — `admin_jwt=x` 로 로그 폭주 가능 | parseJWT + isSessionExpired 검증 추가 |
| 13 | SSE 버퍼 상한 위치(분할 전 검사로 오탐), 미취소 reader, UTF-16 카운팅 | 완결 프레임 드레인 후 잔여만 검사 + overflow 시 reader.cancel() |
| 14 | reportExport `load` 리스너를 navigate 후에 달아 print 경로 사망 가능 | 리스너를 navigate 전에 등록 |
| 15 | `..` 세그먼트가 정규화 후에도 `/admin/` 내 다른 경로 호출 가능 | 원본 path 세그먼트의 `.`/`..` 명시 거부 |
| 16 | next/eslint-config-next `^` 캐럿, lockfile 수작업 흔적 | 정확 버전 핀(15.5.27/19.3.0) + `npm install`+`audit fix` 로 lockfile 재생성 |
| 17~22 nit | stale 주석(Next 14 표기), 테스트 주석 정규식 불일치, 기존 설치본 schema USAGE 미회수, dev-login 무제한 body, timeout→502 오분류, cowork 문서 별칭 불일치 | 주석 정정 / `REVOKE USAGE ON SCHEMA chat_agent` 추가 / dev-login 도 readBoundedBody(4KB) / 타임아웃은 504 로 분류 / 별칭 불일치는 사용자 수정 문서라 보고만 |

sqlglot 실측 26+케이스 + 회귀 테스트 `tests/unit/test_query_db_validation.py`(39건) 추가.

## 수용 잔여(차기)

- `_StreamRelay` 프로세스 로컬 — admin-api 다중 레플리카 시 Redis pub/sub 필요.
- CSP `unsafe-inline`/`unsafe-eval` — Next 인라인 스크립트 구조상 유지.
  nonce 기반 strict-dynamic 은 별도 작업.
- npm audit 잔여(major bump 필요): next→16(postcss 빌드타임 CVE),
  vitest/tinypool(dev-only), next-intl 4(open redirect·precompile —
  두 경로 모두 미사용 API라 비도달). 차기 메이저 정리 시 함께.
- Code Interpreter SANDBOX 에서도 presigned URL 경유 외부 버킷 쓰기는
  가능 — execution role IAM + VPC 엔드포인트 정책이 실질 상한.
- kiro 1라운드 must-fix 1·2번은 출력 절단으로 미확인 — 재실행이
  사용량 한도로 불가해 자체 심층 점검(인증 순서·경로 가드·스트림 처리)으로
  대체. 다음 라운드에서 재검증 권장.
