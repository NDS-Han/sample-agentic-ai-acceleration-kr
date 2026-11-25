# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3b — 실 PG(+실 Redis): 요청 시작 원장, sweeper 추정 행, 늦은 실제 완료 교체, close, DB 폴백
소화.

PROOF_DSN(0063 head 스키마, postgresql+asyncpg://…) 이 있을 때만 돈다. 템플릿 복제 DB 를 쓰고
지운다.
REDIS_PROOF_URL 이 있으면 생존 신호·예약 정산을 실 Redis 로 확인한다(없으면 그 테스트만 건너뜀).

검사하는 불변식: 시작된 요청마다 usage_logs 행이 정확히 하나(실제 또는 추정), budget_usages = Σ(행
청구액).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_URL = os.environ.get("REDIS_PROOF_URL")
_OWNED = "_w3b"
ORG = "c4444444-4444-4444-4444-444444444444"
DEPT = "c3333333-3333-3333-3333-333333333333"
TEAM = "c2222222-2222-2222-2222-22222222222a"
U1 = "c1111111-1111-1111-1111-111111111111"
MODEL = "w3b-model"
DEFAULT_DEPT = "00000000-0000-4000-a000-000000000002"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG(0063 head) 필요")


def _raw(u: str) -> str:
    return u.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def url():
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pre, _, name = PROOF_DSN.rpartition("/")
    owned = name + _OWNED
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
        await c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                             "WHERE datname=:d AND pid<>pg_backend_pid()"), {"d": name})
        await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    await admin.dispose()
    u = f"{pre}/{owned}"
    conn = await asyncpg.connect(_raw(u))
    try:
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'w3b-org') "
                           "ON CONFLICT DO NOTHING", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'w3b-d') "
                           "ON CONFLICT DO NOTHING", DEPT, ORG)
        await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'w3b-t') "
                           "ON CONFLICT DO NOTHING", TEAM, DEPT)
        await conn.execute(
            "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
            "VALUES ($1,$2,'w3b@example.invalid','w3b','DEVELOPER','w3b') ON CONFLICT DO NOTHING",
            U1, TEAM)
    finally:
        await conn.close()
    yield u
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    await admin.dispose()


def _settings(**kw):
    base = dict(reporting_timezone="Asia/Kolkata", rollup_dirty_margin_sec=120,
                cost_stream_key="cost:stream:w3b", cost_stream_group="g-w3b",
                request_sweep_min_age_sec=0.0, request_sweep_max_age_sec=14400.0,
                request_sweep_max_lag=1000, request_sweep_batch_size=200,
                request_starts_retention_hours=48, pending_events_batch_size=500)
    base.update(kw)
    return MagicMock(**base)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    from worker import batch_flusher as bf
    from worker import pending_events as pe
    from worker import sweeper as sw

    for m in (bf, sw, pe):
        monkeypatch.setattr(m, "get_settings", lambda: _settings())


@pytest.fixture
async def sf(url):
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    conn = await asyncpg.connect(_raw(url))
    await conn.execute(
        "TRUNCATE usage.usage_logs, usage.rollup_dirty_marks, usage.cost_dead_letters, "
        "usage.request_starts, usage.cost_pending_events, budget.budget_usages CASCADE")
    await conn.close()
    engine = create_async_engine(url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def rds():
    """실 Redis(있으면) — 없으면 생존 신호 없음/정산 없음으로 동작하는 가짜."""
    if not REDIS_URL:
        r = MagicMock()
        pipe = MagicMock()
        pipe.execute = AsyncMock()
        r.pipeline = MagicMock(return_value=pipe)
        r.publish = AsyncMock(return_value=1)
        r.xinfo_groups = AsyncMock(return_value=[])
        r.exists = AsyncMock(return_value=0)
        r.eval = AsyncMock(return_value=b'{"settled": false}')
        yield r
        return
    from redis.asyncio import Redis

    r = Redis.from_url(REDIS_URL)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def _now():
    return datetime.now(UTC)


def _start(rid, *, est_in=1000, est_cost="0.003000", iid="gw-1:1:dead", at=None, resv=None,
           client="claude-code"):
    from worker.schemas.cost_stream import RequestStartEvent

    at = at or (_now() - timedelta(minutes=10))
    loc = at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    st = RequestStartEvent(
        request_id=rid, instance_id=iid, user_id=U1, team_id=TEAM, dept_id=DEFAULT_DEPT,
        model_alias=MODEL, provider="BEDROCK", client=client, is_streaming=True,
        estimated_input_tokens=est_in, estimated_cost_usd=Decimal(est_cost),
        requested_at=at.isoformat(), period=f"{loc.year}-{loc.month:02d}",
        date=loc.date().isoformat(), budget_reservation=resv or [])
    st._raw = st.model_dump_json()
    return st


def _end(rid, *, i=1000, o=500, cost="0.010500", at=None, status="SUCCESS", client="claude-code"):
    from worker.schemas.cost_stream import CostStreamEntry

    at = at or (_now() - timedelta(minutes=10))
    loc = at.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    return CostStreamEntry(
        request_id=rid, user_id=U1, team_id=TEAM, dept_id=DEFAULT_DEPT, model_alias=MODEL,
        provider="BEDROCK", input_tokens=i, output_tokens=o, cost_usd=Decimal(cost),
        web_search_cost_usd=Decimal("0"), latency_ms=10, is_streaming=True, status=status,
        requested_at=at.isoformat(), completed_at=_now().isoformat(),
        period=f"{loc.year}-{loc.month:02d}", date=loc.date().isoformat(), client=client)


def _close(rid):
    from worker.schemas.cost_stream import RequestCloseEvent

    return RequestCloseEvent(request_id=rid, instance_id="gw-1:1:dead")


def _flusher(sf, redis):
    from worker.batch_flusher import BatchFlusher

    return BatchFlusher(session_factory=sf, redis=redis)


async def _state(url):
    import asyncpg

    conn = await asyncpg.connect(_raw(url))
    try:
        rows = {r["request_id"]: dict(r) for r in await conn.fetch(
            "SELECT request_id, estimated_usage, input_tokens, output_tokens, cost_usd, "
            "status::text AS status FROM usage.usage_logs")}
        starts = {r["request_id"]: dict(r) for r in await conn.fetch(
            "SELECT request_id, closed_at, swept_at, swept_charge_usd FROM usage.request_starts")}
        budget = {(r["scope"], r["client"]): r["used_usd"] for r in await conn.fetch(
            "SELECT scope::text AS scope, client, used_usd FROM budget.budget_usages")}
        pending = await conn.fetchval("SELECT count(*) FROM usage.cost_pending_events")
    finally:
        await conn.close()
    return rows, starts, budget, pending


def _ledger_ok(rows, budget):
    """budget_usages(USER 총합) = Σ 행 청구액 — 행이 없으면 0/부재."""
    total = sum((r["cost_usd"] for r in rows.values()), Decimal("0"))
    assert budget.get(("USER", None), Decimal("0")) == total, (budget, total)
    assert budget.get(("TEAM", None), Decimal("0")) == total
    assert budget.get(("USER", "claude-code"), Decimal("0")) == total


async def _sweep(sf, redis, flusher, **kw):
    from worker.sweeper import sweep_once

    return await sweep_once(sf, redis, flusher, settings=_settings(**kw))


# ── 1. 정상 순서와 뒤바뀐 순서 ──────────────────────────────────────────────────────────

async def test_start_then_end_leaves_one_real_row_and_no_open_start(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("r1")])
    rows, starts, _, _ = await _state(url)
    assert "r1" in starts and not rows
    await f.flush([_end("r1")])
    rows, starts, budget, _ = await _state(url)
    assert list(rows) == ["r1"] and rows["r1"]["estimated_usage"] is False
    assert starts == {}
    _ledger_ok(rows, budget)
    assert await _sweep(sf, rds, f) == 0


async def test_end_before_start_does_not_reopen(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_end("r2")])
    await f.flush([_start("r2")])  # spool 에서 늦게 온 start
    rows, starts, budget, _ = await _state(url)
    assert list(rows) == ["r2"] and starts == {}
    assert await _sweep(sf, rds, f) == 0
    _ledger_ok(rows, budget)


async def test_start_and_end_in_same_batch_cancel_out(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("r3"), _end("r3")])
    rows, starts, _, _ = await _state(url)
    assert list(rows) == ["r3"] and starts == {}


# ── 2. sweeper: 추정 행 정확히 하나, 늦은 실제 완료는 교체(중복 없음) ──────────────────────

async def test_sweep_writes_one_estimated_row_then_late_real_replaces_it(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("r4", est_in=2000, est_cost="0.006000")])
    assert await _sweep(sf, rds, f) == 1
    assert await _sweep(sf, rds, f) == 0  # 두 번째 sweep 은 아무것도 안 한다
    rows, starts, budget, _ = await _state(url)
    assert list(rows) == ["r4"]
    r = rows["r4"]
    assert r["estimated_usage"] is True and r["status"] == "ERROR"
    assert (r["input_tokens"], r["output_tokens"], r["cost_usd"]) == (2000, 0, Decimal("0.006000"))
    assert starts["r4"]["swept_at"] is not None
    assert starts["r4"]["swept_charge_usd"] == Decimal("0.006000")
    _ledger_ok(rows, budget)
    # 늦은 실제 완료(예: 워커 lag 중에 스트림에 남아 있던 것) → 같은 행이 실제 값이 된다.
    await f.flush([_end("r4", i=2100, o=300, cost="0.010800")])
    rows, starts, budget, _ = await _state(url)
    assert list(rows) == ["r4"]
    assert rows["r4"]["estimated_usage"] is False and rows["r4"]["cost_usd"] == Decimal("0.010800")
    assert rows["r4"]["status"] == "SUCCESS" and rows["r4"]["output_tokens"] == 300
    assert starts == {}
    _ledger_ok(rows, budget)
    # 그 완료의 중복(spool 재발행) — 그대로.
    await f.flush([_end("r4", i=2100, o=300, cost="0.010800")])
    rows2, _, budget2, _ = await _state(url)
    assert rows2 == rows and budget2 == budget


async def test_live_instance_is_not_swept_until_heartbeat_lapses(sf, url, rds):
    if not REDIS_URL:
        pytest.skip("REDIS_PROOF_URL 미설정 — 생존 신호는 실 Redis 로 본다")
    f = _flusher(sf, rds)
    await rds.set("cost:live:gw-live:1:aaaa", "1", ex=30)
    await f.flush([_start("r5", iid="gw-live:1:aaaa")])
    assert await _sweep(sf, rds, f) == 0  # 살아 있는 프로세스의 긴 스트림
    await rds.delete("cost:live:gw-live:1:aaaa")  # 파드 SIGKILL → TTL 만료와 같다
    assert await _sweep(sf, rds, f) == 1
    # 최소 나이 전이면 신호가 없어도 쓸지 않는다.
    await f.flush([_start("r5b", iid="gw-gone:1:bbbb", at=_now())])
    assert await _sweep(sf, rds, f, request_sweep_min_age_sec=300.0) == 0


async def test_max_age_sweeps_even_with_live_heartbeat(sf, url, rds):
    if not REDIS_URL:
        pytest.skip("REDIS_PROOF_URL 미설정")
    f = _flusher(sf, rds)
    await rds.set("cost:live:gw-live:2:cccc", "1", ex=30)
    await f.flush([_start("r6", iid="gw-live:2:cccc", at=_now() - timedelta(hours=5))])
    assert await _sweep(sf, rds, f) == 1


async def test_sweeper_waits_while_stream_lags(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("r7")])
    lagging = MagicMock()
    lagging.xinfo_groups = AsyncMock(return_value=[{"name": b"g-w3b", "lag": 5000, "pending": 0}])
    assert await _sweep(sf, lagging, f) == 0
    assert await _sweep(sf, rds, f) == 1


async def test_pending_fallback_event_blocks_sweep(sf, url, rds):
    import asyncpg

    f = _flusher(sf, rds)
    await f.flush([_start("r8")])
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute("INSERT INTO usage.cost_pending_events (request_id, kind, payload) "
                           "VALUES ('r8', 'end', $1)", _end("r8").model_dump_json())
    finally:
        await conn.close()
    assert await _sweep(sf, rds, f) == 0  # 완료가 DB 폴백에 대기 중
    from worker.pending_events import drain_once

    assert await drain_once(sf, f) == 1
    rows, starts, budget, pending = await _state(url)
    assert list(rows) == ["r8"] and rows["r8"]["estimated_usage"] is False
    assert starts == {} and pending == 0
    _ledger_ok(rows, budget)


async def test_sweep_race_with_real_row_deletes_start_only(sf, url, rds):
    """완료가 커밋된 뒤 시작이 기록된 경합(다른 replica 의 동시 배치) — 행은 하나, 예산 불변."""
    import asyncpg

    f = _flusher(sf, rds)
    await f.flush([_end("r9")])
    st = _start("r9")
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute(
            "INSERT INTO usage.request_starts (request_id, instance_id, started_at, payload) "
            "VALUES ('r9', $1, $2, $3)", st.instance_id, datetime.fromisoformat(st.requested_at),
            st._raw)
    finally:
        await conn.close()
    _, _, before, _ = await _state(url)
    assert await _sweep(sf, rds, f) == 0
    rows, starts, budget, _ = await _state(url)
    assert list(rows) == ["r9"] and rows["r9"]["estimated_usage"] is False
    assert starts == {} and budget == before


# ── 3. close(행 없이 끝난 요청) ────────────────────────────────────────────────────────

async def test_close_prevents_sweep_and_late_duplicate_start(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("c1")])
    await f.flush([_close("c1")])
    await f.flush([_start("c1")])  # spool 중복 start
    assert await _sweep(sf, rds, f) == 0
    rows, starts, _, _ = await _state(url)
    assert rows == {} and starts["c1"]["closed_at"] is not None


async def test_close_before_start_leaves_tombstone(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_close("c2")])
    await f.flush([_start("c2")])
    assert await _sweep(sf, rds, f) == 0
    rows, _, _, _ = await _state(url)
    assert rows == {}


async def test_close_after_sweep_removes_estimated_row_and_charge(sf, url, rds):
    f = _flusher(sf, rds)
    await f.flush([_start("c3")])
    assert await _sweep(sf, rds, f) == 1
    rows, _, budget, _ = await _state(url)
    assert budget[("USER", None)] == Decimal("0.003000")
    await f.flush([_close("c3")])
    rows, starts, budget, _ = await _state(url)
    assert rows == {}
    assert budget[("USER", None)] == Decimal("0") and budget[("TEAM", None)] == Decimal("0")
    assert starts["c3"]["closed_at"] is not None


async def test_purge_removes_old_tombstones(sf, url, rds):
    import asyncpg

    from worker.sweeper import purge_once

    f = _flusher(sf, rds)
    await f.flush([_close("p1"), _close("p2")])
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute("UPDATE usage.request_starts SET closed_at = now() - interval '3 days' "
                           "WHERE request_id='p1'")
    finally:
        await conn.close()
    assert await purge_once(sf, settings=_settings()) == 1
    _, starts, _, _ = await _state(url)
    assert set(starts) == {"p2"}


# ── 4. DB 폴백 표 소화: 스트림과 같은 이벤트가 양쪽에 있어도 한 행 ──────────────────────────

async def test_pending_events_and_stream_duplicates_record_once(sf, url, rds):
    import asyncpg

    from worker.pending_events import drain_once

    f = _flusher(sf, rds)
    e = _end("d1")
    await f.flush([e])  # 스트림 경로(XADD 는 서버에서 성공, 응답만 잃음)
    conn = await asyncpg.connect(_raw(url))
    try:
        for p in (e.model_dump_json(), _start("d2")._raw, _end("d2").model_dump_json(),
                  _start("d3")._raw, json.dumps({"kind": "close", "request_id": "d4"}),
                  "{not json"):
            await conn.execute("INSERT INTO usage.cost_pending_events (request_id, kind, payload) "
                               "VALUES (NULL, 'end', $1)", p)
    finally:
        await conn.close()
    assert await drain_once(sf, f) == 6
    rows, starts, budget, pending = await _state(url)
    assert set(rows) == {"d1", "d2"} and pending == 0
    assert set(starts) == {"d3", "d4"} and starts["d4"]["closed_at"] is not None
    _ledger_ok(rows, budget)
    conn = await asyncpg.connect(_raw(url))
    try:
        assert await conn.fetchval("SELECT count(*) FROM usage.cost_dead_letters "
                                   "WHERE reason='parse_failed'") == 1
    finally:
        await conn.close()


# ── 5. 예약 정산(실 Redis + budget_settle.lua) ──────────────────────────────────────────

async def test_sweep_settles_orphan_budget_reservation(sf, url, rds):
    if not REDIS_URL:
        pytest.skip("REDIS_PROOF_URL 미설정")
    f = _flusher(sf, rds)
    period = _start("x")._raw and json.loads(_start("x")._raw)["period"]
    uk = f"budget:user:{{{U1}}}:{period}"
    mk = f"budget:pending:user:{{{U1}}}:s1"
    rk = f"budget:resvsum:user:{{{U1}}}:{period}"
    ik = f"budget:pendingidx:user:{{{U1}}}"
    # 게이트웨이 reserve 직후 상태: 사용 카운터에 최악 추정치 $2 가 선차감돼 있다.
    await rds.set(uk, "2")
    await rds.set(mk, "2", ex=14400)
    await rds.set(rk, "2")
    await rds.hset(ik, "s1", "2")
    resv = [{"scope": "user", "usage_key": uk, "config_key": f"budget:config:user:{{{U1}}}",
             "marker_key": mk, "resvsum_key": rk, "idx_key": ik, "idx_field": "s1",
             "fallback": "", "reserved": "2", "marker_ttl": "14400"}]
    await f.flush([_start("s1", est_cost="0.250000", resv=resv)])
    assert await _sweep(sf, rds, f) == 1
    assert Decimal((await rds.get(uk)).decode()) == Decimal("0.25")
    assert await rds.exists(mk) == 0
    assert Decimal((await rds.get(rk)).decode()) == Decimal("0")
    tomb = await rds.get(f"budget:settled:{mk[len('budget:pending:'):]}")
    assert tomb is not None and Decimal(tomb.decode()) == Decimal("0.25")
    # 이미 게이트웨이가 정산한 예약이면 sweeper 정산은 no-op(이중 적용 없음).
    from worker.schemas.cost_stream import RequestStartEvent
    from worker.sweeper import settle_reservations

    st = RequestStartEvent.model_validate_json(_start("s1", resv=resv)._raw)
    assert await settle_reservations(rds, st, Decimal("0.25")) == 0
    assert Decimal((await rds.get(uk)).decode()) == Decimal("0.25")
