# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""워커가 per-app ``budget_usages`` 행을 쓰는지, 그리고 한도 스냅샷이 섞이지 않는지.

배경 — 두 결함이 같은 SQL 에 있었다.

**(1) 앱별 행을 아무도 쓰지 않았다.** 이 워커는 ``client`` 컬럼에 ``NULL`` 을 하드코딩해
총합 행만 적재했다. 그래서 per-app 예산 상한이 **Redis 카운터로만** 존재했고:

  * Redis 유실/failover 후 소진된 앱 예산이 조용히 0 으로 되돌아갔다 —
    게이트웨이의 복원 경로가 읽을 행이 아예 없었다,
  * ``REDIS_DEGRADED`` 시 DB 폴백 분기가 ``client_used=0`` 을 읽어 앱 한도가 아예
    적용되지 않았다.

migration 0011 의 주석은 이미 "the worker writes total + per-app rows" 라고 적혀
있었다 — 스키마와 문서가 맞고 워커만 어긋난 상태였다.

**(2) ``limit_usd`` 스냅샷 서브쿼리에 client 술어가 없었다.** per-app config 와 총액
config 가 **같은 테이블**에 살고 정렬이 ``effective_from DESC`` 뿐이라, 총합 행의
``limit_usd`` 에 앱별 한도가 박힐 수 있었다 — 어느 쪽이 잡히는지가 비결정적이다.
admin-api 는 자기 쪽 같은 SQL 에 이미 그 술어를 걸고 있었다.

⚠️ 이 파일은 **실 PostgreSQL** 을 요구한다. 검증 대상이 ``ON CONFLICT`` 대상 인덱스와
   ``IS NOT DISTINCT FROM`` 의 NULL 의미론이라, mock 으로는 한 줄도 실행되지 않는다.

실행::

    docker run -d --name pg-proof -p 55432:5432 -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=gwproof pgvector/pgvector:pg16
    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
        pytest tests/regression/test_high_per_app_budget_rows.py
"""

from __future__ import annotations

import ast
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_appbudget"
_FLUSHER = Path(__file__).resolve().parents[2] / "src" / "worker" / "batch_flusher.py"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정적 — SQL 이 되돌아가지 않았는지
# ─────────────────────────────────────────────────────────────────────────────


def _upsert_sql() -> str:
    """``_UPSERT_BUDGET_USAGE`` 의 SQL 문자열을 AST 로 뽑는다.

    주석이 아니라 실제 SQL 을 본다 — "client 를 쓴다" 는 주석은 증거가 아니다.
    """
    tree = ast.parse(_FLUSHER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_UPSERT_BUDGET_USAGE" not in names:
            continue
        for lit in ast.walk(node.value):
            if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                if "INSERT INTO budget.budget_usages" in lit.value:
                    return lit.value
    raise AssertionError("_UPSERT_BUDGET_USAGE 의 SQL 을 찾지 못했다")


def test_client_is_a_bound_parameter_not_a_hardcoded_null():
    sql = _upsert_sql()
    assert ":client" in sql, (
        "client 가 바인드 파라미터가 아니다 — 앱별 행을 쓸 수 없다"
    )
    # 값 목록에 하드코딩 NULL 이 남아 있으면 안 된다.
    values = sql[sql.index("VALUES") : sql.index("ON CONFLICT")]
    assert "\n        NULL,\n" not in values, (
        "VALUES 절에 하드코딩된 NULL 이 남아 있다 — 총합 행만 적재된다"
    )


def test_limit_snapshot_filters_by_client():
    sql = _upsert_sql()
    subq = sql[sql.index("SELECT max_budget_usd") : sql.index("ORDER BY effective_from")]
    assert "client" in subq, (
        "limit_usd 스냅샷 서브쿼리에 client 술어가 없다 — per-app config 의 한도가 "
        "총합 행에 박힐 수 있다(비결정적)"
    )
    assert "IS NOT DISTINCT FROM" in subq, (
        "`= :client` 로는 client 가 NULL 일 때 UNKNOWN 이 되어 한 행도 매칭되지 않고 "
        "한도가 0 이 된다 — IS NOT DISTINCT FROM 이어야 한다"
    )


def test_per_app_params_are_actually_executed():
    """파라미터를 만들어 두고 execute 하지 않으면 아무 행도 안 생긴다."""
    tree = ast.parse(_FLUSHER.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_upsert_budget_usages":
                target = node
    assert target is not None, "_upsert_budget_usages 를 찾지 못했다"

    body = list(target.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = ast.dump(ast.Module(body=body, type_ignores=[]))

    assert "app_sums" in code, "앱별 누적 집계가 없다"
    assert "app_params" in code, "앱별 파라미터가 없다"
    # execute 호출이 3개(user/team/app)여야 한다.
    execs = [
        n
        for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "execute"
    ]
    assert len(execs) >= 3, (
        f"session.execute 가 {len(execs)}번뿐이다 — app_params 가 실행되지 않는다"
    )


def test_per_app_client_set_matches_the_gateway():
    """워커의 client 집합이 게이트웨이의 것과 같아야 한다.

    이쪽이 좁으면 그 앱의 누적 행이 없어 복원이 0 이 되고(한도 소멸), 넓으면 아무도
    읽지 않는 행이 쌓인다.
    """
    src = _FLUSHER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    worker_set = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PER_APP_CLIENTS" for t in node.targets
        ):
            worker_set = set(ast.literal_eval(node.value))
    assert worker_set, "_PER_APP_CLIENTS 를 찾지 못했다"

    gw = (
        Path(__file__).resolve().parents[3]
        / "gateway-proxy"
        / "src"
        / "app"
        / "services"
        / "budget_service.py"
    )
    assert gw.is_file(), f"gateway-proxy budget_service 를 찾지 못했다: {gw}"
    gtree = ast.parse(gw.read_text(encoding="utf-8"))
    gw_set = None
    for node in ast.walk(gtree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PER_APP_BUDGET_CLIENTS" for t in node.targets
        ):
            gw_set = set(ast.literal_eval(node.value))
    assert gw_set, "게이트웨이의 PER_APP_BUDGET_CLIENTS 를 찾지 못했다"

    assert worker_set == gw_set, (
        f"워커 {sorted(worker_set)} ≠ 게이트웨이 {sorted(gw_set)} — "
        "워커가 안 쓰는 client 는 복원할 행이 없어 한도가 사라진다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 PostgreSQL — UPSERT 동작
# ─────────────────────────────────────────────────────────────────────────────

pg = pytest.mark.skipif(
    not PROOF_DSN,
    reason="PROOF_DSN 미설정 — ON CONFLICT 대상 인덱스와 NULL 의미론은 실 PG 에서만 증명된다",
)

USER_ID = uuid.UUID("cccc0000-0000-4000-a000-000000000001")
TEAM_ID_PLACEHOLDER = None  # 시드 팀에서 채운다
PERIOD = "2026-06"


def _split(dsn: str):
    prefix, _, name = dsn.rpartition("/")
    return prefix, name


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _make_db() -> str:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    owned = f"{name}{_OWNED_SUFFIX}"
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


async def _drop_db() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, name = _split(PROOF_DSN)
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}{_OWNED_SUFFIX}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    url = await _make_db()

    conn = await asyncpg.connect(_asyncpg(url))
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"  [init] {f.name}: {type(e).__name__}")

        # ⚠️ ON CONFLICT 대상이 되는 부분 표현식 인덱스(migration 0011). 이게 없으면
        #    ON CONFLICT 자체가 에러라 아래 테스트가 "SQL 이 틀렸다" 로 보인다.
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_usages_scope_period_client "
            "ON budget.budget_usages (scope, scope_id, period, COALESCE(client, ''))"
        )
        n = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE indexname = 'uq_budget_usages_scope_period_client'"
        )
        assert n == 1, "ON CONFLICT 대상 인덱스를 만들지 못했다 — 아래 테스트가 공허해진다"

        team = await conn.fetchval("SELECT id FROM auth.teams LIMIT 1")
        globals()["TEAM_ID_PLACEHOLDER"] = team
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'app@example.com','App','DEVELOPER','app-sub') ON CONFLICT DO NOTHING",
            USER_ID,
            team,
        )
    finally:
        await conn.close()

    yield url
    await _drop_db()


def _entry(client: str | None, cost: str):
    from worker.schemas.cost_stream import CostStreamEntry

    return CostStreamEntry(
        request_id=str(uuid.uuid4()),
        user_id=str(USER_ID),
        team_id=str(TEAM_ID_PLACEHOLDER),
        dept_id=str(uuid.uuid4()),
        model_alias="claude-sonnet-4-6",
        provider="BEDROCK",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal(cost),
        latency_ms=10,
        requested_at="2026-06-05T00:00:00+00:00",
        completed_at="2026-06-05T00:00:01+00:00",
        period=PERIOD,
        date="2026-06-05",
        client=client,
    )


async def _flush(dsn_url: str, entries):
    """``_upsert_budget_usages`` 만 실행한다(스트림/Redis 없이)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from worker.batch_flusher import BatchFlusher

    engine = create_async_engine(dsn_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        flusher = BatchFlusher.__new__(BatchFlusher)  # __init__ 은 Redis 를 요구한다
        async with maker() as session:
            await flusher._upsert_budget_usages(session, entries)
            await session.commit()
    finally:
        await engine.dispose()


async def _rows(dsn_url: str):
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg(dsn_url))
    try:
        return {
            r["client"]: (r["used_usd"], r["limit_usd"])
            for r in await conn.fetch(
                "SELECT client, used_usd, limit_usd FROM budget.budget_usages "
                "WHERE scope = 'USER' AND scope_id = $1 AND period = $2",
                USER_ID,
                PERIOD,
            )
        }
    finally:
        await conn.close()


@pg
async def test_total_and_per_app_rows_are_both_written(dsn):
    """앱별 행이 없으면 per-app 한도는 Redis 카운터로만 존재한다."""
    await _flush(dsn, [_entry("codex", "3.00"), _entry("claude-code", "2.00")])
    rows = await _rows(dsn)
    assert None in rows, "총합 행이 없다"
    assert rows[None][0] == Decimal("5.00"), f"총합이 틀렸다: {rows[None][0]}"
    assert "codex" in rows and "claude-code" in rows, f"앱별 행이 없다: {sorted(rows)}"
    assert rows["codex"][0] == Decimal("3.00")
    assert rows["claude-code"][0] == Decimal("2.00")


@pg
async def test_per_app_rows_accumulate_on_conflict(dsn):
    """두 번째 flush 는 더해야 한다 — 덮어쓰면 그 배치 이전 사용량을 잃는다."""
    before = await _rows(dsn)
    await _flush(dsn, [_entry("codex", "1.50")])
    after = await _rows(dsn)
    assert after["codex"][0] == before["codex"][0] + Decimal("1.50")
    assert after[None][0] == before[None][0] + Decimal("1.50"), "총합도 함께 올라야 한다"


@pg
async def test_unbudgeted_clients_get_no_per_app_row(dsn):
    """``other`` 같은 client 는 per-app 예산이 없다 — 아무도 읽지 않는 행을 만들지 않는다."""
    await _flush(dsn, [_entry("other", "9.00")])
    rows = await _rows(dsn)
    assert "other" not in rows, "예산 없는 client 의 앱별 행이 생겼다"
    assert rows[None][0] >= Decimal("9.00"), "총합에는 반영돼야 한다"


@pg
async def test_null_client_entries_only_touch_the_total(dsn):
    """client 를 안 실어 보내는 구버전 엔트리도 총합에는 반영돼야 한다."""
    before = await _rows(dsn)
    await _flush(dsn, [_entry(None, "0.25")])
    after = await _rows(dsn)
    assert after[None][0] == before[None][0] + Decimal("0.25")
    assert set(after) == set(before), f"새 client 행이 생겼다: {set(after) - set(before)}"


@pg
async def test_limit_snapshot_does_not_mix_total_and_per_app_configs(dsn):
    """**핵심**: 총합 행의 limit 에 앱별 한도가 박히면 안 된다.

    총액 config($100)를 먼저, per-app config($7)를 **나중에** 만든다. 서브쿼리가
    ``effective_from DESC`` 로만 정렬되므로, client 술어가 없으면 나중에 만든 $7 이
    총합 행의 한도로 잡힌다.
    """
    asyncpg = pytest.importorskip("asyncpg")
    uid = uuid.UUID("cccc0000-0000-4000-a000-000000000002")
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'mix@example.com','Mix','DEVELOPER','mix-sub') "
            "ON CONFLICT DO NOTHING",
            uid,
            TEAM_ID_PLACEHOLDER,
        )
        sysid = await conn.fetchval("SELECT id FROM auth.users LIMIT 1")
        # 총액 config — 이른 effective_from
        await conn.execute(
            "INSERT INTO budget.budget_configs "
            "(id, scope, scope_id, client, max_budget_usd, policy, period_type, is_active, "
            " allocated_by, effective_from, created_at) "
            "VALUES (gen_random_uuid(),'USER'::budget.budget_scope,$1,NULL,100,"
            "'HARD_BLOCK'::budget.budget_policy,'MONTHLY'::budget.period_type,true,$2,"
            "DATE '2026-06-01', now())",
            uid,
            sysid,
        )
        # per-app config — **나중** effective_from
        await conn.execute(
            "INSERT INTO budget.budget_configs "
            "(id, scope, scope_id, client, max_budget_usd, policy, period_type, is_active, "
            " allocated_by, effective_from, created_at) "
            "VALUES (gen_random_uuid(),'USER'::budget.budget_scope,$1,'codex',7,"
            "'HARD_BLOCK'::budget.budget_policy,'MONTHLY'::budget.period_type,true,$2,"
            "DATE '2026-06-20', now())",
            uid,
            sysid,
        )
    finally:
        await conn.close()

    # 이 사용자로 flush
    from worker.schemas.cost_stream import CostStreamEntry

    e = _entry("codex", "1.00")
    e = CostStreamEntry(**{**e.model_dump(), "user_id": str(uid)})
    await _flush(dsn, [e])

    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        got = {
            r["client"]: r["limit_usd"]
            for r in await conn.fetch(
                "SELECT client, limit_usd FROM budget.budget_usages "
                "WHERE scope='USER' AND scope_id=$1 AND period=$2",
                uid,
                PERIOD,
            )
        }
    finally:
        await conn.close()

    assert got[None] == Decimal("100"), (
        f"총합 행의 한도가 {got[None]} — per-app config($7)가 새어 들어왔다"
    )
    assert got["codex"] == Decimal("7"), f"앱별 행의 한도가 {got['codex']} — $7 이어야 한다"
