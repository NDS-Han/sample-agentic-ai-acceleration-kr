# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3 — 실 PG: 워커 flush → 롤업 → 비용 대조 0 불일치, 변조는 잡힌다, 재가격이 원장을 같이 고친다.

PROOF_DSN(0062 head 스키마, postgresql+asyncpg://…) 이 있을 때만 돈다. 템플릿 복제 DB 를 쓰고 지운다.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_w3recon"
ORG = "b4444444-4444-4444-4444-444444444444"
DEPT = "b3333333-3333-3333-3333-333333333333"
TEAM = "b2222222-2222-2222-2222-22222222222a"
U1 = "b1111111-1111-1111-1111-111111111111"
MODEL = "w3-model"
NOPRICE = "w3-noprice"
DEFAULT_DEPT = "00000000-0000-4000-a000-000000000002"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG(0062 head) 필요")


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
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'w3-org') "
                           "ON CONFLICT DO NOTHING", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'w3-d') "
                           "ON CONFLICT DO NOTHING", DEPT, ORG)
        await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'w3-t') "
                           "ON CONFLICT DO NOTHING", TEAM, DEPT)
        await conn.execute(
            "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
            "VALUES ($1,$2,'w3@example.invalid','w3','DEVELOPER','w3') ON CONFLICT DO NOTHING",
            U1, TEAM)
        for alias in (MODEL, NOPRICE):
            await conn.execute(
                "INSERT INTO model.model_aliases (alias, provider_model_id, provider, api_format,"
                " created_by) VALUES ($1, $1, 'BEDROCK', 'BEDROCK_NATIVE', $2) "
                "ON CONFLICT DO NOTHING", alias, U1)
        await conn.execute(
            "INSERT INTO model.model_pricings (model_alias,input_price_per_1k_tokens,"
            "output_price_per_1k_tokens,cache_read_price_per_1k_tokens,effective_from,created_by)"
            " VALUES ($1, 0.003, 0.015, 0.0003, now() - interval '400 days', $2)", MODEL, U1)
    finally:
        await conn.close()
    yield u
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    await admin.dispose()


def _settings(**kw):
    base = dict(reporting_timezone="Asia/Kolkata", daily_usage_agg_command_timeout=60.0,
                rollup_dirty_margin_sec=120, reconciliation_dirty_alert_hours=48,
                reconciliation_estimated_warn_pct=0.5, reprice_batch_size=500,
                cost_stream_key="cost:stream", cost_stream_group="g")
    base.update(kw)
    return MagicMock(**base)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    from worker import batch_flusher as bf
    from worker import daily_aggregator as da
    from worker import reconciliation as rc
    from worker import repricer as rp

    for m in (bf, da, rc, rp):
        monkeypatch.setattr(m, "get_settings", lambda: _settings())


@pytest.fixture
async def sf(url):
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    conn = await asyncpg.connect(_raw(url))
    await conn.execute(
        "TRUNCATE usage.usage_logs, usage.daily_aggregates, usage.daily_aggregate_days, "
        "usage.monthly_aggregates, usage.monthly_aggregate_months, usage.hourly_aggregates, "
        "usage.hourly_aggregate_hours, usage.rollup_dirty_marks, usage.cost_dead_letters, "
        "usage.reconciliation_runs, budget.budget_usages CASCADE")
    await conn.close()
    engine = create_async_engine(url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _entry(rid, day: date, *, model=MODEL, i=1000, o=500, cr=0, cost="0.010500", ws=0,
           ws_cost=None, status="SUCCESS", pm=False, est=False, client="claude-code"):
    from worker.schemas.cost_stream import CostStreamEntry

    at = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
    return CostStreamEntry(
        request_id=rid, user_id=U1, team_id=TEAM, dept_id=DEFAULT_DEPT, model_alias=model,
        provider="BEDROCK", input_tokens=i, output_tokens=o, cache_read_tokens=cr,
        cost_usd=Decimal(cost), web_search_count=ws,
        web_search_cost_usd=Decimal(ws_cost) if ws_cost is not None else None,
        latency_ms=10, is_streaming=True, estimated_usage=est, status=status,
        pricing_missing=pm, requested_at=at.isoformat(), completed_at=at.isoformat(),
        period=f"{day.year}-{day.month:02d}", date=day.isoformat(), client=client)


def _redis():
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    r = MagicMock()
    r.pipeline = MagicMock(return_value=pipe)
    r.publish = AsyncMock()
    return r


async def _flush_and_roll(sf, day, entries):
    from worker.batch_flusher import BatchFlusher
    from worker.daily_aggregator import aggregate_day, rebuild_month

    await BatchFlusher(session_factory=sf, redis=_redis()).flush(entries)
    async with sf() as s:
        d = day.replace(day=1)
        while d <= day:  # 월 누적은 [1일, day] 가 전부 집계돼야 만들어진다
            await aggregate_day(s, "Asia/Kolkata", d)
            d += timedelta(days=1)
        await rebuild_month(s, "Asia/Kolkata", day.replace(day=1), day + timedelta(days=1))


def _day() -> date:
    # 이번 달 안의 지난 날짜(월 경계 테스트가 아니다) — 1일이면 오늘.
    today = datetime.now(UTC).date()
    return today - timedelta(days=1) if today.day > 1 else today


async def test_end_to_end_zero_mismatches_then_tamper_detected(sf, url):
    import asyncpg

    from worker.reconciliation import reconcile

    day = _day()
    entries = [
        _entry("ok-1", day),
        _entry("cancel-est", day, o=37, cost="0.003555", status="CANCELLED", est=True),
        _entry("err-billed", day, o=12, cost="0.003180", status="ERROR"),
        _entry("ws", day, ws=3, ws_cost="0.021000"),
        _entry("ws-legacy", day, ws=2),  # 게이트웨이 비용 없음 → 워커 정가
        _entry("cr", day, i=100, cr=2000, o=10, cost="0.001050"),
        _entry("nop", day, model=NOPRICE, cost="0", pm=True),
    ]
    await _flush_and_roll(sf, day, entries)
    res = await reconcile(sf, None, report_date=day)
    assert res["status"] == "ok", res
    c = res["checks"]
    assert c["raw_vs_daily"]["state"] == "clean" and c["raw_vs_daily"]["raw_rows"] == 7
    assert c["monthly_vs_daily"]["state"] == "present"
    assert c["pricing"]["pricing_missing_open"] == 1
    assert c["pricing"]["recompute_checked"] >= 4
    # dept 는 팀에서 해석됐다(B5).
    conn = await asyncpg.connect(_raw(url))
    try:
        assert await conn.fetchval("SELECT count(DISTINCT dept_id) FROM usage.usage_logs") == 1
        assert str(await conn.fetchval("SELECT DISTINCT dept_id FROM usage.usage_logs")) == DEPT
        # 청구액 = 모델 + 웹 검색: 0.0105×3 + 0.003555 + 0.003180 + 0.001050 + 0.021 + 0.014
        used = await conn.fetchval(
            "SELECT used_usd FROM budget.budget_usages WHERE scope='USER' AND client IS NULL")
        assert used == Decimal("0.074285")
        assert await conn.fetchval(
            "SELECT count(*) FROM usage.reconciliation_runs WHERE status='ok'") == 1
        # 변조 — 원장 1 μ$ 차이도 잡힌다(허용 오차 0), 조용한 $0 행도.
        await conn.execute("UPDATE budget.budget_usages SET used_usd = used_usd + 0.000001 "
                           "WHERE scope='TEAM'")
        await conn.execute("UPDATE usage.usage_logs SET cost_usd = 0 WHERE request_id='ok-1'")
    finally:
        await conn.close()
    res2 = await reconcile(sf, None, report_date=day)
    c2 = res2["checks"]
    assert res2["status"] == "mismatch"
    assert c2["budget_vs_raw"]["mismatches"] >= 1
    assert c2["pricing"]["zero_cost_with_tokens"] == 1
    assert c2["raw_vs_daily"]["mismatches"] >= 1  # 롤업은 변조 전 값


async def test_repricer_fills_cost_and_keeps_ledgers_equal(sf, url):
    import asyncpg

    from worker.reconciliation import reconcile
    from worker.repricer import reprice_once

    day = _day()
    await _flush_and_roll(sf, day, [_entry("a", day), _entry("nop", day, model=NOPRICE,
                                                            cost="0", pm=True)])
    assert await reprice_once(sf, None) == 0  # 단가 없음 — 그대로
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute(
            "INSERT INTO model.model_pricings (model_alias,input_price_per_1k_tokens,"
            "output_price_per_1k_tokens,effective_from,created_by)"
            " VALUES ($1, 0.001, 0.002, now(), $2)", NOPRICE, U1)  # 요청 뒤에 생긴 단가
    finally:
        await conn.close()
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=b"1")
    assert await reprice_once(sf, redis) == 1
    conn = await asyncpg.connect(_raw(url))
    try:
        row = await conn.fetchrow("SELECT cost_usd, pricing_missing FROM usage.usage_logs "
                                  "WHERE request_id='nop'")
        assert row["cost_usd"] == Decimal("0.002000") and row["pricing_missing"] is False
        assert await conn.fetchval(
            "SELECT used_usd FROM budget.budget_usages WHERE scope='USER' AND client IS NULL"
        ) == Decimal("0.012500")
    finally:
        await conn.close()
    assert redis.eval.await_count == 3  # user / team / app 카운터
    res = await reconcile(sf, None, report_date=day)
    # 원장끼리는 같다. 롤업은 dirty 표시로 다음 시간 잡이 다시 집계한다(pending — 불일치 아님).
    assert res["checks"]["budget_vs_raw"]["mismatches"] == 0
    assert res["checks"]["raw_vs_daily"]["state"] == "dirty"
    assert res["status"] == "ok", res


async def test_redis_vs_db_check(sf):
    from worker.reconciliation import _r4

    day = _day()
    await _flush_and_roll(sf, day, [_entry("a", day)])
    period = f"{day.year}-{day.month:02d}"
    good = {f"budget:user:{{{U1}}}:{period}": b"0.012500",
            f"budget:resvsum:user:{{{U1}}}:{period}": b"0.002000",
            f"budget:team:{{{TEAM}}}:{period}": b"0.0105"}

    class _R:
        def __init__(self, vals):
            self.vals = vals

        async def xinfo_groups(self, _k):
            return [{"name": b"g", "lag": 0, "pending": 0}]

        async def mget(self, keys):
            return [self.vals.get(k) for k in keys]

    async with sf() as s:
        r = await _r4(s, _R(good), _settings(), period)
    assert (r["checked"], r["absent_in_redis"], r["mismatches"]) == (2, 1, 0)
    bad = dict(good)
    bad[f"budget:team:{{{TEAM}}}:{period}"] = b"0.0104"
    async with sf() as s:
        r = await _r4(s, _R(bad), _settings(), period)
    assert r["mismatches"] == 1
