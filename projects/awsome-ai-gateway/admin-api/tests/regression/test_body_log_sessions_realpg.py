# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Body Logs 세션 목록 — 시간 창 걸음(_explicit_sessions) == 창 전체 GROUP BY, 실 PostgreSQL.

PROOF_DSN 이 있을 때만 돈다. 자기 소유 DB(<dbname>_blsproof)를 만들고 init SQL 을 적용한다.

검증 축:
  * 커서 페이지를 끝까지 넘긴 합집합 == 예전 쿼리(_session_agg_stmt, LIMIT 없음)의 전체 결과 —
    키·started/ended·건수·오류·비용·모델 전부. 중복 없음, ended_at 비증가.
  * 필터(user·client·client=other(NULL)·has_error) × 창(7일·전체) × 페이지 크기 × 행 예산
    (기본 / 아주 작게 — 예산 소진 floor 커서 경로).
  * 같은 ended_at 타이(before_key), 커서 뒤에 행이 있는 세션(앞 페이지 세션), 창 경계를 걸친
    세션, 다른 사용자가 같은 session_id 를 쓰는 충돌.
"""
from __future__ import annotations

import json
import os
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_blsproof"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

ORG = uuid.UUID("c4444444-4444-4444-4444-444444444444")
DEPT = uuid.UUID("c3333333-3333-3333-3333-333333333333")
TEAM = uuid.UUID("c2222222-2222-2222-2222-222222222222")
USERS = [uuid.UUID(f"c1111111-1111-1111-1111-11111111111{i}") for i in range(5)]
NOW = datetime.now(UTC).replace(microsecond=0)


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _admin_exec(sql: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _, _ = PROOF_DSN.rpartition("/")
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(sql))
    finally:
        await admin.dispose()


def _rows():
    """~160 세션. 시작 시각은 지난 9일, 호출 간격 1~180분(30일 창 경계·커서를 걸친다)."""
    rnd = random.Random(42)
    out = []
    clients = ["claude-code", "cowork", None]
    for k in range(160):
        uid = USERS[k % len(USERS)]
        client = clients[(k // 5) % 3]
        # 충돌: 다른 사용자가 같은 sid 를 쓴다(k 와 k+1 이 같은 sid).
        sid = f"s{k - (k % 2) if k % 7 == 0 else k}"
        t = NOW - timedelta(minutes=rnd.uniform(5, 9 * 24 * 60))
        for _ in range(rnd.randint(1, 25)):
            status = "ERROR" if rnd.random() < 0.06 else "SUCCESS"
            out.append((uid, client, sid, t, status, Decimal(f"0.{rnd.randint(0, 999):03d}"),
                        f"m{rnd.randint(0, 3)}"))
            t = t + timedelta(minutes=rnd.uniform(1, 180), microseconds=rnd.randint(0, 999_999))
            if t > NOW + timedelta(minutes=5):
                break
    # session_id 가 없는 구 행 — 휴리스틱 세션이 명시 세션 사이에 끼어 커서를 끌어내린다.
    for k in range(60):
        t = NOW - timedelta(minutes=rnd.uniform(5, 9 * 24 * 60))
        out.append((USERS[k % 2], "claude-code" if k % 3 else None, None, t, "SUCCESS",
                    Decimal("0.01"), "m0"))
    # 타이: 서로 다른 세션 세 개가 정확히 같은 마지막 시각으로 끝난다.
    tie = NOW - timedelta(hours=30, microseconds=123_456)
    for j in range(3):
        out.append((USERS[j], "claude-code", f"tie{j}", tie - timedelta(hours=1), "SUCCESS",
                    Decimal("0.1"), "m0"))
        out.append((USERS[j], "claude-code", f"tie{j}", tie, "SUCCESS", Decimal("0.1"), "m1"))
    return out


async def _seed(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'bls-org')", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'d')",
                           DEPT, ORG)
        await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'t')",
                           TEAM, DEPT)
        for i, u in enumerate(USERS):
            await conn.execute(
                "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
                "VALUES ($1,$2,$3,$4,'DEVELOPER',$3)",
                u, TEAM, f"bls{i}@example.invalid", f"User {i}")
        await conn.executemany(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
            "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
            "completed_at,client,session_id) "
            "VALUES ($1,$2,$3,$4,$5,'BEDROCK',10,5,$6,10,$7::usage.usage_status,$8,$8,$9,$10)",
            [(str(uuid.uuid4()), r[0], TEAM, DEPT, r[6], r[5], r[4], r[3], r[1], r[2])
             for r in _rows()])
        await conn.execute("ANALYZE usage.usage_logs")
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def owned_dsn():
    import asyncpg

    prefix, _, name = PROOF_DSN.rpartition("/")
    owned = f"{name}{_OWNED_SUFFIX}"
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
    await _admin_exec(f'CREATE DATABASE "{owned}"')
    dsn = f"{prefix}/{owned}"
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001 — pgvector 없는 이미지 등
                print(f"  [init] {f.name}: {type(e).__name__}: {e}")
    finally:
        await conn.close()
    await _seed(dsn)
    yield dsn
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')


@pytest.fixture
async def session(owned_dsn):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(owned_dsn)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


def _request():
    req = MagicMock()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(return_value=True)
    return req


def _canon(s: dict) -> tuple:
    return (s["key"], s["started_at"], s["ended_at"], s["call_count"], s["error_count"],
            None if s["total_cost_usd"] is None else round(s["total_cost_usd"], 6),
            tuple(s["models"]))


async def _reference(session, *, days, user, client, has_error) -> list[tuple]:
    """예전 쿼리 — 창 전체 GROUP BY, LIMIT 없음."""
    from sqlalchemy import String, cast, select

    from app.models.auth import User
    from app.routers.body_logs import _session_agg_stmt

    cutoff = NOW - timedelta(days=days) if days else None
    user_ids = None
    if user:
        user_ids = list((await session.execute(
            select(User.id).where(User.email.ilike(f"%{user}%")
                                  | cast(User.id, String).ilike(f"%{user}%")))).scalars())
    rows = (await session.execute(_session_agg_stmt(
        cutoff, None, user_ids=user_ids, client=None if client == "other" else client,
        has_error=has_error, null_client=client == "other"))).all()
    return [
        (f"sid|{r.session_id}|{r.user_id or 'none'}|{r.client or 'other'}",
         r.started_at.isoformat(), r.ended_at.isoformat(), r.call_count, r.error_count,
         None if r.total_cost is None else round(float(r.total_cost), 6),
         tuple(sorted(m for m in (r.models or []) if m)))
        for r in rows
    ]


async def _paginate(session, *, days, user, client, has_error, limit) -> tuple[list[tuple], int]:
    from app.routers import body_logs

    got: list[dict] = []
    before = None
    before_keys: list[str] = []
    pages = 0
    while True:
        pages += 1
        assert pages < 500, "pagination does not terminate"
        resp = await body_logs.list_body_log_sessions(
            _request(), days=days, gap_minutes=30, limit=limit,
            before=datetime.fromisoformat(before) if before else None, before_key=before_keys,
            user=user, client=client, has_error=has_error, sid=None,
            admin=SimpleNamespace(), session=session,
        )
        await session.rollback()
        data = json.loads(resp.body)
        got.extend(data["sessions"])
        if not data["has_more"]:
            break
        assert data["next_before"], "has_more without a cursor"
        before = data["next_before"]
        before_keys = [s["key"] for s in got if s["ended_at"] == before]
    keys = [s["key"] for s in got]
    assert len(keys) == len(set(keys)), "a session appeared on two pages"
    ends = [datetime.fromisoformat(s["ended_at"]) for s in got]
    assert ends == sorted(ends, reverse=True), "pages are not in ended_at order"
    # 비교 대상은 명시 세션 — 휴리스틱(NULL session_id) 세션은 섞여 나오기만 한다.
    return [_canon(s) for s in got if s["session_id"] is not None], pages


CASES = [
    dict(days=7, user=None, client=None, has_error=False),
    dict(days=0, user=None, client=None, has_error=False),
    dict(days=7, user="bls1", client=None, has_error=False),
    dict(days=3, user=None, client="cowork", has_error=False),
    dict(days=7, user=None, client="other", has_error=False),
    dict(days=7, user=None, client=None, has_error=True),
    dict(days=0, user="bls2", client="claude-code", has_error=True),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(f"{k}={v}" for k, v in c.items()))
@pytest.mark.parametrize("limit", [1, 4, 20])
@pytest.mark.parametrize("budget", ["default", "tiny"])
async def test_paged_union_equals_window_group_by(session, monkeypatch, case, limit, budget):
    from app.routers import body_logs

    monkeypatch.setattr(body_logs, "datetime", _FrozenDatetime)
    if budget == "tiny":
        # 아주 작은 걸음·예산 — 거의 매 페이지가 floor 커서(예산 소진)로 끝난다.
        monkeypatch.setattr(body_logs, "_SESSION_SLICE_START", timedelta(minutes=7))
        monkeypatch.setattr(body_logs, "_SESSION_SLICE_ROW_BUDGET", 15)
        monkeypatch.setattr(body_logs, "_SESSION_AGG_BATCH", 3)
    ref = await _reference(session, **case)
    assert ref, "fixture produced no sessions for this case"
    got, pages = await _paginate(session, limit=limit, **case)
    assert sorted(got) == sorted(ref)
    if budget == "tiny" and limit == 20:
        # 예산 소진 floor 커서가 실제로 쓰였다 — 페이지 수가 limit 만으로 정해지는 수보다 많다.
        assert pages > -(-len(ref) // limit)


async def test_fixture_has_heuristic_sessions(session, monkeypatch):
    from app.routers import body_logs

    monkeypatch.setattr(body_logs, "datetime", _FrozenDatetime)
    resp = await body_logs.list_body_log_sessions(
        _request(), days=7, gap_minutes=30, limit=50, before=None, before_key=[], user=None,
        client=None, has_error=False, sid=None, admin=SimpleNamespace(), session=session)
    assert any(s["session_id"] is None for s in json.loads(resp.body)["sessions"])


async def test_first_page_matches_old_limit_query(session, monkeypatch):
    """첫 페이지(limit+1) — ended_at 열이 예전 ORDER BY max DESC LIMIT 와 같다."""
    from app.routers import body_logs

    monkeypatch.setattr(body_logs, "datetime", _FrozenDatetime)
    ref = await _reference(session, days=7, user=None, client=None, has_error=False)
    ref_ends = sorted((r[2] for r in ref), reverse=True)[:21]
    rows, floor = await body_logs._explicit_sessions(
        session, cutoff=NOW - timedelta(days=7), before=None, exclude_keys=set(), need=21,
        user_ids=None, client=None, null_client=False, has_error=False)
    assert floor is None
    assert [r.ended_at.isoformat() for r in rows] == ref_ends


async def test_cursor_excludes_sessions_with_rows_after_before(session, monkeypatch):
    """커서 뒤에 행이 있는 세션(앞 페이지에 나온 것)은 커서 아래 구간에서 다시 나오지 않는다."""
    from app.routers import body_logs

    monkeypatch.setattr(body_logs, "datetime", _FrozenDatetime)
    ref = await _reference(session, days=0, user=None, client=None, has_error=False)
    cursor = datetime.fromisoformat(sorted(r[2] for r in ref)[len(ref) // 2])
    expect = sorted((r for r in ref if datetime.fromisoformat(r[2]) <= cursor),
                    key=lambda r: r[2], reverse=True)[:5]
    rows, _ = await body_logs._explicit_sessions(
        session, cutoff=None, before=cursor, exclude_keys=set(), need=5,
        user_ids=None, client=None, null_client=False, has_error=False)
    assert [r.ended_at.isoformat() for r in rows] == [r[2] for r in expect]


class _FrozenDatetime(datetime):
    """라우터의 now() 를 시드 시각에 고정한다(창 경계가 테스트 중에 움직이지 않게)."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return NOW if tz is None else NOW.astimezone(tz)
