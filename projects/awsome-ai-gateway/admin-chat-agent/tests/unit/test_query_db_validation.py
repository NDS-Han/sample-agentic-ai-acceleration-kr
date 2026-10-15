# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R17-S1 — query_db Lambda validate_sql/force_limit 회귀.

배경: 검증 로직이 "비-SELECT root 일 때만 DML 스캔"이라 CTE 안의 DML,
SET/DISCARD 같은 세션 조작문, FOR UPDATE 잠금, SELECT INTO 테이블 생성,
query_to_xml 계열 문자열-SQL 우회가 전부 통과했다. sqlglot 의 Anonymous 노드
함수명 함정(sql_name() → 'ANONYMOUS')과 CTE 별칭이 Table 노드로 잡히는
false-positive 도 함께 커버한다.

psycopg2 는 Lambda 전용 의존이라 스텁으로 대체한다.
"""

import os
import sys
import types

import pytest

_pg = types.ModuleType("psycopg2")
_pg.__path__ = []
_pg.extras = types.ModuleType("psycopg2.extras")
sys.modules.setdefault("psycopg2", _pg)
sys.modules.setdefault("psycopg2.extras", _pg.extras)

_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "lambdas", "query_db"
)
sys.path.insert(0, os.path.abspath(_LAMBDA_DIR))

os.environ.setdefault("DB_HOST", "x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("DB_USER", "x")
os.environ.setdefault("DB_SECRET_ARN", "x")
os.environ.setdefault("S3_STAGING_BUCKET", "x")
os.environ.setdefault(
    "SCHEMA_WHITELIST_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "schema_whitelist.yaml"),
)

import lambda_function as L  # noqa: E402

OK_QUERIES = [
    "SELECT user_id, cost_usd FROM usage.usage_logs LIMIT 10",
    "WITH daily AS (SELECT * FROM usage.usage_logs) SELECT * FROM daily",
    # CTE 이름이 실테이블과 같아도 CTE 참조는 허용
    "WITH usage_logs AS (SELECT * FROM usage.usage_logs) SELECT * FROM usage_logs",
    "SELECT * FROM usage.usage_logs u JOIN auth.users x ON u.user_id = x.id",
    "SELECT concat('a', 'b') FROM usage.usage_logs",
    "SELECT coalesce(model_alias, 'x') FROM usage.usage_logs",
    "SELECT count(*) FROM usage.usage_logs ORDER BY 1",
    "SELECT * FROM usage.usage_logs UNION ALL SELECT * FROM usage.usage_logs",
    # normalize_identifiers — 비인용 대문자는 Postgres 와 같이 소문자 폴딩
    "SELECT * FROM USAGE.USAGE_LOGS LIMIT 1",
    "WITH Daily AS (SELECT count(*) c FROM usage.usage_logs) SELECT * FROM DAILY",
]

REJECT_QUERIES = [
    # CTE 내부 DML 우회 (과거: root=Select 라 스캔 스킵)
    "WITH x AS (DELETE FROM usage.usage_logs RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE usage.usage_logs SET cost_usd=0 RETURNING *) SELECT * FROM x",
    # 비-SELECT 세션/환경 조작문 (과거: 타입 검사 없이 통과)
    "SET statement_timeout = 0",
    "LISTEN foo",
    "DISCARD ALL",
    # 행 잠금 — writer 블로킹 DoS
    "SELECT * FROM usage.usage_logs FOR UPDATE",
    "SELECT * FROM usage.usage_logs FOR NO KEY UPDATE",
    "SELECT * FROM usage.usage_logs FOR KEY SHARE",
    "SELECT * FROM usage.usage_logs FOR SHARE",
    # 테이블 생성
    "SELECT * INTO tmp FROM usage.usage_logs",
    # 문자열 SQL 실행 / 파일·세션·시퀀스·네트워크 함수
    "SELECT query_to_xml('select 1', true, false, '')",
    "SELECT table_to_xml('usage.usage_logs', true, false, '')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('statement_timeout','0',false)",
    "SELECT dblink('x','select 1')",
    "SELECT ts_stat('select 1')",
    "SELECT ts_rewrite('x','select 1')",
    "SELECT nextval('seq')",
    "SELECT setval('seq', 1)",
    "SELECT lo_from_bytea(0, decode('00','hex'))",
    "SELECT lo_import('/etc/passwd')",
    # 금지 컬럼 — 수식/비수식/별칭 전부
    "SELECT u.sso_subject FROM auth.users u",
    "SELECT sso_subject FROM auth.users",
    "SELECT v.key_value_encrypted FROM auth.virtual_keys v",
    # 비화이트리스트 스키마/테이블
    "SELECT * FROM chat_agent.messages",
    "SELECT * FROM audit.audit_logs",
    # CTE 스코프 밖에서 같은 이름의 실테이블 참조(우회 방지)
    "SELECT * FROM (WITH alembic_version AS (SELECT 1) SELECT * FROM alembic_version) a, alembic_version",
    # 멀티문장
    "SELECT * FROM usage.usage_logs; DROP TABLE auth.users",
    # claude R17 — bare VALUES root 는 traverse_scope 가 내부를 안 타서
    # 서브쿼리 속 비화이트리스트 테이블이 검사를 통과했다
    "VALUES ((SELECT string_agg(query, ' | ') FROM pg_catalog.pg_stat_activity))",
    "VALUES (1,2)",
    "WITH v AS (VALUES ((SELECT string_agg(rolname, ',') FROM pg_roles))) SELECT * FROM v",
    "SELECT 1 UNION ALL VALUES ((SELECT datname FROM pg_database))",
    # claude R17 — 괄호 래핑 root 의 locks 는 Select 가 아니라 Subquery 에 붙는다
    "(SELECT * FROM usage.usage_logs) FOR UPDATE",
    "(SELECT * FROM usage.usage_logs FOR UPDATE) s",
    "(SELECT * INTO tmp FROM usage.usage_logs)",
    # claude R17-2 M1 — scope-local 면제 검증: traverse_scope 사각지대(LATERAL
    # 안의 VALUES 서브쿼리)에 있는 Table 노드가 다른 스코프의 동명 CTE 로
    # 위장해 면제되던 우회
    "WITH pg_roles AS (SELECT 1 AS x) SELECT * FROM usage.usage_logs u, LATERAL (VALUES ((SELECT rolname FROM pg_roles))) v(r)",
    "SELECT * FROM usage.usage_logs u, LATERAL (VALUES ((SELECT rolname FROM pg_roles))) v(r)",
    "WITH pg_roles AS (VALUES ((SELECT string_agg(rolname, ',') FROM pg_roles))) SELECT * FROM pg_roles",
    "WITH a AS (VALUES((SELECT datname FROM pg_stat_activity))), pg_stat_activity AS (SELECT 1) SELECT * FROM a",
    # claude R17-3 M1 — sqlglot CTE 매칭은 대소문자 구분, Postgres 는 비인용
    # 식별자를 소문자 폴딩 → 외부 PG_ROLES 는 실제 pg_catalog.pg_roles
    'WITH "PG_ROLES" AS (SELECT 1) SELECT * FROM PG_ROLES',
    # 비화이트리스트 스키마가 대소문자 변형으로도 못 빠져나가는지
    "SELECT * FROM Usage.Usage_Logs x JOIN Pg_Roles p ON true",
    "SELECT * FROM PG_CATALOG.PG_ROLES",
    # 인용된 대문자 — normalize 후에도 대소문자 유지 → 화이트리스트 불일치
    'SELECT * FROM "USAGE"."USAGE_LOGS"',
    # 인용된 위험 함수명도 denylist 에 걸려야 한다
    'SELECT "PG_READ_FILE"(\'/x\') FROM usage.usage_logs',
    # 인용 CTE 의 대소문자 불일치 참조는 CTE 가 아니라 실테이블(=비화이트리스트)
    'WITH "pg_roles" AS (SELECT 1) SELECT * FROM "PG_ROLES"',
]


@pytest.mark.parametrize("sql", OK_QUERIES)
def test_legitimate_queries_pass(sql):
    L.validate_sql(sql)


@pytest.mark.parametrize("sql", REJECT_QUERIES)
def test_dangerous_queries_rejected(sql):
    with pytest.raises(L.ValidationError):
        L.validate_sql(sql)


def test_force_limit_caps_plain_select_and_respects_smaller():
    ast = L.validate_sql("SELECT id FROM usage.usage_logs")
    assert f"LIMIT {L.QUERY_LIMIT}" in L.force_limit(ast)
    ast2 = L.validate_sql("SELECT id FROM usage.usage_logs LIMIT 5")
    assert "LIMIT 5" in L.force_limit(ast2)


def test_force_limit_wraps_union_so_total_is_bounded():
    ast = L.validate_sql(
        "SELECT id FROM usage.usage_logs UNION ALL SELECT id FROM usage.usage_logs"
    )
    out = L.force_limit(ast)
    # 전체를 서브쿼리로 감싸 총량을 묶는다 — 브랜치당 LIMIT 이 아니라 외부 LIMIT.
    assert out.startswith("SELECT * FROM (")
    assert out.rstrip().endswith(f"LIMIT {L.QUERY_LIMIT}")


def test_force_limit_preserves_order_by_on_plain_select():
    # Select root 는 서브쿼리 래핑 없이 제자리 LIMIT — ORDER BY 의미 보존.
    ast = L.validate_sql("SELECT id FROM usage.usage_logs ORDER BY id")
    out = L.force_limit(ast)
    assert "ORDER BY" in out
    assert not out.startswith("SELECT * FROM (")


# ─── force_limit 의 FETCH 절 처리 (kiro R17-5 S3) ───
def test_force_limit_fetch_first_becomes_capped_limit():
    ast = L.validate_sql("SELECT id FROM usage.usage_logs FETCH FIRST 5 ROWS ONLY")
    out = L.force_limit(ast)
    assert "LIMIT 5" in out
    assert "FETCH" not in out


def test_force_limit_fetch_over_cap_is_clamped():
    ast = L.validate_sql("SELECT id FROM usage.usage_logs FETCH FIRST 5000 ROWS ONLY")
    out = L.force_limit(ast)
    assert f"LIMIT {L.QUERY_LIMIT}" in out
    assert "FETCH" not in out


def test_force_limit_fetch_with_ties_is_preserved():
    ast = L.validate_sql(
        "SELECT id FROM usage.usage_logs ORDER BY id DESC FETCH FIRST 5 ROWS WITH TIES"
    )
    out = L.force_limit(ast)
    # WITH TIES 의미론(동순위 초과 행)을 살리려 원래 FETCH 를 남기고 바깥 LIMIT 으로 상한.
    assert "WITH TIES" in out
    assert out.startswith("SELECT * FROM (")
    assert out.rstrip().endswith(f"LIMIT {L.QUERY_LIMIT}")
