# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``POST /internal/productivity`` 의 재전송 중복 제거.

배경 — 웹훅은 재전송된다(수신측 타임아웃·5xx·네트워크 단절). 중복 제거가 없으면 같은
이벤트가 두 번 적재되어 ROI 지표(생성/수용 줄 수)가 그만큼 부풀려진다. 조용히 틀린
숫자이고, 나중에 원인을 역추적할 방법이 없다.

⚠️ 마이그레이션 0034 가 ``idempotency_key`` 컬럼과 부분 유니크 인덱스를 넣었지만
   **읽는 코드가 없었다** — ORM 필드도, 요청 필드도, 중복 판정도 없는 죽은 컬럼이었다.
   스키마만 있고 애플리케이션이 쓰지 않으면 중복은 그대로 들어온다. 이 파일은 그
   상태로 되돌아가는 것을 막는다.

⚠️ 동시 재전송 경합은 **실 PostgreSQL 에서만** 증명된다. 두 트랜잭션이 각자
   스냅샷에서 "없다" 를 보고 둘 다 INSERT 하는 상황은 mock 으로 재현할 수 없다.
   그래서 이 파일의 행동 검증은 ``PROOF_DSN`` 을 요구한다.

실행::

    docker run -d --name pg-proof -p 55432:5432 -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=gwproof pgvector/pgvector:pg16
    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
        pytest tests/regression/test_high_productivity_idempotency.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_prodidem"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"
_MIGRATION_0034 = (
    Path(__file__).resolve().parents[3]
    / "db"
    / "versions"
    / "0034_add_missing_indexes_and_uniqueness.py"
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정적 계약 — 컬럼이 다시 죽지 않게
# ─────────────────────────────────────────────────────────────────────────────


def test_orm_model_declares_the_column():
    """ORM 필드가 없으면 INSERT 에 값이 실리지 않는다 — 컬럼은 영원히 NULL 이다."""
    from app.models.usage import ProductivityEvent

    assert "idempotency_key" in ProductivityEvent.__table__.columns, (
        "ProductivityEvent 에 idempotency_key 컬럼이 없다 — 0034 가 넣은 DB 컬럼이 "
        "애플리케이션에서 죽은 상태다"
    )


def test_orm_does_not_declare_a_full_unique_constraint():
    """DB 의 제약은 **부분** 유니크 인덱스다(``WHERE idempotency_key IS NOT NULL``).

    ORM 에 ``unique=True`` 를 붙이면 메타데이터가 실물과 어긋난다. 그리고 그 어긋남은
    조용하지 않다 — 전체 유니크는 **NULL 을 넣는 호출자**(키 없는 이벤트)를 두 번째부터
    막는 방향으로 드리프트한다.
    """
    from app.models.usage import ProductivityEvent

    col = ProductivityEvent.__table__.columns["idempotency_key"]
    assert col.unique is not True, (
        "idempotency_key 에 unique=True 가 선언됐다 — DB 는 부분 유니크 인덱스를 쓴다"
    )
    assert col.nullable is True, "키 없는 이벤트를 계속 받아야 한다"


def test_request_schema_accepts_the_key():
    from app.routers.productivity import ProductivityEventRequest

    fields = ProductivityEventRequest.model_fields
    assert "idempotency_key" in fields, "요청 스키마에 idempotency_key 가 없다"
    # 생략 가능해야 한다 — 기존 호출자를 깨뜨리면 안 된다.
    assert not fields["idempotency_key"].is_required()


def test_router_actually_dedupes_not_just_stores():
    """키를 저장만 하고 판정하지 않으면 중복 제거가 아니다.

    주석은 증거가 아니므로 docstring/주석을 걷어낸 코드에서 판정한다.
    """
    import ast

    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "app"
        / "routers"
        / "productivity.py"
    ).read_text(encoding="utf-8")

    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "record_productivity_event":
                target = node
    assert target is not None, "record_productivity_event 를 찾지 못했다"

    body = list(target.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = ast.dump(ast.Module(body=body, type_ignores=[]))

    assert "idempotency_key" in code, "라우터 실행부가 idempotency_key 를 전혀 다루지 않는다"
    # 경합을 닫는 층이 있어야 한다 — 선-조회만으로는 동시 재전송에서 500 이 난다.
    assert "begin_nested" in code, (
        "세이브포인트가 없다 — 동시 재전송 시 유니크 인덱스가 한쪽을 거부하고 500 이 된다"
    )
    assert "IntegrityError" in code, "IntegrityError 를 잡지 않는다"


def test_migration_created_the_partial_index():
    """대조군 — DB 제약이 실제로 존재하는지. 없으면 위 단정들의 전제가 깨진다."""
    src = _MIGRATION_0034.read_text(encoding="utf-8")
    assert "idx_productivity_events_idempotency" in src
    assert "WHERE idempotency_key IS NOT NULL" in src, (
        "부분 인덱스가 아니다 — NULL 을 넣는 호출자가 두 번째부터 막힌다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 행동 검증 — 실 PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

pg = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 동시 재전송 경합은 실 PG 에서만 증명된다"
)


def _split(dsn: str) -> tuple[str, str]:
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
async def db_dsn():
    asyncpg = pytest.importorskip("asyncpg")
    dsn = await _make_db()

    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"  [init] {f.name}: {type(e).__name__}")

        # 0034 가 넣는 것을 그대로 재현한다(alembic 을 태우지 않고 이 두 객체만).
        await conn.execute(
            "ALTER TABLE usage.productivity_events "
            "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_productivity_events_idempotency "
            "ON usage.productivity_events (idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
        # 대조군 — 인덱스가 실제로 만들어졌는가.
        n = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE indexname = 'idx_productivity_events_idempotency'"
        )
        assert n == 1, "부분 유니크 인덱스가 만들어지지 않았다 — 아래 경합 테스트가 공허해진다"

        team = await conn.fetchval("SELECT id FROM auth.teams LIMIT 1")
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'p@example.com','P','DEVELOPER','p-sub') ON CONFLICT DO NOTHING",
            USER_ID,
            team,
        )
    finally:
        await conn.close()

    yield dsn
    await _drop_db()


USER_ID = uuid.UUID("bbbb0000-0000-4000-a000-000000000001")


async def _post(dsn: str, key: str | None, lines: int = 10):
    """라우터 핸들러를 실 세션으로 직접 호출한다(HTTP 계층 없이)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers.productivity import ProductivityEventRequest, record_productivity_event

    engine = create_async_engine(dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    body = ProductivityEventRequest(
        user_id=str(USER_ID),
        event_type="CODE_GENERATED",
        lines_generated=lines,
        idempotency_key=key,
    )
    try:
        async with maker() as session:
            return await record_productivity_event(None, body, session)
    finally:
        await engine.dispose()


async def _count(dsn: str, key: str | None = None) -> int:
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        if key is None:
            return await conn.fetchval("SELECT count(*) FROM usage.productivity_events")
        return await conn.fetchval(
            "SELECT count(*) FROM usage.productivity_events WHERE idempotency_key = $1", key
        )
    finally:
        await conn.close()


@pg
async def test_retransmission_does_not_double_count(db_dsn):
    """같은 키로 두 번 = 1건만 적재. 이게 이 기능의 존재 이유다."""
    key = f"evt-{uuid.uuid4()}"
    first = await _post(db_dsn, key)
    second = await _post(db_dsn, key)

    assert await _count(db_dsn, key) == 1, "재전송이 두 번 적재됐다 — ROI 지표가 부풀려진다"
    assert second.get("deduplicated") is True, "중복임을 알리지 않았다"
    assert second["event_id"] == first["event_id"], (
        "중복 응답이 원본 이벤트 id 를 돌려주지 않는다 — 호출자가 상관관계를 잃는다"
    )


@pg
async def test_events_without_a_key_are_not_deduplicated(db_dsn):
    """키 없는 이벤트는 서로 다른 사건이다 — 부분 인덱스가 NULL 을 막지 않아야 한다.

    이게 전체 유니크 제약이었다면 두 번째가 실패한다.
    """
    before = await _count(db_dsn)
    await _post(db_dsn, None)
    await _post(db_dsn, None)
    assert await _count(db_dsn) == before + 2, "키 없는 이벤트가 중복 제거됐다"


@pg
async def test_different_keys_are_separate_events(db_dsn):
    """과잉 차단 대조군."""
    before = await _count(db_dsn)
    await _post(db_dsn, f"a-{uuid.uuid4()}")
    await _post(db_dsn, f"b-{uuid.uuid4()}")
    assert await _count(db_dsn) == before + 2


@pg
async def test_concurrent_retransmission_yields_one_row_and_no_error(db_dsn):
    """**핵심**: 동시 재전송에서도 1건이고, 어느 쪽도 예외를 보지 않는다.

    선-조회만 있는 구현은 여기서 깨진다 — 두 트랜잭션이 각자 스냅샷에서 "없다" 를
    보고 둘 다 INSERT 하면 부분 유니크 인덱스가 한쪽을 거부하고 그 요청은 500 이 된다.
    웹훅 발신자는 500 을 보고 **또** 재전송한다.
    """
    key = f"race-{uuid.uuid4()}"
    results = await asyncio.gather(
        _post(db_dsn, key), _post(db_dsn, key), return_exceptions=True
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"동시 재전송에서 예외가 났다: {errors!r}"
    assert await _count(db_dsn, key) == 1, (
        f"동시 재전송이 {await _count(db_dsn, key)}건 적재됐다 — 중복 제거가 경합을 못 닫는다"
    )
    # 양쪽 모두 성공 응답이어야 한다(한쪽만 200 이면 나머지는 재전송을 유발한다).
    assert all(r.get("status") == "ok" for r in results), f"성공 응답이 아니다: {results}"


@pg
async def test_three_way_concurrent_retransmission(db_dsn):
    """2개로 통과하고 3개에서 깨지는 구현이 있다(재시도 상한이 1인 경우)."""
    key = f"race3-{uuid.uuid4()}"
    results = await asyncio.gather(
        *[_post(db_dsn, key) for _ in range(3)], return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"3중 동시 재전송에서 예외: {errors!r}"
    assert await _count(db_dsn, key) == 1
