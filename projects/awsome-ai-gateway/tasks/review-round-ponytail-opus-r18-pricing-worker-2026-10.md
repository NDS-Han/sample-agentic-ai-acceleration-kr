# R18 — Haiku 5.5 장문 요금제 + cost-recorder-worker/agent 보조모듈 리뷰 (2026-10)

리뷰어: kiro `claude-opus-5.5` (read-only, 실측 포함 — venv 예외변환 재현,
fewshot 13건 가드 통과 실행, regex 매칭, Postgres 컨테이너 검증) + 자체 검증.
대상: Claude Haiku 5.5 프롬프트 길이 구간 요금 end-to-end(alias 경유 호출의
티어 과금), 미검토 컴포넌트 — cost-recorder-worker(batch_flusher/stream_consumer/
daily_aggregator/main/worker), admin-chat-agent 보조 모듈(candidate_select/
sql_struct/fewshot/envelopes), get_schema Lambda.

## 배경 — Haiku 5.5 구간 요금

Anthropic/AWS 공시: 프롬프트(캐시 포함)가 100K 초과면 전 차원 5배.
alias `claude-haiku-5-5` 로 호출해도 provider_model_id 기준 구간이 적용돼야 함.
라이브 감사: 87건 / $0.050046 / 최대 프롬프트 70,086 tok — 100K 초과 0건,
소급 정산 불필요(마이그레이션만으로 종결).

## 라운드 1 — NOT CONVERGED (M 2, S 5, N 9)

### M (수렴 차단)

**M1. 워커 영구오류 분류가 asyncpg 에서 동작하지 않음 — poison 이 그룹 봉쇄**
- `batch_flusher._PERMANENT_FLUSH_ERRORS = (IntegrityError, DataError, ValueError)`
  이지만 SQLAlchemy asyncpg 방언(`_asyncpg_error_translate`)은 class-22 서버
  오류를 `DataError` 가 아니라 범용 `DBAPIError` 로, UndefinedColumn 을
  `ProgrammingError` 로 변환한다 — 클래스 매칭이 전부 빗나감.
- 결과: 결정적 오류가 dead-letter 로 못 빠져 배치 머리가 영구 정지 →
  스트림 MAXLEN(500K) 트림으로 **전원 청구기록 유실** 가능.
- 조치: `_is_permanent_flush_error()` — `exc.orig.sqlstate[:2] in {"22","42"}` +
  `ProgrammingError` + asyncpg 클라이언트측 `DataError`(sqlstate 없음) 판별.
  테스트는 실제 변환 형태(DBAPIError+sqlstate fake orig, 실제
  `asyncpg.exceptions.DataError`)로 재구성 — 기존 테스트는 `sqlalchemy.exc.
  DataError` 직접 raise 라 결함이 가려져 있었다. 회귀 4건 추가.
  venv 에서 SQLAlchemy 소스 직독으로 변환 매핑 확인.

**M2. 0047 이 단가값/alias 가드로 대상을 잡아 레포 증거와 충돌 + 조용한 no-op**
- 레포 산출물(test_pricing_precision/CreateModelDialog/pricing.ts)은
  `us.anthropic.claude-haiku-5-5` + global×1.1 요율을 증거로 함 — 단가
  정확일치 가드는 지역 프리미엄 등록 환경에서 성공 표시인 채 no-op.
- 조치: `provider_model_id LIKE '%claude-haiku-5-5%'` 로 대상 지정,
  long 단가는 **닫는 행의 short ×5 로 INSERT…SELECT** (공시 구조적 5배 —
  지역/할증 무관 성립), 유일 가드는 "티어 미존재". 마지막 DO 블록이 대상
  alias 에 열린 티어 행이 없으면 RAISE WARNING.
- 자체 검증(postgres:16 컨테이너): global 행 ×5 적용, us. 프리미엄 비율 보존
  (0.00011→0.00055), 이미 티어 있는 행·비haiku alias 미접촉, 가격행 없는
  alias WARNING, 2차 실행 UPDATE 0/INSERT 0 멱등, downgrade 가 우리 행만
  삭제+재오픈(열린행 유일 인덱스 보호).

### S

**S1. 선예약이 티어를 모름 (5배 증폭)**
- `_estimate_cost`(CPM/CPH)는 long 요율 미반영 — 100K 초과 요청이 분/시간
  한도를 실비의 1/5 로 통과. `_estimate_worst_cost`(월예산)는 문자 추정으로
  임계 판정해 105K 실제가 ~80K 추정이면 단락 예약.
- 조치: `_uses_long_tier()` — 추정이 임계의 **70%** 를 넘으면 long 요율
  (선예약은 settle 환불되므로 경계 위 과대예약이 아래 과소예약보다 안전).
  두 경로 공유. 회귀 2건.
- 끊긴 스트림 추정 과금(cost_recorder)은 추정치가 곧 기록 토큰이 돼 실제
  판정과 동일 경로 — 마진을 실과금에 얹으면 추정 초과 과금이라 미적용(의도).

**S2. 근본 원인 재발 경로 — 신규 등록은 다시 무티어**
- 생성 폼/`ModelCreateRequest` 에 long 필드 없음 → 같은 모델 재등록 시
  NULL 티어 반복. 마이그레이션은 실행 시점 행만 고침.
- 조치: `_KNOWN_TIERED_PROVIDER_IDS = ("claude-haiku-5-5",)` — set_pricing 이
  해당 provider 의 무티어 행을 열 때 structlog 경고 + 감사 변경사항에
  `tiered_model_missing_long_context` 기록.

**S3. candidate_select 가 "실행 기반"이 아니었음**
- 후보 rows 가 query_db 결과가 아니라 LLM 주장 `SqlEnvelope.rows` — 같은
  값 지어내면 허위 합의, 별칭만 다르면 false-split(키 포함 해시), rows
  생략 시 전원 동일해시 → 합의 1.0 PASS.
- 조치: `ask_sql_verified` 가 각 후보 SQL 을 `query_db` 로 **실제 실행**해
  결과 필드(rows/row_count/columns/s3_uri/accuracy_warnings)를 envelope 에
  덮어씀 — 실행 실패 후보는 error 로 무효 처리. 해시는 키·컬럼순서를 버린
  **값 멀티셋**(별칭 차이·컬럼 재정렬 모두 커버). 회귀 + 별칭 케이스 테스트.

**S4. sql_struct 결정적 사실이 CTE/서브쿼리에서 오보고**
- 최외곽 Select 만 봐서 CTE 내부 GROUP BY/WHERE 를 "(없음)" 으로 보고,
  CTE 이름 `t` 를 테이블로 오인, 위치 기반 `GROUP BY 1` 은 "없음".
- 조치: 모든 Select 스코프 순회 + CTE 이름 테이블 제외(schema 한정 동명은
  실테이블로 유지) + 위치 번호 → select 표현식 해석. 회귀 테스트 5건 신규.

**S5. fewshot 뱅크가 가드가 경고하는 패턴을 가르침**
- 9/13건이 대시보드 정합 경고(status='SUCCESS' 누락) — L3 동률 페널티에
  걸리는 패턴을 모범으로 제시하고 있었다.
- 조치: usage_logs 총량 집계 8건(fs01/02/03/06/09/10/11/12)에
  `status='SUCCESS'` 추가. fs07 은 budget_configs 라 해당 없음.
  `check_dashboard_consistency` 실측으로 13/13 정합 확인.

### N (조치)

- **N1** `_above` 첫매칭 비결정 + min() 이 다른 티어 단가를 한 임계에 혼합 →
  차원 내 다중 N 은 최소 N 선택, 차원 간 N 불일치 시 **티어 전체 거부** +
  errors 기록.
- **N2** 0047 downgrade 재오픈에 "열린 행 없음" 가드 추가(유일 인덱스 위반 방지).
- **N3** EFFECTIVE_FROM 00:00Z 소급 — 워커 재계산 없어 무해, 정렬 정합 확인.
- **N4** stale 주석 정정(cost_recorder 2배/1.5배 표현 → 모델별 배수,
  MAXLEN 100K→500K 2곳, dead-letter docstring).
- **N5** 워커 `schema_version` 기본값 2→3 (게이트웨이 발행 정합).
- **N6** `flush_dead_letter` written 이 replay/경합 no-op 도 집계 → 실삽입만.
- **N7** get_schema 목록모드의 forbidden 컬럼명 노출 — 이름만·관리자 컨텍스트,
  허용 판정(기록).
- **N8** ECS `COST_STREAM_CONSUMER="worker-1"` 고정 — 롤링 중 같은 PEL 공유,
  RETURNING 가드로 이중계상 없음 확인(기록).
- **N9** `PricingRequest` validator in/out 만 요구 vs 서비스 5개 → 스키마를
  5개 전부로 정렬(경계 422, 서비스 400 과 동일 규약).

### 정상 확인(kiro)

- 스키마 패리티(게이트웨이↔워커 CostStreamEntry 일치), 비용 드리프트 없음
  (워커는 게이트웨이 cost_usd 신뢰·재계산 안 함), ack/replay 이중계상 없음
  (`_dedup_in_batch`→`_filter_replays`→RETURNING 가산), XTRIM MINID floor
  안전, `context_tier` 기록 정합, `resolve_context_tier` 의미론(세 입력 버킷
  합 > threshold, 요청 단위)이 Haiku 과금 의미론과 부합, 스트리밍 usage
  캐시필드·invocationMetrics 처리 정합.

## 라운드 2 — NOT CONVERGED (M 2, S 3, N 7)

### M-A (클라이언트측 asyncpg DataError 누락 주장) — FIXED/검증완료

- kiro 주장: 클라이언트측 `asyncpg.exceptions.DataError` 가 SQLAlchemy
  `InterfaceError` 로 변환돼 판별 누락.
- 실측(실 PG + 설치된 sqlalchemy 2.0.49/asyncpg 0.31.0): bad-uuid 는
  `DBAPIError(orig=adapter Error, sqlstate='22000', __cause__=DataError)`
  로 온다 — 기존 sqlstate 22 판별이 이미 잡고, 원인 체인의
  `isinstance(asyncpg.DataError)` 검사가 이중 방어.
- 회귀 테스트를 실제 변환 형태(DBAPIError+sqlstate 속성)로 갱신.

### M-B (class-42를 영구 오류로 분류 → 전량 청구 유실) — FIXED

- 첫 수정이 ProgrammingError/42 를 영구로 분류해 UndefinedColumn·UndefinedTable·
  42501 같은 **배포 수준** 오류를 행 poison 으로 삼키는 결함이었다 —
  배치 전량이 usage_logs·budget_usages 없이 XACK(즉시 청구 유실).
- 조치: `_PERMANENT_ROW_SQLSTATE_CLASSES={22,23}` 만 영구 — 행 단위
  결정적 오류. 42/ProgrammingError·40P01(데드락)·연결 오류는 3개 경로
  (`flush`/`_flush_per_row`/`flush_dead_letter`) 모두 전파해 PEL 보존.
- 회귀: 22P02 → 영구, 42703 → 전파, 40P01 → 전파 테스트 추가.

### S-A (웹서치 배수가 티어 판정에 혼입) — FIXED

- `_gw_admission_input_mult`(웹서치 루프 턴당 재청구 대비 예약 배수)가 곱해진
  `estimated_input` 으로 티어 판정 → 단일 턴이 5배 구간으로 과대예약.
- 조치: `estimated_input_billing`(턴 추정)을 `tier_input` 으로 분리 —
  `_estimate_cost`·`_estimate_worst_cost` 모두 비용 곱셈은 배수 입력,
  티어 판정은 턴 추정. 청구 폴백(`rate_limit_state.estimated_input`)도
  턴 추정으로 교체(끊긴 스트림의 iters 배 과대청구 방지).

### S-B (candidate_select 가 stats 를 실실행값으로 안 덮음) — FIXED

- `ask_sql_verified` 가 query_db 실실행값으로 rows/row_count/columns/
  **stats**/accuracy_warnings/explain_cost/s3_uri/ok 를 덮어씀 —
  `stats` 는 orchestrator 가 "결정적 계산·그대로 인용"하는 근거라 LLM
  주장이 남으면 지어낸 숫자가 검증 통과.
- `result_fingerprint(rows, row_count, stats)` 신설 — 인라인 rows 상한
  (20행)을 넘는 꼬리 차이·서버측 전체행 요약 차이까지 지문에 포함해
  허위 합의 차단. 회귀 3건 추가.

### S-C (create_model 무티어 등록 경로) — FIXED

- `model_service._KNOWN_TIERED_PROVIDER_IDS`(pmid 부분문자열→threshold·배수)
  + `_auto_long_tier`: create_model 시 단락×공시배수 자동 파생 +
  감사 `long_context_auto_derived`. 지역 프리미엄 단가도 비율 보존.
- `update_model`: tiered pmid 인데 열린 가격 행이 무티어면 logger.warning.
- `set_pricing` 무티어 경고는 라운드 1에서 추가됨.

### N (조치)

- **N-a** `RAISE WARNING` 은 asyncpg(log listener 없음)에서 버려짐 →
  Python `op.get_bind()` 로 matched/closed/missing 카운트 +
  `alembic.runtime.migration` 로거 warning.
- **N-b** UPDATE/INSERT 가드 비대칭(닫기만 되고 재오픈 실패 → 열린 행 0개
  = $0 과금 위험) → 단일 CTE(`WITH closed AS (UPDATE…RETURNING) INSERT…
  SELECT FROM closed`)로 원자화 + effective_from 충돌·역전 구간 가드를
  UPDATE 에도 대칭 적용. 실 PG 로 멱등·충돌·downgrade·지역 단가 ×5 검증.
- **N-d** sql_struct `filters` 중복 제거.
- **N-e** 글로벌 alias_map 폐기 → `_col_fq` 가 컬럼별 **최근접 Select
  스코프**의 `_scope_sources`(from_/joins 의 Table 만)로 해석, 상관
  서브쿼리는 바깥 스코프로 상승. 위치 GROUP BY → 투영 컬럼의 FQ.
  회귀 3건 추가.
- **N-f** 값 멀티셋 해시의 동형 열 교환 병합 — 문서화된 trade-off, 허용.
- **N-g** L3 순차 지연 → `ask_sql_verified` 후보 생성+실행을
  ThreadPoolExecutor+`copy_context()` 병렬화(stash=False 라 공유 쓰기
  없음, `_session_id` context 전파로 query_db fail-closed 통과).

### 테스트

- cost-recorder-worker 38 · admin-chat-agent 273 · admin-api 975 ·
  gateway 대상 248 — 전부 통과.
- 0047 을 실 PG(컨테이너)로 검증: global/지역/이미티어/비대상/충돌/멱등/
  downgrade 전 케이스 정확.

## 라운드 3 — NOT CONVERGED (S-B 회귀 + 신규 material 1)

8/9 FIXED 확인(kiro 가 실 PG·sqlglot 소스·compute_stats 실import 검증).
잔여:

- **S-B STILL-BROKEN** — `compute_stats` 최상위 키가 컬럼 **별칭**이라
  `_normalize_stats` 가 키를 유지하면 별칭 무관성이 깨짐(false-split
  회귀). → 최상위 키 버리고 값 정렬, share_pct 리스트 정렬. FIXED.
- **[material] S3 staging 키 충돌 주장** — 검증 결과 **성립 안 함**:
  Lambda 가 빈 step_id 에 uuid 를 생성(호출마다 유일 키). 단 `s3_uri`
  의 LLM 주장값 폴백은 실결함 → `actual.get("s3_uri")` 로 교체. FIXED.
- **[should] boto3 client 생성 thread-safety** → `_lambda_client()` 모듈
  싱글턴(Lock 이중검사), query_db·get_schema 공용. FIXED.
- **[should] 후보별 예외 격리** → `_gen_candidate` 래퍼가 예외를 무효
  Candidate 로 변환. FIXED.
- **[nit] dead_letter_dropped 오집계** → `flush_dead_letter` 가
  `(written, dead_lettered)` 반환, replay 스킵 미집계 + 로그. FIXED.
- **[nit] 0047 미래 effective_from 스킵** — 감사상 대상 행 10-13 생성이라
  경로 안 탐. 현 방식(스킵+경고) 수용.

## 라운드 4 — CONVERGED (조건부, material 0)

라운드 3 잔여 6건 중 5건 FIXED + staging 주장 철회. 잔여 should/nit
전부 조치:

- **N1** 전 후보 실패 시 LLM 주장 rows/stats/s3_uri 가 grounding 집합에
  유입 → 실패 분기에서 결과 필드 전부 비움.
- **N2** 20행 초과 비정렬 결과 false-split → `row_count > len(rows)` 이고
  stats 있으면 지문을 row_count+stats 로만(stats 없으면 rows 유지 —
  허위 합의보다 split 방향이 안전).
- **N3** `compute_stats` 숫자 판별이 rows[0] 만 봐 NULL 첫행이면 컬럼 누락
  → 전 행 스캔(전 컬럼 확정 시 조기종료). 지문에서는 share_pct 제외
  (label 컬럼이 컬럼순서 의존).
- **N4** code_specialist.md data_ref 예시 경로 정정.
- **nit** `written` 카운트를 commit 이후로 이동(양쪽 이중집계 방지).
- **nit** staging 키에 uuid 접미사 항상 부여 — LLM step_id 재사용·살균
  충돌로 앞 객체 덮어쓰기 차단.

### R18 최종 상태

- **테스트**: cost-recorder-worker 47 · admin-chat-agent 275 ·
  admin-api 975 · gateway 대상 248 — 전부 통과.
- **검증**: 0047 실 PG(컨테이너)로 global/지역/이미티어/비대상/충돌/
  멱등/downgrade 전 케이스 확인. asyncpg→SQLAlchemy 예외 변환 실형태
  실측(class-22 → DBAPIError+sqlstate, class-42 → ProgrammingError).
- **결론**: material 0 — CONVERGED.
