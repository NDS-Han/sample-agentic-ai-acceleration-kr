# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ws-history-fix — 실 PG: 역사적 웹 검색 비용 소급(option B).

검증: dry-run 계획(2 rows/3 searches/$0.021 형태), apply 멱등, scoped daily/monthly/hourly
롤업 갱신, budget 테이블 무변경, retro_not_charged 정보 분류, post-cutover 구멍 탐지.

PROOF_DSN(head 스키마, postgresql+asyncpg://…) 이 있을 때만 돈다. 템플릿 복제 DB 를 쓰고 지운다.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_wsh"
TZ = "Asia/Kolkata"
ORG = "d4444444-4444-4444-4444-444444444444"
DEPT = "d3333333-3333-3333-3333-333333333333"
TEAM = "d2222222-2222-2222-2222-22222222222a"
USER = "d1111111-1111-1111-1111-111111111111"
MODEL = "wsh-model"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG(head) 필요")


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
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'wsh-org')", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'wsh-d')",
                           DEPT, ORG)
        await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'wsh-t')",
                           TEAM, DEPT)
        await conn.execute(
            "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
            "VALUES ($1,$2,'wsh@example.invalid','wsh','DEVELOPER','wsh@example.invalid')",
            USER, TEAM)
    finally:
        await conn.close()
    yield u
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    await admin.dispose()


def _settings(**kw):
    base = dict(reporting_timezone=TZ, daily_usage_agg_command_timeout=60.0,
                rollup_dirty_margin_sec=120, reconciliation_dirty_alert_hours=48,
                reconciliation_estimated_warn_pct=0.5, reprice_batch_size=500,
                cost_stream_key="cost:stream", cost_stream_group="g",
                reconciliation_redis_epsilon_usd=0.0001, reconciliation_hourly_stale_sec=7200,
                daily_usage_agg_hourly_bridge_days=2, hourly_rollup_enabled=True)
    base.update(kw)
    return MagicMock(**base)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    from worker import backfill_ws_history as wh
    from worker import daily_aggregator as da
    from worker import reconciliation as rc

    for m in (wh, da, rc):
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
        "usage.reconciliation_runs, budget.budget_usages, audit.audit_logs CASCADE")
    await conn.execute("DELETE FROM public.system_settings "
                       "WHERE key IN ('web_search_cost_tracking_since')")
    await conn.close()
    engine = create_async_engine(url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _utc(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def _days():
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(TZ)).date()
    if today.day < 3:
        pytest.skip("월 초 이틀은 월 누적 검증 날짜를 만들 수 없다")
    return now, today, today - timedelta(days=1)


async def _insert_ws_row(conn, rid: str, at: datetime, count: int, cost,
                         *, model: str = MODEL, user: str = USER):
    await conn.execute(
        "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
        "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
        "completed_at,client,web_search_count,web_search_cost_usd) VALUES "
        "($1,$2,$3,$4,$5,'BEDROCK',0,0,0,10,'SUCCESS',$6,$6,'claude-code',$7,$8)",
        rid, user, TEAM, DEPT, model, at, count, cost)


async def _fetch(conn, sql: str, *args):
    return await conn.fetch(sql, *args)


async def test_dry_apply_idempotent_rollups_budget_untouched_and_retro_classification(sf, url):
    import asyncpg

    from worker import reconciliation as rc
    from worker.backfill_ws_history import build_plan, run

    _, today, day = _days()
    period = f"{day.year}-{day.month:02d}"
    cutoff = _utc(day, 12)
    assert cutoff < datetime.now(UTC)
    conn = await asyncpg.connect(_raw(url))
    try:
        await _insert_ws_row(conn, "h1", _utc(day, 9), 1, None)
        await _insert_ws_row(conn, "h2", _utc(day, 9, 5), 2, None)
        # 같은 날짜지만 보정 대상이 아닌 다른 입도 — scoped 갱신은 이 롤업 행을 건드리지 않는다.
        await _insert_ws_row(conn, "other-grain", _utc(day, 13), 4, None,
                             model="wsh-other-model")
    finally:
        await conn.close()

    # 보정 전에는 미추적 상태로 일/월 롤업이 이미 만들어져 있다고 가정한다.
    from worker.daily_aggregator import aggregate_day, rebuild_month

    async with sf() as s:
        d = day.replace(day=1)
        while d <= day:
            await aggregate_day(s, TZ, d)
            d += timedelta(days=1)
        await rebuild_month(s, TZ, day.replace(day=1), today)
    conn = await asyncpg.connect(_raw(url))
    try:
        before = await conn.fetchrow(
            "SELECT sum(web_search_count) q, sum(web_search_cost_usd) c,"
            " sum(web_search_untracked) u FROM usage.daily_aggregates"
            " WHERE date=$1 AND model_alias=$2", day, MODEL)
        assert (before["q"], before["c"], before["u"]) == (3, None, 3)
        # untouched 입도를 일부러 어긋나게 둔다 — scoped 갱신은 이 값을 고치지 않아야 한다.
        await conn.execute("UPDATE usage.daily_aggregates SET web_search_cost_usd=99.999999,"
                           " web_search_untracked=99 WHERE date=$1 AND model_alias=$2",
                           day, "wsh-other-model")
        await conn.execute("UPDATE usage.monthly_aggregates SET web_search_cost_usd=99.999999,"
                           " web_search_untracked=99 WHERE period=$1 AND model_alias=$2",
                           day.replace(day=1), "wsh-other-model")
        # 같은 scope 에 원장 행을 심어 둔다 — apply 가 정말 아무것도 더하지 않는지(0 유지) 본다.
        await conn.execute("INSERT INTO budget.budget_usages (scope,scope_id,period,client,used_usd,"
                           "limit_usd) VALUES ('USER',$1,$2,NULL,0,10)", USER, period)
        budget0 = await conn.fetch("SELECT scope::text, scope_id, client, used_usd "
                                   "FROM budget.budget_usages")
    finally:
        await conn.close()

    plan = await build_plan(sf, tz_name=TZ, cutoff=cutoff, periods=[period])
    assert plan["rows"] == 2 and plan["searches"] == 3
    assert Decimal(plan["cost_usd"]) == Decimal("0.021000")
    assert [(x["date"], x["searches"]) for x in plan["per_day"]] == [(day.isoformat(), 3)]
    scopes = {(x["scope"], x["scope_id"], x["client"]) for x in plan["per_scope"]}
    assert ("USER", USER, None) in scopes and ("TEAM", TEAM, None) in scopes
    assert ("USER", USER, "claude-code") in scopes

    # CLI dry-run 도 아무것도 쓰지 않는다.
    assert await run(["--cutoff", cutoff.isoformat(), "--period", period],
                     session_factory=sf) == 0
    conn = await asyncpg.connect(_raw(url))
    try:
        assert await conn.fetchval("SELECT count(*) FROM usage.usage_logs "
                                   "WHERE web_search_cost_usd IS NOT NULL"
                                   " AND request_id IN ('h1','h2')") == 0
        assert await conn.fetchval("SELECT count(*) FROM audit.audit_logs") == 0
    finally:
        await conn.close()

    assert await run(["--cutoff", cutoff.isoformat(), "--period", period, "--apply"],
                     session_factory=sf) == 0
    conn = await asyncpg.connect(_raw(url))
    try:
        costs = {r["request_id"]: r["web_search_cost_usd"] for r in await _fetch(
            conn, "SELECT request_id, web_search_cost_usd FROM usage.usage_logs"
                  " WHERE request_id IN ('h1','h2')")}
        assert costs == {"h1": Decimal("0.007000"), "h2": Decimal("0.014000")}
        daily = await conn.fetchrow(
            "SELECT sum(web_search_count) q, sum(web_search_cost_usd) c,"
            " sum(web_search_untracked) u FROM usage.daily_aggregates"
            " WHERE date=$1 AND model_alias=$2", day, MODEL)
        assert (daily["q"], daily["c"], daily["u"]) == (3, Decimal("0.021000"), 0)
        monthly = await conn.fetchrow(
            "SELECT sum(web_search_count) q, sum(web_search_cost_usd) c,"
            " sum(web_search_untracked) u FROM usage.monthly_aggregates"
            " WHERE period=$1 AND model_alias=$2", day.replace(day=1), MODEL)
        assert (monthly["q"], monthly["c"], monthly["u"]) == (3, Decimal("0.021000"), 0)
        untouched = await conn.fetchrow(
            "SELECT web_search_cost_usd c, web_search_untracked u FROM usage.daily_aggregates"
            " WHERE date=$1 AND model_alias=$2", day, "wsh-other-model")
        assert (untouched["c"], untouched["u"]) == (Decimal("99.999999"), 99)
        assert await conn.fetch("SELECT scope::text, scope_id, client, used_usd "
                                "FROM budget.budget_usages") == budget0
        audits = await conn.fetch(
            "SELECT resource_type, resource_id, changes FROM audit.audit_logs "
            "WHERE action='BACKFILL_WS_HISTORY'")
    finally:
        await conn.close()
    parsed = [
        (a["resource_type"], a["resource_id"],
         json.loads(a["changes"]) if isinstance(a["changes"], str) else a["changes"])
        for a in audits
    ]
    row_audits = [a for a in parsed if a[0] == "UsageLog"]
    assert len(row_audits) == 2
    assert all(a[1].startswith("ws_history:h") for a in row_audits)
    assert all(a[2]["retro_not_charged"] is True for a in row_audits)
    assert {a[2]["delta"] for a in row_audits} == {"0.007000", "0.014000"}
    summaries = [a for a in parsed if a[0] == "WsHistoryBackfill"]
    assert len(summaries) == 1
    assert summaries[0][2]["budget_writes"] == 0
    assert summaries[0][2]["redis_writes"] == 0
    assert summaries[0][2]["rollup"]["daily_keys_updated"] == 1
    assert summaries[0][2]["rollup"]["monthly_keys_updated"] == 1

    # budget_vs_raw: usage 가 올라도 소급분은 retro_not_charged 로 빠져 mismatch 는 0.
    res = await rc.reconcile(sf, None, report_date=day)
    r3 = res["checks"]["budget_vs_raw"]
    assert r3["mismatches"] == 0, r3
    retro = r3["retro_not_charged"]
    assert retro["rows"] == 2 and retro["queries"] == 3
    assert Decimal(retro["amount_usd"]) == Decimal("0.021000")
    assert res["checks"]["retro_not_charged"]["mismatches"] == 0
    assert res["checks"]["web_search_cost_gap"]["mismatches"] == 0

    # retro 는 현재 usage 값이 아니라 감사 delta 에 고정된다 — 소급 후 수동 변경은 mismatch.
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute("UPDATE usage.usage_logs SET web_search_cost_usd=0.008000"
                           " WHERE request_id='h1'")
    finally:
        await conn.close()
    changed = await rc.reconcile(sf, None, report_date=day)
    assert changed["checks"]["budget_vs_raw"]["mismatches"] > 0
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute("UPDATE usage.usage_logs SET web_search_cost_usd=0.007000"
                           " WHERE request_id='h1'")
    finally:
        await conn.close()

    # 멱등 — 두 번째 apply 는 후보 0 rows. 원장/감사된 행 비용은 그대로다.
    assert await run(["--cutoff", cutoff.isoformat(), "--period", period, "--apply"],
                     session_factory=sf) == 0
    plan2 = await build_plan(sf, tz_name=TZ, cutoff=cutoff, periods=[period])
    assert plan2["rows"] == 0 and plan2["searches"] == 0
    conn = await asyncpg.connect(_raw(url))
    try:
        assert {r["request_id"]: r["web_search_cost_usd"] for r in await _fetch(
            conn, "SELECT request_id, web_search_cost_usd FROM usage.usage_logs"
                  " WHERE request_id IN ('h1','h2')")} == costs
    finally:
        await conn.close()


async def test_rollup_error_rolls_back_usage_stamp_and_row_audit(sf, url, monkeypatch):
    """배치 트랜잭션 원자성 — 롤업 갱신이 죽으면 usage 스탬프/행 감사도 남지 않는다."""
    import asyncpg

    from worker import backfill_ws_history as wh

    _, _, day = _days()
    cutoff = _utc(day, 12)
    conn = await asyncpg.connect(_raw(url))
    try:
        await _insert_ws_row(conn, "atomic-h", _utc(day, 9), 1, None)
    finally:
        await conn.close()

    async def boom(*_a, **_kw):
        raise RuntimeError("forced rollup failure")

    monkeypatch.setattr(wh, "_refresh_ws_rollup_keys", boom)
    with pytest.raises(RuntimeError, match="forced rollup failure"):
        await wh.run(["--cutoff", cutoff.isoformat(), "--apply"], session_factory=sf)
    conn = await asyncpg.connect(_raw(url))
    try:
        assert await conn.fetchval("SELECT web_search_cost_usd FROM usage.usage_logs"
                                   " WHERE request_id='atomic-h'") is None
        assert await conn.fetchval("SELECT count(*) FROM audit.audit_logs"
                                   " WHERE action='BACKFILL_WS_HISTORY'") == 0
    finally:
        await conn.close()


async def test_retained_completed_hour_is_backfilled_and_tracked_cost_is_written(sf, url):
    import asyncpg

    from worker.backfill_ws_history import run
    from worker.daily_aggregator import aggregate_hour, hour_buckets

    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(TZ)).date()
    done = [(s, e) for s, e in hour_buckets(ZoneInfo(TZ), today) if e <= now]
    if not done:
        pytest.skip("오늘 완료된 시간 버킷이 아직 없다")
    hs, he = done[-1]
    at = hs + timedelta(minutes=1)
    cutoff = min(at + timedelta(minutes=2), now - timedelta(seconds=1))
    if cutoff <= at:
        pytest.skip("cutoff 를 현재 이전으로 만들 수 없다")
    conn = await asyncpg.connect(_raw(url))
    try:
        await _insert_ws_row(conn, "hourly-h", at, 1, None)
        # 이미 추적된 행은 백필 대상이 아니지만, 시간 롤업 작성기는 그 비용을 기록해야 한다.
        await _insert_ws_row(conn, "hourly-new", at, 1, Decimal("0.007000"))
    finally:
        await conn.close()
    async with sf() as s:
        await aggregate_hour(s, TZ, today, hs, he)
    conn = await asyncpg.connect(_raw(url))
    try:
        before = await conn.fetchrow("SELECT web_search_cost_usd c, web_search_untracked u,"
                                     " web_search_count q FROM usage.hourly_aggregates"
                                     " WHERE hour_start=$1", hs)
        assert (before["q"], before["c"], before["u"]) == (2, Decimal("0.007000"), 1)
    finally:
        await conn.close()
    assert await run(["--cutoff", cutoff.isoformat(), "--apply"], session_factory=sf) == 0
    conn = await asyncpg.connect(_raw(url))
    try:
        after = await conn.fetchrow("SELECT web_search_cost_usd c, web_search_untracked u,"
                                    " web_search_count q FROM usage.hourly_aggregates "
                                    "WHERE hour_start=$1", hs)
        assert (after["q"], after["c"], after["u"]) == (2, Decimal("0.014000"), 0)
    finally:
        await conn.close()


async def test_post_cutover_null_or_zero_cost_is_a_gap(sf):
    from worker.reconciliation import _ws_cost_gap

    _, _, day = _days()
    since = _utc(day, 12)
    async with sf() as s:
        await s.execute(
            text("INSERT INTO public.system_settings(key,value) VALUES "
                 "('web_search_cost_tracking_since', CAST(:v AS jsonb))"),
            {"v": f'{{"since": "{since.isoformat()}"}}'})
        await s.commit()
    async with sf() as s:
        for rid, cost in (("gap-null", None), ("gap-zero", 0)):
            await s.execute(
                text("INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
                     "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
                     "completed_at,client,web_search_count,web_search_cost_usd) VALUES "
                     "(:rid, :user, :team, :dept, :model, 'BEDROCK',0,0,0,10,"
                     "'SUCCESS', :at, :at, 'claude-code', 2, :cost)"),
                {"rid": rid, "user": USER, "team": TEAM, "dept": DEPT, "model": MODEL,
                 "at": _utc(day, 13), "cost": cost})
        await s.commit()
        out = await _ws_cost_gap(s, TZ, day)
        await s.commit()
    assert out["mismatches"] == 2
    assert out["rows"] == 2 and out["searches"] == 4
    assert out["sample"] == ["gap-null", "gap-zero"]
