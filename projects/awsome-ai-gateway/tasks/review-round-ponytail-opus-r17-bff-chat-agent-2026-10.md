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

## Claude 검증 라운드(kiro 월 한도 소진으로 대체 진행)

kiro 사용량 소진 후 Anthropic Claude(Bedrock)로 adversarial review 계속.

### 라운드 1 (`830edb86` diff) — NOT CONVERGED

| # | 발견 | 조치 |
|---|---|---|
| MF1 | `chat_agent.py` `timedelta` 미임포트 — 라이브 챗 post_message 전부 500 | import 추가 + 만료-세션 회귀 테스트 |
| MF2 | `VALUES` 루트가 `traverse_scope` 비대상 → whitelist 우회 (`VALUES ((SELECT … FROM pg_stat_activity))`) | `_ALLOWED_ROOTS` 에서 `exp.Values` 제거 + 미해석 Table 노드 fail-closed |
| M3 | 괄호 서브쿼리 `(...) FOR UPDATE` — Lock 이 Subquery 에 붙어 스캔 우회 | `exp.Lock`/`exp.Into` 전체 AST 스캔 |
| SF4 | users/models/budgets 외 3개 액션 경로 미인코딩 | 전수 조사 + `encodeURIComponent` |
| nits | api-client `%2e`/`#`/`?` 우회, ChatLayout 410 재시도 실패 시 에러 삼켜짐, TextDecoder tail flush 누락 | 정규화 경로 비교로 전환 / 재시도·세션생성 실패 setError / decode() tail flush |

→ `b4341860` 커밋·배포(admin-api·scheduler `b4341860`, admin-ui `33ded004`).

### 라운드 2 — NOT CONVERGED

| # | 발견 | 조치 |
|---|---|---|
| M1 | 커버리지 검사의 전역 `cte_names` 면제 — `WITH a AS (…EXISTS(SELECT … FROM pg_roles x))…` 계열 미해석 노드가 동명 CTE 로 면제 | scope-로컬 해석으로 교체(`scope.sources` 키가 name·alias 모두 포함 확인) |
| S1 | api-client `#` 세그먼트 미거절 | 거절 추가 |

→ `33ded004`. 검증 도중 발생한 일회성 배치 이상 출력은 재현 불가 —
pytest(권위) 46통과 + 단독 재실행 정상으로 확정.

### 라운드 3 — NOT CONVERGED

| # | 발견 | 조치 |
|---|---|---|
| M1 | 식별자 대소문자 폴딩 우회 — `WITH "PG_ROLES" AS (…) SELECT * FROM PG_ROLES` 가 Postgres 에서는 `pg_roles`(인용 없음→소문자 폴딩)를 참조하는데 validator 는 인용 CTE 별칭으로 면제 | `normalize_identifiers(ast, dialect="postgres")` 를 파싱 직후 적용. 인용은 대소문자 보존·비인용은 소문자 폴딩이라 의미론이 Postgres 와 일치 |

→ `16a4ed89`.

### 라운드 4 — CONVERGED

must-fix 0. sqlglot 30.18 소스 실독 검증:
`ASCII_ONLY_NORMALIZATION`(Unicode 폴딩 우회 불가), `branch()` 가 부모
FROM 별칭을 자식에 넘기지 않음(서브쿼리·LATERAL 안전), `walk_in_scope` 가
Query 경계에서 정지(미방문 테이블은 커버리지 fail-closed 에 걸림),
WITH RECURSIVE 전방참조는 오탐 방향으로만 실패.

| # | 잔여 | 조치 |
|---|---|---|
| S1 | 화이트리스트 비교가 양쪽 소문자화 — 인용된 `"USAGE_LOGS"` 가 `usage_logs` 에 매칭(오늘은 해당 릴레이션 부재로 runtime error 로 끝나지만, 동명-대소문자 테이블이 생기면 우회) | 정규화 후 정확 비교로 전환 |
| S2 | 회귀 테스트 보강 | 케이스 6개 추가 → 58통과 |

→ `4b89c43c`. **최종 verdict: CONVERGED.**

### 최종 누계(R17)

- 커밋: `830edb86` → `b4341860` → `33ded004` → `16a4ed89` → `4b89c43c` (push 완료)
- 테스트: chat-agent 241 / admin-ui 418 / admin-api 970 / validator 회귀 58
- 배포: Helm rev 212 · 스모크 12/0 · admin-api/scheduler `b4341860`,
  admin-ui `33ded004`(timedelta 수정 + UI 수정 포함)
- `query_db` Lambda 는 dev 미배포(`LAMBDA_QUERY_DB` 빈 값, terraform
  `enable_chat_agent=false`) — 활성화 시 수정 코드가 함께 배포됨.
  prod 활성화 전 terraform 재실행 필요.

## kiro(Opus 5.5) 추가 라운드 — Claude 수렴 후 재검증

Claude 4라운드 CONVERGED 이후 kiro 토큰 회복, 동일 절차(누적 diff → 검증)
로 라운드 5~6 진행. kiro 가 실측(실제 Postgres 컨테이너 기동 포함)으로
validator 우회 시도 약 80건을 수행 — 전부 방어 확인.

### 라운드 5 — NOT CONVERGED

| # | 발견 | 조치 |
|---|---|---|
| M1 | deep mode 맥락 상실 — 매 invoke 마다 새 orchestrator 생성으로 plan 승인턴이 이전 턴을 못 봄 | 세션별 orchestrator 인스턴스 캐시(`runtimeSessionId` 로 세션 격리 유지, sub-agent 는 호출별 생성 유지) + 회귀 테스트 |
| S1 | CSP `img-src https://*.amazonaws.com` — 마크다운 이미지로 임의 AWS 호스트 유출 경로 | `CHAT_IMAGE_HOSTS` 화이트리스트로 전환(fail-closed 기본) + 마크다운 임의 이미지 렌더 차단 + ChartRenderer https 강제 |
| S2 | whitelist ↔ GRANT 드리프트 — `client`, `reasoning_tokens` 컬럼이 스키마에 있지만 chat_reader 그랜트 누락 | 마이그레이션 `0046_grant_chat_reader_client_reasoning.py` + whitelist↔GRANT 드리프트 회귀 테스트 |
| S3 | `force_limit` 이 `FETCH FIRST n`/`WITH TIES` 를 LIMIT 1000 으로 덮어씀 | `exp.Fetch` 처리 — 비-ties 는 LIMIT 정규화, `WITH TIES` 는 내부 보존 + 외부 상한 |
| N1-N4 | cli-download raw fetch, 주석 근거, `readBoundedBody` 예외, 410 재시도 맥락 소실 고지 | 전부 수정 |

→ `a4279bfb`. 테스트: validator 61 / admin-ui 418 / chat-agent 전부 통과.
배포: Helm rev 213 · 스모크 통과 · 마이그레이션 0046 적용.

### 라운드 6 — NOT CONVERGED (must-fix 0)

| # | 발견 | 조치 |
|---|---|---|
| S1 | 세션 리셋 notice 가 pending 버블 뒤에 삽입(유령 버블) + 하드코딩 한글 + report export 포함 | notice 를 pending 버블 앞에 삽입 + `sessionResetNotice` i18n(en/ko) + notice 플래그로 export 제외 |
| S2 | AgentCore microVM idle timeout(기본 ~900s) 후 in-memory 이력 유실 → 재기동 시 맥락 상실 | admin-api 가 DB 이력을 payload 에 탑재, agent 는 `Agent.messages` 비어있을 때만 재수화(락 안, 이중 시드 방지) |
| S3 | `FETCH FIRST ROW`(count 생략)·비정수 count·`PERCENT` 잔여 시맨틱 왜곡 | 생략 count 는 LIMIT 1 정규화, `WITH TIES`/`PERCENT`/비정수는 내부 보존 + 외부 상한 래핑 + 엣지 테스트 |
| N1 | 실패 턴의 dangling user 메시지가 다음 프롬프트에 잔류 | 스트림 전 `len(messages)` 스냅샷 → 예외/취소 시 복원 |
| N2 | `CHAT_IMAGE_HOSTS` 미검증 — `;`/`*`/scheme 으로 CSP 지시문 인젝션 가능 | `CSP_HOST_RE` 로 호스트 형식만 허용, 무효 값 폐기, 미구성 시 fail-closed |
| N3 | `CHAT_IMAGE_HOSTS` Helm values 미연결/문서화 부재 | values.yaml 에 항목+주석 추가 |

→ `69b7a05e`. 테스트: validator 65 / chat-agent 250 / admin-api 970 /
admin-ui 418 / tsc 클린.
배포: Helm rev 214 — admin-api·scheduler·admin-ui `69b7a05e` · 스모크 13/0
(migration 이미지는 `a4279bfb` 유지 — db/ 변경 없음).

### 라운드 7 — NOT CONVERGED (must-fix 0, S-1·S-2 가 수렴 조건)

| # | 발견 | 조치 |
|---|---|---|
| S-1 | 재수화 이력이 role 교대 미보장 — 고아 user 행(producer 취소/재시작으로 assistant 미저장)·빈 assistant 필터·LIMIT 12 패리티 깨짐으로 `[user,user]`/선두 assistant 시드 → Bedrock ValidationException 으로 해당 microVM 의 후속 턴 전부 실패 | `_seed_history` 정규화 — 선두 assistant 제거, 연속 같은 role 최신만, 후미 user 제거 |
| S-2 | N1 스냅샷 인덱스(`del msgs[mark:]`)가 SlidingWindow 제자리 트림과 충돌 — strands `apply_management` 는 `finally` 에서 실행돼 예외 경로에도 window_size 40 으로 앞을 잘라 인덱스 복원 무효, CancelledError(BaseException)도 미포착 | 리스트 사본 스냅샷 + `orch_msgs[:] = snapshot` 통째 복원, `except BaseException` |
| N-1 | `LIMIT (2+3)`(5행)·`LIMIT 2.9`(3행)·음수 LIMIT 이 LIMIT 1000 으로 치환돼 의미 확대/PG 오류→성공 변환 | 비리터럴·음수 LIMIT 도 FETCH 와 동일하게 서브쿼리 래핑 보존 |
| N-2 | `CSP_HOST_RE` 는 형식만 검사 — `s3.리전.amazonaws.com` path-style 엔드포인트 허용 시 모든 버킷 경로 개방으로 R17-5 S1 재개방 | `s3.`/`s3-` 접두 호스트 거부 + values.yaml 문서화 |
| N-3 | user 메시지 무제한 + 이력 SELECT 가 원문 통째로 읽은 뒤 Python 절단 | `MessageCreate.content` `max_length=16000` + `left(content,4000)` DB 절단 |
| N-4 | 시드/복원/취소 경로 테스트 공백 + `payload.get` 직접 호출 | 회귀 테스트 4건 + middlewareCsp 4건 신설, `(payload or {}).get` |
| 기타 | `handleSubmit` deps `t` 누락, values 주석의 deployment.yaml:87→120 stale | 정정 |

→ `c4e71a8d`. 테스트: chat-agent 258 / admin-api 970 / admin-ui 422
(middlewareCsp +4) / tsc 클린.
배포: Helm rev 215 — admin-api·scheduler·admin-ui `c4e71a8d` · 스모크 13/0.

### 라운드 8 — CONVERGED (M 0 / S 0 / N 8)

라운드 7 수정 6건+minor 2건 전부 FIXED 확인. kiro 가 Strands 1.42 소스
실독으로 재검증: `self.messages` 재할당은 `__init__`·`load_snapshot` 뿐이라
스트림 경로의 `[:]` 복원이 유효, 대화 관리자는 in-place 변경만 함.
잔여 N 8건은 동일 라운드에서 전부 조치:

| # | 발견 | 조치 |
|---|---|---|
| N-a | 시드가 role 외 사유(비-dict 원소·blank 텍스트)로 무효하면 microVM 영구 실패 | `isinstance(m, dict)` + `text.strip()` 방어 |
| N-b | 얕은 스냅샷 — `_truncate_tool_results` 의 dict 제자리 변형 미복원 | `copy.deepcopy` 스냅샷 |
| N-c | `int(expr.this)` 가 hex(`0x10`)·bigint-오버플로 리터럴의 의미/오류를 바꿈 | `exp.Literal` 숫자+bigint 범위 검사로 제한(데드 `< 0` 분기 제거) |
| N-d | 16000 초과 입력이 422 + pydantic 원문 에코 노출 | textarea `maxLength=16000` |
| N-e | `vpce-*.s3.리전.vpce.amazonaws.com` VPC endpoint path-style 미차단 | `^vpce-` 거부 추가 |
| N-f | 음수 LIMIT 테스트가 escape hatch 로 공허 + NULL/hex 미커버 | 테스트 강화 + NULL/hex/오버플로 케이스 |
| N-g | chat_agent.py stale 주석(1503→1601) | 정정 |
| N-h | `screen_context` 무제한 | 직렬화 16000 상한(field_validator) |

→ `1d398dea`. **최종 verdict: CONVERGED.**

### 최종 누계(R17, kiro 라운드 포함)

- 커밋: … → `a4279bfb` → `69b7a05e` → `c4e71a8d` → `1d398dea`
- 테스트: chat-agent 261 / admin-ui 422 / admin-api 970 / tsc 클린
- 배포: Helm rev 216 — admin-api·scheduler·admin-ui `1d398dea`,
  migration `a4279bfb` · 스모크 13/0
- 알려진 트레이드오프: 실패 턴은 agent 메모리에서 지워지지만 부분 답변이
  DB 에 남아 warm/재수화 microVM 의 맥락이 다를 수 있음(교대 규칙은 유지 —
  kiro R17-8 에서 허용 가능으로 판정)
