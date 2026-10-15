# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""query_db Lambda — admin-chat-agent's read-only SQL execution tool.

검증 단계 (Layer A — docs/admin-chat-agent-spec.md §2.5 / §3.1):
  1. sqlglot.parse(sql, dialect='postgres') — 파싱 실패 시 에러 피드백
  2. AST 검증 — DDL/DML 거부 (allowed: SELECT, WITH 만)
  3. 모든 참조 테이블 화이트리스트 확인
  4. EXPLAIN (FORMAT JSON) 으로 비용 추정 — total_cost > 50000 reject
  5. 실행 — read-only role 'gateway_chat_reader', statement_timeout=10s,
     LIMIT 1000 강제 wrap
  6. 결과 → S3 staging Parquet 업로드 (Code Specialist 가 read)
  7. sample 20행 + 메타만 LLM 에 반환 (INLINE_ROWS)

호출 입력:
  { "sql": str, "session_id": str, "step_id": str }
호출 출력:
  { "ok": True,
    "rows": [...최대 20행],
    "row_count": int,
    "columns": [{"name": str, "type": str}],
    "s3_uri": "s3://...",
    "explain_cost": float,
    "elapsed_ms": int }

  실패 시:
  { "ok": False, "error": str, "hint": str | None }
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import re

import boto3
import psycopg2
import psycopg2.extras
import sqlglot
import sqlglot.expressions as exp
import yaml

try:
    import sql_guard  # L0/L1 결정적 정확도 안전망 (DEVLOG §58)
except ImportError:  # pragma: no cover — guard 부재 시 기존 동작 유지(비파괴)
    sql_guard = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ─── Config ───
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "gateway")
DB_USER = os.environ.get("DB_USER", "gateway_chat_reader")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
S3_STAGING_BUCKET = os.environ["S3_STAGING_BUCKET"]
WHITELIST_PATH = os.environ.get(
    "SCHEMA_WHITELIST_PATH",
    "/var/task/schema_whitelist.yaml",
)
EXPLAIN_COST_LIMIT = float(os.environ.get("EXPLAIN_COST_LIMIT", "50000"))
QUERY_LIMIT = int(os.environ.get("QUERY_LIMIT", "1000"))
# LLM 에 인라인 반환하는 sample 행수. "top 10/20" 류 질의가 잘리지 않게 20.
INLINE_ROWS = int(os.environ.get("INLINE_ROWS", "20"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "10000"))


secrets = boto3.client("secretsmanager")
s3 = boto3.client("s3")


# ─── Whitelist (lazy load, cached) ───
_whitelist_cache: dict[str, Any] | None = None


def get_whitelist() -> dict[str, Any]:
    global _whitelist_cache
    if _whitelist_cache is None:
        with open(WHITELIST_PATH, encoding="utf-8") as f:
            _whitelist_cache = yaml.safe_load(f)
    return _whitelist_cache


def allowed_table_set() -> set[tuple[str, str]]:
    """{(schema, table)} set."""
    wl = get_whitelist()
    return {(t["schema"], t["table"]) for t in wl.get("allowed_tables", [])}


def forbidden_columns() -> set[str]:
    """{'schema.table.column'} set."""
    return set(get_whitelist().get("forbidden_columns", []))


# ─── Validation ───
class ValidationError(Exception):
    """SQL 검증 실패. message 가 LLM 으로 전달되어 self-correction trigger."""


def validate_sql(sql: str) -> sqlglot.Expression:
    """1, 2, 3 단계 검증 — sqlglot AST + 화이트리스트."""
    # 1. parse — 단일 문장만 허용. parse_one 은 첫 문장만 돌려주므로
    #    "SELECT 1; DROP ..." 의 두 번째 문장을 못 본다. 실행은 AST 재렌더라
    #    실무적으로는 안전하지만, 방어가 재렌더 동작 하나에만 기대지 않게 명시 가드.
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:
        raise ValidationError(f"SQL parse error: {e}") from e

    statements = [s for s in statements if s is not None]
    if not statements:
        raise ValidationError("Empty SQL")
    if len(statements) > 1:
        raise ValidationError("Only a single SQL statement is permitted")
    ast = statements[0]

    # Postgres 식별자 의미론에 맞춰 비인용 식별자를 소문자로 폴딩한다.
    #    sqlglot 은 CTE 매칭이 대소문자 구분인데 Postgres 는 비인용 이름을
    #    소문자로 접는다 — `WITH "PG_ROLES" AS (...) SELECT * FROM PG_ROLES`
    #    에서 sqlglot 은 외부 PG_ROLES 를 CTE 참조로 보지만 Postgres 는
    #    (pg_roles ≠ "PG_ROLES") 실제 pg_catalog.pg_roles 를 읽어 스키마
    #    카탈로그가 새는 우회였다 (claude R17-3 M1). 동시에
    #    `WITH x AS ... SELECT * FROM X` 같은 정상 쿼리의 오탐도 사라진다.
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

    try:
        ast = normalize_identifiers(ast, dialect="postgres")
    except Exception as e:  # noqa: BLE001 — 정규화 불가 = fail closed
        raise ValidationError(f"Could not normalize identifiers: {e}") from e

    # 2. 허용된 statement type 만 — SELECT 계열.
    #    ⚠️ 과거 로직은 "비허용 root 인데 DML 노드를 못 찾으면" 조용히 통과시켰다 —
    #    `SET`/`DISCARD`/`LISTEN` 같은 세션 조작문이 그대로 새고, `WITH x AS
    #    (DELETE ...)` 는 root 가 Select 라 DML 검사 자체를 건너뛰었다 (R17-S1,
    #    sqlglot 실측). 이제 root 는 엄격 화이트리스트 + DML/DDL 은 root 무관 전체
    #    스캔(중첩 CTE 포함)이다.
    # ⚠️ bare `VALUES (...)` root 는 거부 — traverse_scope 가 그 내부를 전혀
    #    방문하지 않아(scope 0개, sqlglot 실측) `VALUES ((SELECT ... FROM
    #    pg_roles))` 같은 루트가 테이블 화이트리스트를 통째로 우회했다 (R17
    #    claude 검증 #2). `WITH v AS (VALUES ...) SELECT` 처럼 SELECT 계열 안에
    #    중첩된 VALUES 는 root 가 Select/With 이므로 영향 없다.
    _ALLOWED_ROOTS = (
        exp.Select, exp.Union, exp.Intersect, exp.Except,
        exp.With, exp.Subquery,
    )
    if not isinstance(ast, _ALLOWED_ROOTS):
        raise ValidationError(
            "Only SELECT statements are permitted "
            "(SELECT/WITH/UNION; SET, DDL, DML, bare VALUES rejected)."
        )

    for op_name in (
        "Insert", "Update", "Delete", "Merge",
        "Create", "Drop", "AlterTable", "Truncate", "Grant", "Copy",
    ):
        op_cls = getattr(exp, op_name, None)
        if op_cls and ast.find(op_cls):
            raise ValidationError(
                f"{op_name} is not allowed — only read-only SELECT."
            )

    # 잠금/쓰기 절 — SELECT 만으로 가능한 부수효과를 차단한다.
    #   FOR UPDATE/SHARE  : reader role 의 SELECT grant 에 잠금 권한이 포함돼
    #                       그대로 실행돼 writer(cost-recorder)를 최대
    #                       statement_timeout 만큼 블로킹한다 (반복 호출 시 DoS).
    #   SELECT INTO       : 테이블 생성. reader role 에 CREATE 가 없어 DB 가
    #                       막지만 검증 의도상 여기서 거절한다 (심층 방어).
    # ⚠️ Select 노드만 보면 안 된다 — `(SELECT ...) FOR UPDATE` 는 locks 가
    #    괄호 래핑된 Subquery root 에 붙는다(sqlglot 실측 — R17 claude #3).
    #    Lock/Into 노드는 AST 어디에 있든 거절한다.
    if ast.find(exp.Lock):
        raise ValidationError(
            "Locking clauses (FOR UPDATE/FOR SHARE) are not allowed."
        )
    if ast.find(exp.Into):
        raise ValidationError("SELECT INTO is not allowed.")

    # 위험 함수 거부 — 함수 인자의 SQL 문자열은 AST 에 Table 노드로 안 잡혀
    # 테이블 화이트리스트를 완전히 우회한다 (R17-S1 실측:
    # `QUERY_TO_XML('select content from chat_agent.messages', ...)` 통과 —
    # chat_agent.* 에 reader SELECT grant 가 있어 다른 관리자의 대화를 읽는
    # 경로가 실제로 열려 있었다). 파일/세션/잠금/네트워크 계열도 같은 이유로 거부.
    _DENIED_FN = re.compile(
        r"^(?:"
        r"pg_.*|set_config|current_setting|"           # 세션/파일/잠금/백엔드 제어
        r"dblink.*|postgres_fdw.*|"                     # 외부 연결
        r"lo_.*|"                                       # large object 파일 접근
        r".*to_xml.*|xpath|"                            # 문자열 SQL 실행(XML 계열)
        r"ts_(?:stat|rewrite)|"                         # 문자열 SQL 실행(tsquery 계열)
        r"(?:next|set|curr)val|txid_.*|"                # 시퀀스/트랜잭션 부수효과
        r"http_.*|curl.*"                               # 네트워크 egress 확장
        r")$",
        re.IGNORECASE,
    )
    for fn in ast.find_all(exp.Func):
        # ⚠️ 이름 추출은 노드 타입에 따라 다르다 (R17 실측):
        #    - exp.Anonymous(미등록 함수): sql_name() 은 'ANONYMOUS' 를 반환하고
        #      실제 이름은 .name 에 있다 (query_to_xml 등).
        #    - 타입드 함수(exp.Concat 등): .name 은 Expression.name = 첫 인자
        #      리터럴이 돼 concat('http_', x) 가 'http_' 로 오인되어 과거 구현은
        #      정상 함수를 거절했다 — sql_name() 이 정답.
        if isinstance(fn, exp.Anonymous):
            name = (getattr(fn, "name", "") or "").lower()
        else:
            try:
                name = (fn.sql_name() or "").lower()
            except Exception:  # noqa: BLE001
                name = ""
        if name and _DENIED_FN.match(name):
            raise ValidationError(
                f"Function '{name}' is not allowed in read-only queries."
            )

    # 3. 모든 참조 테이블이 화이트리스트에 있는지
    #    traverse_scope 는 SQL 의 실제 이름 해석을 따라간다 — CTE 별칭은
    #    Scope(비테이블)로, 실테이블만 Table 노드로 나타나므로
    #    `WITH daily AS (...) SELECT * FROM daily` 가 정상 통과하고,
    #    스코프 밖에서 CTE 이름을 도용해 실테이블을 가리키는 우회는 잡힌다
    #    (과거 단순 find_all(Table) + 전역 CTE 이름 집합은 둘 다 실패했다).
    from sqlglot.optimizer.scope import traverse_scope

    allowed = allowed_table_set()
    try:
        scopes = traverse_scope(ast)
        table_sources = [
            src
            for scope in scopes
            for src in scope.sources.values()
            if isinstance(src, exp.Table)
        ]
    except Exception as e:  # noqa: BLE001 — 스코프 해석 불가 = fail closed
        raise ValidationError(f"Could not resolve table references: {e}") from e

    # 커버리지 검사(fail closed) — traverse_scope 가 방문하지 않는 AST 영역에
    #    Table 노드가 남아 있으면 화이트리스트 우회다(실측: `LATERAL (VALUES
    #    ((SELECT ... FROM t)))` 내부 t). 면제는 노드가 속한 **같은 스코프**에서
    #    이름이 Scope 로 해석된 경우뿐 — 전역 CTE 이름 집합은 다른 스코프의
    #    동명 CTE로 실테이블을 숨기는 우회를 연다(claude R17 M1:
    #    `WITH pg_roles AS (SELECT 1) ... LATERAL (VALUES((SELECT ... FROM
    #    pg_roles)))` — 안쪽 pg_roles 는 실테이블인데 이름이 면제돼 통과했다).
    #    UDTF(FROM generate_series)는 name 이 빈 Table 이라 별도 스킵 — 위
    #    denylist 가 함수 자체를 이미 거절한다.
    resolved_ids = {
        id(src)
        for scope in scopes
        for src in scope.sources.values()
        if isinstance(src, exp.Table)
    }
    covered_ids = set()
    for scope in scopes:
        for t in scope.tables:
            if id(t) in resolved_ids:
                covered_ids.add(id(t))
                continue
            src = scope.sources.get(t.name)
            # sqlglot 자체 규칙(_traverse_tables)과 맞춘다 — 스키마 한정이
            #    있는 노드는 절대 CTE 참조로 면제하지 않는다 (claude R17-3 S1).
            if (
                src is not None
                and not isinstance(src, exp.Table)
                and not t.args.get("db")
            ):
                covered_ids.add(id(t))  # CTE/서브쿼리 참조 — 이 스코프 한정
    for node in ast.find_all(exp.Table):
        if not node.name:
            continue
        if id(node) not in covered_ids:
            raise ValidationError(
                f"Could not resolve table reference: {node.sql()}"
            )

    for table in table_sources:
        # table.text("db") returns the schema as a plain string ("" if absent);
        # table.args.get("db") returns an Identifier object → no .lower(). Use text().
        # ⚠️ normalize_identifiers 이후라 비인용 이름은 이미 소문자 — 소문자
        #    변환을 다시 하면 인용된 "USAGE_LOGS" 같은 별개 릴레이션이 화이트
        #    리스트를 통과한다(claude R17-4 S1). 정확 비교로 둔다.
        name = table.name or ""
        if not name:
            # FROM generate_series(...) 등 UDTF — this 가 Identifier 가 아니라
            # Func 라 name 이 비어 있다. 실테이블이 아니므로 스킵 (함수 자체는
            # 위 denylist 검사를 받는다).
            continue
        schema = table.text("db") or "public"
        if (schema, name) not in allowed:
            raise ValidationError(
                f"Table '{schema}.{name}' is not in the whitelist. "
                f"Allowed tables: {sorted(allowed)}"
            )

    # forbidden column — 컬럼 이름이 forbidden 에 매치되면 reject.
    # ⚠️ 과거엔 `db.table.column` 완전 FQCN 일치만 걸렀다 — `password_hash`
    #    (비수식)나 `u.password_hash`(별칭)는 전부 빠져나갔다 (R17). 실제 차단은
    #    DB 컬럼 GRANT 가 했지만 검증 의도상 이름 기준으로 바로잡는다:
    #    forbidden 항목의 마지막 세그먼트(컬럼명)와 동일한 이름의 컬럼 참조를
    #    거절한다. 허용 테이블의 동명 컬럼까지 막을 수 있으나, 항목들이
    #    `sso_subject`/`key_value_encrypted` 처럼 민감 전용 이름이라 실무상
    #    false positive 가 없다.
    # ⚠️ `SELECT u.*` / `row_to_json(u)` 처럼 이름을 명시하지 않는 참조는 AST 에
    #    Column 노드를 남기지 않아 이 검사를 통과한다 — 최종 차단선은 DB 의
    #    컬럼 단위 GRANT 다(08_create_chat_reader.sql 에서 민감 컬럼 미부여).
    forbidden = forbidden_columns()
    if forbidden:
        forbidden_names = {f.lower().split(".")[-1] for f in forbidden}
        for col in ast.find_all(exp.Column):
            if col.name.lower() in forbidden_names:
                raise ValidationError(
                    f"Column '{col.name}' is forbidden"
                )

    return ast


def force_limit(ast: sqlglot.Expression) -> str:
    """4단계 — LIMIT 강제. 이미 LIMIT 있으면 작은 값 채택.

    ⚠️ R17-S1: `ast.find(exp.Select)` 는 첫 Select 만 반환해서 UNION root 에는
    첫 브랜치에만 LIMIT 이 붙고 총 반환 행이 한도×브랜치 수로 늘었다.
    - plain Select root: LIMIT 를 제자리에 설정 — 서브쿼리로 감싸면 안쪽
      ORDER BY 의 순서 보장이 SQL 스펙상 사라지므로(실측 Postgres 는 보존하지만
      의존하지 않는다) root 가 Select 일 때는 직접 설정한다.
    - Union/Intersect/Except/With/Values root: `a UNION b LIMIT n` 은 Postgres
      에서 전체 union 에 적용되지만, root 타입별로 직접 LIMIT 의 렌더 위치와
      의미가 달라 서브쿼리 래핑이 가장 보수적으로 일관된다(kiro R17-5 N3 — 구
      주석의 "마지막 브랜치에만 걸린다" 는 부정확).
    root LIMIT(union 포함)이 있으면 더 작은 값을 채택해 호출자 상한을 존중.
    """
    limit = QUERY_LIMIT
    existing_limit = ast.args.get("limit")
    def _literal_nonneg_int(node: sqlglot.Expression | None) -> int | None:
        # kiro R17-8 N-c — 진짜 정수 리터럴만 인정한다. int(node.this) 는
        # HexString('10') 을 10 으로 읽거나(PG16+ 에선 16, 이하는 구문 오류),
        # bigint 오버플로 리터럴을 min() 으로 성공시켜 원래 의미/오류를 바꾼다.
        # 형태(숫자만)와 범위(bigint)를 같이 검사해 아니면 원래 절을 보존한다.
        if not isinstance(node, exp.Literal) or node.is_string:
            return None
        if not re.fullmatch(r"\d+", node.this):
            return None
        value = int(node.this)
        return value if value <= 9_223_372_036_854_775_807 else None

    preserve_clause = False  # 내부 절을 지우지 않고 서브쿼리로 감싸야 하는 경우
    if isinstance(existing_limit, exp.Fetch):
        # FETCH FIRST n ROWS — count 는 expression 이 아니라 args["count"]
        # (kiro R17-5 S3). WITH TIES/PERCENT/비리터럴 count 는 LIMIT 의미론과
        # 달라 제자리 치환 금지 — 쿼리를 감싸 원래 절을 보존하고 바깥 LIMIT 으로
        # 상한만 둔다. `FETCH FIRST ROW ONLY`(count 생략)는 1로 취급(R17-6 S3).
        count_expr = existing_limit.args.get("count")
        opts = existing_limit.args.get("limit_options")
        percent = bool(opts and opts.args.get("percent"))
        ties = bool(opts and opts.args.get("with_ties"))
        count: int | None = None
        if count_expr is None:
            count = 1 if not percent else None
        else:
            count = _literal_nonneg_int(count_expr)
        if percent or ties or count is None:
            preserve_clause = True
        else:
            limit = min(limit, count)
    elif existing_limit:
        # kiro R17-7 N-1 — `LIMIT (2+3)`(5행)이나 `LIMIT 2.9`(3행)를 LIMIT
        # 1000 으로 덮으면 의미가 넓어진다. 음수 리터럴은 PG 가 거부해야 할
        # 문인데 상한으로 치환하면 오류가 성공으로 바뀐다. 리터럴 정수이고
        # 0 이상일 때만 제자리 축소, 나머지는 FETCH 와 같은 방식으로 감싸
        # 원래 절(=원래 의미/오류)을 보존한다.
        limit_value = _literal_nonneg_int(existing_limit.expression)
        if limit_value is None:
            preserve_clause = True
        else:
            limit = min(limit, limit_value)
    if isinstance(ast, exp.Select) and not preserve_clause:
        ast.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
        return ast.sql(dialect="postgres")
    outer = QUERY_LIMIT if preserve_clause else limit
    return (
        f"SELECT * FROM ({ast.sql(dialect='postgres')}) AS _q LIMIT {outer}"
    )


# ─── DB connection ───
def get_db_connection():
    secret = secrets.get_secret_value(SecretId=DB_SECRET_ARN)
    secret_data = json.loads(secret["SecretString"])
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=secret_data["password"],
        connect_timeout=5,
        sslmode="require",
        # NOTE: do NOT pass libpq `options=-c ...` — RDS Proxy rejects command-line
        # options (it multiplexes connections). Set statement_timeout via SET below.
        # The gateway_chat_reader role also has statement_timeout=10s at role level.
    )
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def explain_cost(conn, sql: str) -> float:
    """5단계 — EXPLAIN 으로 비용 추정."""
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        row = cur.fetchone()
        if not row:
            return 0.0
        plan = row[0][0]["Plan"]
        return float(plan.get("Total Cost", 0))


def _json_safe(value: Any) -> Any:
    """PG 값을 JSON 직렬화 가능한 형태로 정규화.

    Lambda 런타임은 핸들러 반환을 자체 json.dumps(default 없음)로 마샬링하므로
    datetime/date/Decimal/UUID/bytes 등이 남으면 Runtime.MarshalError 로 죽는다
    (시계열 use case 가 requested_at/day 컬럼 때문에 광범위하게 실패하던 원인).
      - Decimal → float (cost_usd 등 수치 의미 보존)
      - datetime/date/time → ISO 문자열
      - UUID → 문자열
      - bytes/memoryview → (노출 안 되게) repr 길이만
    """
    import datetime as _dt
    import decimal as _dec
    import uuid as _uuid

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _dec.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))}B>"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def execute_and_collect(conn, sql: str) -> tuple[list[dict], list[dict]]:
    """6단계 — 실행 + 결과 + 컬럼 메타 반환. 행은 JSON-safe 로 정규화."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        columns = [
            {"name": col.name, "type_oid": col.type_code}
            for col in (cur.description or [])
        ]
    return [_json_safe(dict(r)) for r in rows], columns


def compute_stats(rows: list[dict]) -> dict | None:
    """숫자 컬럼별 결정적 요약(min/max/mean/sum) + 단일 숫자컬럼이면 행별 비중%.

    LLM 이 인사이트 헤드라인("X 가 전체의 35%")을 지어내지 않고 **이 결정적
    필드를 인용**하게 한다(§55 insight-first — deep-insight _df_summary 패턴).
    전체 rows(LIMIT 적용분) 기준이라 인라인 샘플(INLINE_ROWS)보다 정확.
    LLM 비용 0 — 순수 Python.
    """
    if not rows:
        return None
    numeric_cols: dict[str, list[float]] = {}
    for col, val in rows[0].items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            numeric_cols[col] = []
    if not numeric_cols:
        return None
    for r in rows:
        for col in numeric_cols:
            v = r.get(col)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric_cols[col].append(float(v))
    stats: dict[str, Any] = {}
    for col, vals in numeric_cols.items():
        if not vals:
            continue
        total = sum(vals)
        stats[col] = {
            "min": min(vals),
            "max": max(vals),
            "mean": round(total / len(vals), 6),
            "sum": round(total, 6),
            "count": len(vals),
        }
    # 숫자 컬럼이 1개고 행수가 적당하면(≤20) 행별 비중% — "top N 점유율" 인용용.
    if len(stats) == 1:
        col = next(iter(stats))
        total = stats[col]["sum"]
        if total and len(rows) <= 20:
            label_col = next(
                (c for c in rows[0] if c != col), None
            )
            if label_col:
                stats[col]["share_pct"] = [
                    {
                        "label": str(r.get(label_col)),
                        "pct": round(float(r.get(col, 0) or 0) / total * 100, 2),
                    }
                    for r in rows
                ]
    return stats or None


def assert_resultset(rows: list[dict], columns: list[dict], stats: dict | None) -> list[str]:
    """L1 — 결과셋 결정적 어서션(LLM 추가호출 0, DEVLOG §58).

    실행된 결과셋에 결정적 술어를 걸어 '조용히 틀린 숫자'의 신호를 잡는다.
    _reconcile_numbers 는 본문 숫자가 envelope 유래인지만 보지만(환각만 탐지),
    이 어서션은 '결과셋 자체가 말이 되는가'를 본다 — fan-out(②)·필터누락(⑧)·
    정렬깨짐 같은 의미오류의 실행기측 신호. 위반은 warnings 로 fail-soft.

    어서션은 결정적·검증가능한 술어만(부호/단조성/NULL/정렬) — LLM 환각 위험 0.
    """
    warnings: list[str] = []
    if not rows:
        return warnings

    numeric_names = set((stats or {}).keys())

    # 1) measure 음수 — 비용/토큰/지연은 음수 불가(데이터/집계 오류 신호)
    for col in numeric_names:
        s = stats[col]
        nonneg_hint = any(k in col.lower() for k in ("cost", "token", "calls", "count", "latency", "requests"))
        if nonneg_hint and s.get("min", 0) < 0:
            warnings.append(
                f"결과셋 이상: `{col}` 에 음수({s['min']}) — 비용/토큰/건수는 음수 불가. "
                f"집계나 조인이 잘못됐을 수 있음."
            )

    # 2) 정렬 단조성 — ORDER BY DESC 류 ranking 의도인데 첫 숫자 컬럼이 비단조면
    #    상위 N 순위가 깨진 신호(잘못된 GROUP BY/중복행).
    if numeric_names and len(rows) >= 3:
        first_num = next(iter(numeric_names))
        vals = [r.get(first_num) for r in rows if isinstance(r.get(first_num), (int, float))]
        if len(vals) >= 3:
            desc = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
            asc = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
            if not (desc or asc):
                # 정렬 자체가 없을 수도 있으니 약한 신호로만(WARN)
                warnings.append(
                    f"결과셋 주의: `{first_num}` 가 단조 정렬이 아님 — ranking 질의라면 "
                    f"ORDER BY 누락/중복행 가능성(결과 순서 확인)."
                )

    # 3) 단일 행 + ranking 의심: row_count==1 인데 여러 라벨 컬럼 → 과집계 신호는
    #    상위(sql_specialist note)에서 다루므로 여기선 생략(중복 경고 방지).
    return warnings


# ─── S3 staging ───
def upload_to_staging(rows: list[dict], session_id: str, step_id: str) -> str:
    """7단계 — Parquet 업로드. 큰 결과를 Code Specialist 에게 전달."""
    # MVP: 일단 JSON Lines 로 (parquet 은 pyarrow layer 필요)
    body = "\n".join(json.dumps(r, default=str) for r in rows).encode("utf-8")
    # staging/ prefix — 중간 데이터(1일 만료). reports/(7일)와 키 공간 분리해
    # lifecycle 을 prefix 별로 독립 제어(§49). 과거 평면 키({session}/...)는 broad
    # 규칙이 reports 까지 1일에 삭제하는 문제가 있었음.
    # ⚠️ session_id/step_id 는 LLM 이 tool 인자로 넣는 값이다 — 신뢰 입력이
    #    아니므로 키 문자셋으로 살균한다(다른 세션 키 덮어쓰기/이상 키 방지, R17).
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "-", str(session_id))[:64]
    safe_step = re.sub(r"[^A-Za-z0-9_-]", "-", str(step_id))[:64]
    key = f"staging/{safe_sid}/{safe_step}.jsonl"
    s3.put_object(
        Bucket=S3_STAGING_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/x-ndjson",
        ServerSideEncryption="aws:kms",
    )
    return f"s3://{S3_STAGING_BUCKET}/{key}"


# ─── Lambda entrypoint ───
def lambda_handler(event: dict, context: Any) -> dict:
    sql = event.get("sql", "").strip()
    session_id = event.get("session_id") or str(uuid.uuid4())
    step_id = event.get("step_id") or str(uuid.uuid4())

    if not sql:
        return {"ok": False, "error": "SQL is required"}

    started = time.perf_counter()

    try:
        # 1, 2, 3 — AST validation
        ast = validate_sql(sql)

        # L0 — 결정적 정확도 가드(컬럼 해석/타임존/fan-out/대시보드 정합, §58).
        # errors 는 reject(self-correction 피드백), warnings 는 결과에 동봉.
        accuracy_warnings: list[str] = []
        if sql_guard is not None:
            try:
                g = sql_guard.guard(sql, get_whitelist())
                if g.get("errors"):
                    return {
                        "ok": False,
                        "error": "; ".join(g["errors"]),
                        "hint": "Fix the SQL per the validation message and retry.",
                    }
                accuracy_warnings.extend(g.get("warnings", []))
            except Exception as e:  # noqa: BLE001 — 가드 자체 오류가 본 경로 막지 않게
                logger.warning(f"sql_guard failed (non-fatal): {e}")

        # 4 — LIMIT 강제
        bounded_sql = force_limit(ast)

        with get_db_connection() as conn:
            # 5 — EXPLAIN
            cost = explain_cost(conn, bounded_sql)
            if cost > EXPLAIN_COST_LIMIT:
                return {
                    "ok": False,
                    "error": f"Estimated cost {cost:.0f} exceeds limit {EXPLAIN_COST_LIMIT:.0f}",
                    "hint": "Add WHERE clauses to narrow the query, or aggregate further.",
                }

            # 6 — 실행
            rows, columns = execute_and_collect(conn, bounded_sql)

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # L1 — 결과셋 결정적 어서션(§58). compute_stats 결과 위에 술어 평가.
        result_stats = compute_stats(rows)
        try:
            accuracy_warnings.extend(assert_resultset(rows, columns, result_stats))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assert_resultset failed (non-fatal): {e}")

        # 7 — S3 staging (행이 많을 때만 가치, 인라인 한도 이하면 inline)
        s3_uri: str | None = None
        if len(rows) > INLINE_ROWS:
            try:
                s3_uri = upload_to_staging(rows, session_id, step_id)
            except Exception as e:
                logger.warning(f"S3 staging failed: {e}")
                # graceful — staging 실패해도 sample 결과는 반환

        return {
            "ok": True,
            "sql": bounded_sql,
            # 인라인 sample 행수. 5행이던 것을 20행으로 — "top 10" 류 질의가 5건만
            # 표시되던 문제(§52) 해소. 20행 초과분은 s3_uri 로(Code Specialist 용).
            "rows": rows[:INLINE_ROWS],
            "row_count": len(rows),
            "columns": columns,
            # 결정적 숫자 요약(§55 insight-first) — LLM 이 비중%/합계를 지어내지
            # 않고 이 필드를 인용. 전체 rows 기준이라 샘플 추론보다 정확.
            "stats": result_stats,
            # L0/L1 결정적 정확도 경고(§58) — fail-soft. orchestrator/validator 가
            # 이 신호를 보고 재생성/검증 강화, deep 모드는 스트리밍으로 노출.
            "accuracy_warnings": accuracy_warnings,
            "s3_uri": s3_uri,
            "explain_cost": cost,
            "elapsed_ms": elapsed_ms,
        }

    except ValidationError as e:
        return {"ok": False, "error": str(e), "hint": "Fix the SQL and retry."}
    except psycopg2.errors.QueryCanceled:
        return {
            "ok": False,
            "error": f"Query timed out after {STATEMENT_TIMEOUT_MS}ms",
            "hint": "Add WHERE filters to reduce scan size.",
        }
    except Exception as e:
        logger.exception("query_db unexpected error")
        return {"ok": False, "error": f"Unexpected error: {type(e).__name__}: {e}"}
