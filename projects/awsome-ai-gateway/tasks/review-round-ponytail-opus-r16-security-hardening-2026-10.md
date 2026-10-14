# R16 — 교차 보안/신뢰성/배포 드리프트 심층 리뷰 (2026-10)

리뷰어: kiro `claude-opus-5.5` (read-only) + 자체 코드 추적 검증.
대상: 인증 체인(dev token/OIDC/virtual key/service token/internal token),
STS CLI 발급, body 로깅, JWT 키 캐시, Cognito sync 부수효과, 감사 로그 커버리지,
미들웨어 순서, 셧다운 순서, 차트↔서비스 배포 드리프트.

## 발견 및 조치

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| H1 | HIGH | **dev-login 우회**: `dev.<b64>.sig` 토큰은 무서명 — `DEV_LOGIN_ENABLED=true` 가 prod 배포에 새면 임의 ID 인증 가능. prod 가드·OIDC 공존 가드 부재 | `_parse_dev_token` 에 이중 가드 — `settings.is_production` 이면 무조건 거부, `oidc_issuer_url` 설정 시에도 거부(OIDC 구성 환경=실인증 환경). dev values 파일에 명시적 주석·admin-api/admin-ui 분리, prod 는 기존 false 유지 |
| H2 | HIGH | **CLI STS 계정 검증 우회**: `ALLOWED_AWS_ACCOUNTS` 빈 리스트가 계정 검증을 조용히 비활성화, `arn:aws:iam::user/*` 는 `role_name=None` 이라 역할 검증도 우회 | 발급 경로 fail-closed — 계정 allowlist 가 비거나 미구성이면 거부. ARN 종류와 무관하게 계정 검증 후 assumed-role 은 역할 검증, iam-user ARN 은 명시 거부. config docstring 을 fail-closed 의미로 정정 |
| H3 | HIGH | **body 로그 바이트 무제한** — 큐 개수만 제한(레코드 수십~수백 KB × 수천 = 수백 MB 누적), bedrock 라우터와 에러/스트림 실패 경로는 로깅 누락 | 큐 총 직렬화 바이트 회계 + 초과 시 oldest eviction(개수 상한 유지). messages/openai_compat/bedrock 의 비-2xx·provider 오류·스트림 실패 경로에 enqueue 배선. enqueue 는 메모리 연산만(네트워크 I/O 무) |
| M1 | MEDIUM | **401 위조**: auth 미들웨어가 downstream 앱 예외를 인증 실패로 오분류 — response 시작 후 PermissionError 에 401 두 번 씀. JWT 캐시가 kid 원문 키 + 검증 전 캐시 | 시작 후 예외는 재전파, 미시작 인프라 오류는 503. `kid` 는 sha256 digest 로 캐시 키화. 캐시키 우선 검증 → 불일치 시 활성 DB 키 전부 순회 → 검증된 키만 캐시. unknown kid refresh 스로틀. `max_body_size` 미들웨어 신설(LIFO 순서 명시 주석) |
| M2 | MEDIUM | **Cognito sync 부수효과 누락** — 팀 이관이 user-context/VK/예산/RL 캐시를 미무효화, `_build_cognito_sync_service` 에 cache_mgr 미전달, commit 전 무효화는 stale 재적재 경합 | cache_mgr 주입 + 커밋 후 무효화 순서. 리더 포인터 정리는 best-effort(실패해도 sync 중단 없음). `repo.flush()` 추가로 무결성 오류가 카운터/결과 보고 전에 표면화 |
| M3 | MEDIUM | **감사 로그 공백** — service-token 발급/회전/폐기, cognito sync, app 정책/default-model, 웹검색 정책, 사용자별 client/model 변경 무기록 | 각 라우터에 `audit_logger.log` — 성공 커밋 후, actor/role/resource/request_id/IP/before-after |
| M4 | MEDIUM | **배포 드리프트** — `WORKERS`(Helm 주입)와 `uvicorn_workers`(레이트리밋 divisor) 분리로 replica 수 불일치 가능; 비용 스풀 drain 전 Redis close; naive datetime 비교; LAMBDA 이름 하드코드; 토큰 비교가 `==` | `WORKERS` 를 `uvicorn_workers` validation alias 로 수렴. 셧다운: 스풀 drain → bounded body-log stop → Redis close. `key_service` naive→UTC 강제. `compare_digest` 전환 + `INTERNAL_API_TOKEN` 미설정 fail-closed. chat_agent Lambda 이름/리전 설정화. install-eks.sh OIDC audience/issuer 주입 |

## 회귀 테스트

- gateway-proxy `tests/regression/test_r16_security_fixes.py` (+9): 바이트 상한
  eviction, 최대 바디 413, downstream 예외 재전파, JWT kid digest 키/전체 키
  순회/검증키만 캐시, WORKERS alias, naive datetime 등
- admin-api `tests/regression/test_r16_security_fixes.py` (+5): prod/OIDC dev
  토큰 거부, STS 빈 allowlist fail-closed, IAM-user ARN 거부, cognito sync
  팀 이관 부수효과(캐시 무효화·예산 비활성·RL 해제), internal token non-ASCII
- admin `tests/unit/test_key_service.py` +1: naive sso_session_expires_at UTC 간주
- `tests/unit/test_cli_service.py`: 레거시 permissive 단정을 fail-closed 계약으로 갱신

## 수용 잔여

- body 로그는 프롬프트/응답 원문 비마스킹 — 프라이버시 갭으로 별도 작업 필요
  (배포 시 Firehose/S3 접근통제가 방어선).
- dev token 은 무서명 형식 유지 — dev 환경 전용이라는 전제를 가드로 강제한 것이지
  토큰 자체를 서명한 것은 아님.
- audit 큐는 비동기 인프로세스 — 가득 차면 drop(기존 AuditLogger 설계, 변경 아님).
- `scan_iter` mock/unawaited AsyncMock RuntimeWarning 등 테스트 위생 경고는
  기존 것으로 유지 — 본 라운드에서 미해결, 실패·커버리지 손실 아님.

## 테스트

- gateway-proxy: 1396 pass / 60 skip / 1 warning (회귀 +9)
- admin-api: 965 pass / 50 skip / 98 warnings (회귀 +5, key_service +1)

## 검증 라운드 (수정분 재검토)

### 라운드 1 (v1 diff) — must-fix 2 + should 다수

| # | 발견 | 조치 |
|---|---|---|
| V1-1 | **JWT 전역 스로틀이 역 DoS** — `_jwt_keys_refreshed_at` 전역 타임스탬프를 위조 kid 하나가 전진시켜 정상 JWT 사용자 전원 60초 lockout | 프로세스 스로틀 자체를 제거 — 활성 키 **keyset** Redis 캐시(60s TTL)로 교체. 위조 kid 는 DB 를 전혀 치지 않고, 정상 신키도 lockout 없음 |
| V1-2 | **`ALLOWED_AWS_ACCOUNTS` fail-closed 가 배포 경로를 깸** — ECS/docker-compose/plain-helm 에 env 미배선 | ECS taskdefs 에 현재 계정 주입, `.env.example`·values·NOTES 에 배선/경고, dev.local 에 계정 610156625835 명시 |
| V1-3 | chunked 업로드(Content-Length 없음)가 max_body_size 우회 | receive 래핑 누적 카운트 + disconnect 합성 |
| V1-4 | body 로그 큐가 파싱 dict 보유 → 실 메모리 ~3x 초과 | 직렬화 bytes+메타데이터만 큐잉 |
| V1-5 | Cognito sync 팀 이관이 `rl:config:USER:*` 무효화·`team:vk_hashes` 역인덱스 스왑 누락 | `collect_team_transfer_side_effects` 공용 헬퍼(transfer_user·per-user·sync_all·stale-team 공유) |
| V1-6 | messages 전회로개방 503·openai_compat 디스패치 예외 body-log 누락 | 배선 추가(effective_model_config 사용) |
| V1-7~11 | LAMBDA_QUERY_DB 미주입·.local.yaml issuer/dev-login 모순·shutdown 데드라인·WORKERS/UVICORN_WORKERS 우선순위 | env 주입+주석, issuerUrl 비우기(후에 V3-M1 로 재수정), 40s 공용 데드라인, WORKERS 우선 alias+불일치 경고 |

### 라운드 2 (v2 diff) — must-fix 1

| # | 발견 | 조치 |
|---|---|---|
| V2-1 | **sync_all stale-team 정리가 캐시 작업을 수집만 하고 drain 하지 않음** | `_upsert_one_user` 에 cache_out 수집 경로(savepoint 밖 이관), stale-team 커밋 후 `_flush_pending_revocations` 호출 |
| V2-2~11 | JWT 로컬 keyset 폴백·캐시 우선 순서·413 엣지·stale 주석·실패 기록 세션·issuer 우선순위 주석·shutdown 인플라이트·바이너리 디코딩 | `_get_active_keyset`(Redis→로컬→DB, DB 장애 시 stale 로컬 반환), response_started 추적+종결 body, 세션 전달+커밋, install-eks.sh/.local 주석, `_flush_once` 취소 시 재인큐, incremental UTF-8 decoder |

### 라운드 3 (v3 diff) — must-fix 1 + should 5

| # | 발견 | 조치 |
|---|---|---|
| V3-M1 | **dev.local.yaml: 공인 ALB(0.0.0.0/0) + DEV_LOGIN_ENABLED=true + 빈 issuer → 무서명 dev ADMIN 토큰이 인터넷 통과** | DEV_LOGIN_ENABLED=false(양쪽), issuerUrl/audience 를 ap-south-1 풀로 복원 — 배포 환경은 dev-login 금지 원칙 복귀 |
| V3-S1 | MaxBodySize 거부 시 `_drain_body` 가 disconnect 를 흡수해 **잘린 접두사가 완전 본문으로 재생돼 처리·과금** | `_drain_body` 가 `ClientDisconnect` 전파 — MaxBodySize 가 잡아 413 마무리 |
| V3-S2 | `swap:` 실패행이 재전파로 세션 롤백돼 retry 불가(기록이 남지 않음) | `_swap` 내부에서 실패행 커밋 후 재전파 |
| V3-S3 | stale-team 멤버 루프에 savepoint 없음 — 부수효과 실패가 트랜잭션 poison | 멤버당 `begin_nested` + 로컬 수집 → 성공분만 pending 병합 |
| V3-S4 | body 로그 재인큐가 배치 전체 → 배송 완료분 중복 가능 | `done` 인덱스 세트 — 미결 항목만 재인큐(at-least-once 명시) |
| V3-S5 | chat-agent 가 LAMBDA_QUERY_DB 미설정 시 **prod** 함수명으로 폴백 | 기본값 제거 — 미설정 시 명시 오류 반환. dev values 에 배선+출처 주석 |
| nits | auth 주석·keyset 변형 레코드 500·decoder final flush | 주석 정정, `rec.get("pem")` 가드 |

### 라운드 4 (v4 diff) — must-fix 1 + should 2

| # | 발견 | 조치 |
|---|---|---|
| V4-MF1 | **AgentCore runtime JSON에 LAMBDA_* env 없음** — agent 코드의 prod-기본값 제거(S5)와 맞물려 prod BI 도구 사망 회귀 | NDS-02가 terraform output(`chat_agent_*_function`)을 읽어 runtime environmentVariables에 주입; 빈 output 시 키 생략(후에 V5-M3에서 die로 강화) |
| V4-SF1 | adminUi 측 `DEV_LOGIN_ENABLED` 여전히 true — API가 dev 토큰을 거부하는데 UI만 dev 로그인 폼 노출 | `.local.yaml` adminUi.devLoginEnabled=false |
| V4-SF2 | stale-team 루프에서 savepoint 롤백 후 `member.id` 접근 → expire lazy-load 위험 | member_id/team 속성 루프 전 로컬 캡처 |
| V4-N1 | `get_schema`도 prod-이름 기본값 | env 필수로 변경 |
| V4-N2 | stale-team 카운터/포인터가 실패 멤버도 성공으로 계산 | moved_ids=성공분만, leader repoint는 리더가 실제 이동됐을 때만 |
| V4-N3 | 취소 경로 재인큐가 바이트 상한/드롭 카운터 우회 | 정상 enqueue 경로 재사용 |

### 라운드 5 (v5 diff) — must-fix 3 + should 7

| # | 발견 | 조치 |
|---|---|---|
| V5-M1 | **Bedrock 미드스트림 예외가 여전히 유실/비마스킹** — botocore는 `*Exception` dict 멤버가 아니라 `EventStreamError`(=ClientError)를 raise. dict 멤버 검사는 데드코드였고, `/v1/messages`는 `str(exc)` 비살균, invoke/converse 경로는 에러 이벤트 없이 잘림 | 두 제너레이터에서 `ClientError`+`ConnectionClosedError`+`EndpointConnectionError`+`ReadTimeoutError`를 잡아 합성 에러 이벤트로 변환(ARN 살균). converse는 유니온 멤버 모양 `{code: {message}}` 유지. `streaming.py` 3곳 `str(exc)` 전부 `_redact_arns` 적용 |
| V5-M2 | **`ALLOWED_IAM_ROLES` 빈 리스트 fail-open** — `if settings.ALLOWED_IAM_ROLES:` 가드 하나라 비어 있으면 역할 검사+IAM-user 거부가 통째로 스킵돼 허용 계정의 임의 assumed-role(EC2/Lambda/CI)이나 IAM user가 VK 발급 | 빈 리스트는 명시적 거부(fail-closed, NOTES.txt 문서화된 계약과 일치). IAM user 거부는 무조건 적용 |
| V5-M3 | **prod outputs.tf에 `chat_agent_*_function` output 없음** — NDS-02 prod 실행 시 env에서 키 누락 + agent 기본값 제거와 맞물려 prod BI 회귀 | prod outputs.tf에 dev와 동일 output 3종 추가. NDS-02는 자체적으로 `enable_chat_db_tools=true`를 강제하므로 빈 output=미프로비전 → `die`로 전환 |
| V5-SF1 | NDS-02 ⑦단계 yq가 `adminApi.env.LAMBDA_*` 미기록 — `/sql/execute` 503 | yq에 `LAMBDA_QUERY_DB`/`LAMBDA_GET_SCHEMA` 추가 |
| V5-SF2 | `_repoint_leader(team, …)`가 `team.members` 순회 — savepoint 롤백된 실패 멤버 expire → MissingGreenlet로 잔여 팀 정리 중단 | 멤버 속성 스냅샷을 루프 전 캡처, 실패(미이동) 멤버 중 TEAM_LEADER만 후보로 리더 재지정 |
| V5-SF3 | bedrock 스트림 finally에서 `_enqueue_body_log`가 `_aclose_iter`보다 먼저 — enqueue 예외 시 이터레이터 미종료 → executor 스레드 누수 | close-first 순서로 교체 |
| V5-SF4 | oversized 레코드 S3-미설정/put 실패 시 `_dropped` 미증가 | 두 경로 모두 카운터 증가 |
| V5-SF5 | `UVICORN_WORKERS`만 설정 시 settings는 그 값을 쓰지만 Dockerfile은 `${WORKERS:-4}`로 4워커 기동 → divisor 드리프트 잔존 | Dockerfile도 `${WORKERS:-${UVICORN_WORKERS:-4}}` — settings와 동일 우선순위 |
| V5-SF6 | per-kid JWT 캐시 1h — 비활성 키가 keyset 우회 경로로 최대 1시간 유효 | `JWT_KEY_CACHE_TTL`을 keyset TTL(60s)에 맞춤 — 재캐시는 keyset GET+검증이라 DB 무관 |

### 라운드 6 (v6 diff) — must-fix 1 + should 1 + nit 4

| # | 발견 | 조치 |
|---|---|---|
| V6-MF1 | **V5-M1 수정이 회귀 유발** — 미드스트림 에러가 정상 청크로 변환돼 `_scan`이 error로 마킹하지 않음 → usage_logs.status/body-log status가 실패 스트림을 SUCCESS로 기록(E2E S5 계약 위반) | `_scan_bedrock_stream_chunk`가 에러 프레임에 `error_type` 기록(invoke `type:"error"`/converse `*Exception` 멤버), `_scan`에서 `mark_outcome` — `modelTimeoutException`은 "timeout" |
| V6-SF1 | 캐치 목록의 절반이 발화 불가 — 미드스트림 전송 실패는 urllib3 원형(ProtocolError/ReadTimeoutError)으로 올라와 botocore 예외가 아님 | 두 제너레이터에 `urllib3.exceptions.ProtocolError`/`ReadTimeoutError` 추가 |
| V6-N1 | 전송 예외 문자열의 ARN이 percent-encoded(`arn%3A…`)라 `_ARN_RE` 미스 → 추론프로파일명 유출 | `_redact_arns`에서 unquote 후 치환 |
| V6-N2 | 폐기 전파 창이 실제 ~2×TTL(≈120s) — per-kid 엔트리가 최대-60s-stale keyset에서 재캐시될 수 있음 | 주석으로 명시 |
| V6-N3 | dev `/sql/execute`는 NDS-02 전까지 503 (의도된 fail-safe) | values 주석으로 명시 |
| V6-N4 | IAM-user 거부가 breaking change (role allowlist 설정 시에도) | 릴리스 노트 성격 — 문서 기록 |

나머지 검증 항목 통과: 캐치 배치(GeneratorExit 미삼킴·finally stream.close)·converse 유니온 모양·cli_service 검사 순서·NDS-02 JSON/die·repoint 스냅샷 계약·SF3-SF5·import cycle 없음.

### 라운드 7 (v7 diff) — **CONVERGED** ✅

must-fix/should-fix 0. 검증자가 urllib3 ProtocolError/ReadTimeoutError를 직접
raise해 양 제너레이터의 합성 이벤트와 스캐너 마킹을 확인하고, 전체
gateway 스위트(1406) 통과를 재확인.

수용된 nit 3건(비차단):
- disconnect 이후 도착하는 에러 프레임은 SUCCESS 기록 — "클라이언트 끊김은
  상류 선행 에러 없으면 성공 취급"이라는 기존 문서화 계약과 일치(회귀 아님).
- ReadTimeoutError는 TIMEOUT이 아닌 ERROR로 기록 — 전송 에러 분류로 수용.
- `_scan`→finalize(status=ERROR) 라우터 수준 테스트는 없음 — 스캐너 단위
  테스트로 커버, 후속 위생 과제로 수용.

## 테스트 (최종)

- gateway-proxy: **1403 pass / 60 skip / 1 warning** (회귀 +11)
- admin-api: **968 pass / 50 skip / 97 warnings** (회귀 +5, cli_service +1, key_service +1)
- NDS-02: `bash -n` OK + terraform output 유무 4케이스(둘 다/하나/없음) 생성 JSON 유효 검증 + 빈 output 시 die 동작 확인
