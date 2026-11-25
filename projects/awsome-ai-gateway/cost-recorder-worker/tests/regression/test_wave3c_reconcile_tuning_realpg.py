# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3c — 실 PG: 신호 전용 대조(진행 중 날 = 시간 롤업, Redis 허용 오차) + 원장 보정.

dev 와 같은 모양의 데이터(wave 3 이전 워커가 쓴 행: 웹 검색 비용이 예산에 없고 원장은 NUMERIC(12,4)
반올림 잔차 + wave 3 워커가 쓴 행)를 만들고:

1. 보정 전 대조는 budget_vs_raw 불일치를 보고한다(dev 의 12건과 같은 종류).
2. dry-run 은 아무것도 쓰지 않는다.
3. 적용 뒤 대조는 **0 불일치**(오늘 = 시간 롤업 비교 포함), 다시 적용하면 0건(멱등).
4. 그 뒤 넣은 진짜 불일치(원장 1 µ$, Redis 카운터, 원장 없는 행)는 여전히 잡힌다.

PROOF_DSN(head 스키마, postgresql+asyncpg://…) 이 있을 때만 돈다. 템플릿 복제 DB 를 쓰고 지운다.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_w3c"
TZ = "Asia/Kolkata"
ORG = "c4444444-4444-4444-4444-444444444444"
DEPT = "c3333333-3333-3333-3333-333333333333"
TEAM = "c2222222-2222-2222-2222-22222222222a"
TEAM2 = "c2222222-2222-2222-2222-22222222222b"
U1 = "c1111111-1111-1111-1111-111111111111"  # dev 의 537f… — 웹 검색 + 반올림 잔차
U2 = "c1111111-1111-1111-1111-111111111112"  # dev 의 da54… — 4자리 반올림으로 0 이 된 원장
U3 = "c1111111-1111-1111-1111-111111111113"  # dev 의 25d6… — 반올림으로 원장이 더 크다
MODEL = "w3c-model"

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
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'w3c-org') "
                           "ON CONFLICT DO NOTHING", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'w3c-d') "
                           "ON CONFLICT DO NOTHING", DEPT, ORG)
        for t, n in ((TEAM, "w3c-t"), (TEAM2, "w3c-t2")):
            await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,$3) "
                               "ON CONFLICT DO NOTHING", t, DEPT, n)
        for uid, t in ((U1, TEAM), (U2, TEAM2), (U3, TEAM)):
            await conn.execute(
                "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
                "VALUES ($1,$2,$3,'w3c','DEVELOPER',$3) ON CONFLICT DO NOTHING",
                uid, t, f"{uid[-4:]}@example.invalid")
        await conn.execute(
            "INSERT INTO model.model_aliases (alias, provider_model_id, provider, api_format,"
            " created_by) VALUES ($1, $1, 'BEDROCK', 'BEDROCK_NATIVE', $2) "
            "ON CONFLICT DO NOTHING", MODEL, U1)
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
    from worker import backfill_budget_legacy as bb
    from worker import batch_flusher as bf
    from worker import daily_aggregator as da
    from worker import reconciliation as rc

    for m in (bb, bf, da, rc):
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
    await conn.close()
    engine = create_async_engine(url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeRedis:
    """대조 R4·보정이 쓰는 명령만 — 값은 bytes(실 Redis 와 같다)."""

    def __init__(self):
        self.kv: dict[str, bytes] = {}

    async def xinfo_groups(self, _k):
        return [{"name": b"g", "lag": 0, "pending": 0}]

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def set(self, k, v, ex=None):
        self.kv[k] = str(v).encode()

    async def eval(self, script, _n, key, arg):
        if key not in self.kv:
            return None if "INCRBYFLOAT" in script else 0
        if "INCRBYFLOAT" in script:
            v = float(self.kv[key]) + float(arg)
            self.kv[key] = repr(v).encode()
            return self.kv[key]
        self.kv[f"{key}:lost"] = b"1"
        return 1

    def add(self, key, amount: Decimal):
        cur = float(self.kv.get(key, b"0"))
        self.kv[key] = repr(cur + float(amount)).encode()


def _keys(uid, team, client, period):
    out = [f"budget:user:{{{uid}}}:{period}", f"budget:team:{{{team}}}:{period}"]
    if client in ("claude-code", "cowork", "codex"):
        out.append(f"budget:user:{{{uid}}}:{client}:{period}")
    return out


def _q4(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


async def _legacy_rows(conn, redis: FakeRedis, period: str, rows: list[dict]):
    """wave 3 이전 워커: usage_logs 에는 웹 검색 비용이 있고, 원장에는 모델 비용만 — flush 마다
    NUMERIC(12,4) 반올림. Redis 카운터는 모델 비용을 float 로 정확히 더했다."""
    ledger: dict[tuple, Decimal] = {}
    for r in rows:
        await conn.execute(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
            "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
            "completed_at,client,web_search_count,web_search_cost_usd) VALUES "
            "($1,$2,$3,$4,$5,'BEDROCK',$6,$7,$8,10,'SUCCESS',$9,$9,$10,$11,$12)",
            r["rid"], r["user"], r["team"], DEPT, MODEL, r["i"], r["o"], r["cost"], r["at"],
            r["client"], r["wsn"], r["ws"])
        scopes = [("USER", r["user"], None), ("TEAM", r["team"], None)]
        if r["client"] in ("claude-code", "cowork", "codex"):
            scopes.append(("USER", r["user"], r["client"]))
        for s in scopes:
            ledger[s] = _q4(ledger.get(s, Decimal(0)) + _q4(r["cost"]))
        for k in _keys(r["user"], r["team"], r["client"], period):
            redis.add(k, r["cost"])
    for (scope, sid, client), amt in ledger.items():
        await conn.execute(
            "INSERT INTO budget.budget_usages (scope,scope_id,period,client,used_usd,limit_usd)"
            " VALUES ($1,$2,$3,$4,$5,0)", scope, sid, period, client, amt)


def _entry(rid, at: datetime, user, team, *, i=100, o=10, cost="0.000450", ws=0,
           ws_cost=None, client="claude-code"):
    from worker.schemas.cost_stream import CostStreamEntry

    local = at.astimezone(ZoneInfo(TZ))
    return CostStreamEntry(
        request_id=rid, user_id=user, team_id=team, dept_id=DEPT, model_alias=MODEL,
        provider="BEDROCK", input_tokens=i, output_tokens=o, cost_usd=Decimal(cost),
        web_search_count=ws,
        web_search_cost_usd=Decimal(ws_cost) if ws_cost is not None else None,
        latency_ms=10, is_streaming=True, status="SUCCESS", requested_at=at.isoformat(),
        completed_at=at.isoformat(), period=f"{local.year}-{local.month:02d}",
        date=local.date().isoformat(), client=client)


def _redis_stub():
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    r = MagicMock()
    r.pipeline = MagicMock(return_value=pipe)
    r.publish = AsyncMock()
    return r


async def _wave3_flush(sf, redis: FakeRedis, period: str, entries):
    """wave 3 워커 — 원장과 Redis 에 모델 + 웹 검색 비용(게이트웨이가 카운터에 더한 값)."""
    from worker.batch_flusher import BatchFlusher

    await BatchFlusher(session_factory=sf, redis=_redis_stub()).flush(entries)
    for e in entries:
        for k in _keys(e.user_id, e.team_id, e.client, period):
            redis.add(k, e.cost_usd + (e.web_search_cost_usd or 0))


def _days():
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(TZ)).date()
    if today.day < 3:
        pytest.skip("월 초 이틀은 지난 날짜·오늘이 같은 달이 아니다")
    return now, today, today - timedelta(days=1)


def _utc(d: date, h: int, m: int = 0) -> datetime:
    """리포팅 tz 의 d 날짜 h:m 을 UTC 로."""
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


async def _build_dev_like(sf, url, redis: FakeRedis):
    import asyncpg

    from worker.daily_aggregator import aggregate_day, aggregate_hour, hour_buckets, rebuild_month

    now, today, day = _days()
    period = f"{day.year}-{day.month:02d}"
    cutoff = _utc(day, 12)
    legacy = [
        # U1: 웹 검색 1회($0.007) 가 예산에 없다 + 행마다 4자리 반올림
        dict(rid="l-u1-ws", user=U1, team=TEAM, client="claude-code", i=100, o=10,
             cost=Decimal("0.000450"), wsn=1, ws=Decimal("0.007000"), at=_utc(day, 9)),
        dict(rid="l-u1-a", user=U1, team=TEAM, client="claude-code", i=100, o=10,
             cost=Decimal("0.000450"), wsn=0, ws=Decimal("0"), at=_utc(day, 9, 5)),
        dict(rid="l-u1-b", user=U1, team=TEAM, client="other", i=100, o=10,
             cost=Decimal("0.000450"), wsn=0, ws=None, at=_utc(day, 9, 10)),
        # U3: 0058 이전(웹 검색 비용 미추적 NULL) — 예산에 넣을 것이 없다, 반올림만
        dict(rid="l-u3-ws-null", user=U3, team=TEAM, client="claude-code", i=100, o=10,
             cost=Decimal("0.000450"), wsn=2, ws=None, at=_utc(day, 9, 20)),
    ] + [
        # U2: $0.000003 행 10개 — 원장은 매번 0.0000 으로 반올림돼 0 으로 남았다
        dict(rid=f"l-u2-{k}", user=U2, team=TEAM2, client="other", i=1, o=0,
             cost=Decimal("0.000003"), wsn=0, ws=Decimal("0"), at=_utc(day, 10, k))
        for k in range(10)
    ]
    conn = await asyncpg.connect(_raw(url))
    try:
        await _legacy_rows(conn, redis, period, legacy)
    finally:
        await conn.close()
    # wave 3 워커가 쓴 행 — 어제 + 오늘(끝난 시간 하나와 진행 중인 시간 하나)
    post = [_entry("p-u1-ws", _utc(day, 13), U1, TEAM, ws=2, ws_cost="0.014000"),
            _entry("p-u1", _utc(day, 14), U1, TEAM, client="other"),
            _entry("p-u3", _utc(day, 15), U3, TEAM)]
    done_hours = [(s, e) for s, e in hour_buckets(ZoneInfo(TZ), today) if e <= now]
    if done_hours:
        post.append(_entry("p-today-done", done_hours[-1][0] + timedelta(minutes=1), U1, TEAM))
    post.append(_entry("p-today-open", now - timedelta(seconds=5), U1, TEAM))
    await _wave3_flush(sf, redis, period, post)
    async with sf() as s:
        d = day.replace(day=1)
        while d <= day:
            await aggregate_day(s, TZ, d)
            d += timedelta(days=1)
        await rebuild_month(s, TZ, day.replace(day=1), today)
        for hs, he in done_hours:
            await aggregate_hour(s, TZ, today, hs, he)
    return dict(now=now, today=today, day=day, period=period, cutoff=cutoff,
                has_done_hour=bool(done_hours))


async def _r4(sf, redis, settings, period):
    from worker.reconciliation import _r4 as r4

    async with sf() as s:
        out = await r4(s, redis, settings, period)
        await s.commit()
    return out


async def _audit_count(url) -> int:
    import asyncpg

    conn = await asyncpg.connect(_raw(url))
    try:
        return await conn.fetchval("SELECT count(*) FROM audit.audit_logs")
    finally:
        await conn.close()


async def _ledger(url) -> dict:
    import asyncpg

    conn = await asyncpg.connect(_raw(url))
    try:
        return {(r["scope"], str(r["scope_id"]), r["client"]): r["used_usd"] for r in
                await conn.fetch("SELECT scope::text, scope_id, client, used_usd "
                                 "FROM budget.budget_usages")}
    finally:
        await conn.close()


async def test_dev_like_dataset_zero_after_backfill_and_real_discrepancy_detected(sf, url):
    import asyncpg

    from worker import reconciliation as rc
    from worker.backfill_budget_legacy import run as backfill

    redis = FakeRedis()
    ctx = await _build_dev_like(sf, url, redis)
    day, today, period, cutoff = ctx["day"], ctx["today"], ctx["period"], ctx["cutoff"]
    cut = cutoff.isoformat()

    # 1. 보정 전 — dev 와 같은 종류의 불일치(웹 검색 누락 + 반올림 잔차).
    before = await rc.reconcile(sf, redis, report_date=day)
    assert before["status"] == "mismatch", before
    r3 = before["checks"]["budget_vs_raw"]
    assert r3["mismatches"] >= 5, r3  # U1 총합·앱·TEAM, U2 총합·TEAM2, U3 …
    assert before["checks"]["raw_vs_daily"]["state"] == "clean"
    assert before["checks"]["raw_vs_daily"]["mismatches"] == 0
    # Redis(정확한 float 합) vs 반올림된 원장 — 허용 오차 0 이면 dev 의 redis_vs_db 4건과 같은 종류.
    strict = await _r4(sf, redis, _settings(reconciliation_redis_epsilon_usd=0), period)
    assert strict["mismatches"] >= 2, strict

    # 2. dry-run — 계획만, 아무것도 쓰지 않는다.
    led0 = await _ledger(url)
    assert await backfill(["--cutoff", cut, "--period", period, "--no-redis"],
                          session_factory=sf) == 0
    assert await _ledger(url) == led0
    assert await _audit_count(url) == 0

    # 3. 적용 → 대조 0 불일치. 오늘(진행 중)은 시간 롤업과 비교한다.
    assert await backfill(["--cutoff", cut, "--period", period, "--apply"],
                          session_factory=sf, redis=redis) == 0
    led1 = await _ledger(url)
    # U1 총합: 웹 검색 $0.007 + 반올림 잔차(legacy 3행 0.0005×3 → 0.00045×3 = −$0.00015)
    assert led1[("USER", U1, None)] - led0[("USER", U1, None)] == Decimal("0.006850")
    after = await rc.reconcile(sf, redis, report_date=day)
    assert after["status"] == "ok", after
    assert after["mismatch_count"] == 0
    assert after["checks"]["redis_vs_db"]["checked"] >= 6
    assert after["checks"]["budget_vs_raw"]["mismatches"] == 0
    # 원장 = Σ(cost + web_search) 정확히(허용 오차 0 인 R3 이 통과했다) — U1 총합 검산
    conn = await asyncpg.connect(_raw(url))
    try:
        exp = await conn.fetchval(
            "SELECT sum(cost_usd + COALESCE(web_search_cost_usd,0)) FROM usage.usage_logs "
            "WHERE user_id=$1", U1)
        assert led1[("USER", U1, None)] == exp
        audits = await conn.fetch("SELECT resource_type, resource_id, changes FROM "
                                  "audit.audit_logs WHERE action='BACKFILL_BUDGET_LEGACY'")
    finally:
        await conn.close()
    kinds = sorted(a["resource_id"].split(":")[0] for a in audits
                   if a["resource_type"] == "BudgetUsage")
    assert kinds.count("web_search") == 3  # U1 총합·앱·TEAM
    assert "rounding" in kinds
    assert sum(1 for a in audits if a["resource_type"] == "BudgetBackfill") == 1
    # Redis 도 웹 검색 몫을 받았다 — 허용 오차 0 으로도 일치(반올림 잔차는 원장 쪽에서 맞췄다).
    strict2 = await _r4(sf, redis, _settings(reconciliation_redis_epsilon_usd=0), period)
    assert strict2["mismatches"] == 0, strict2

    today_res = await rc.reconcile(sf, redis, report_date=today)
    r1t = today_res["checks"]["raw_vs_daily"]
    assert r1t["state"] == "hourly" and r1t["reason"] == "in_progress", r1t
    assert r1t["mismatches"] == 0 and r1t["rows_in_open_hour"] >= 1
    if ctx["has_done_hour"]:
        assert r1t["hours_checked"] >= 1
    assert today_res["status"] == "ok", today_res

    # 4. 멱등 — 다시 적용해도 0 건, 원장 그대로.
    assert await backfill(["--cutoff", cut, "--period", period, "--apply"],
                          session_factory=sf, redis=redis) == 0
    assert await _ledger(url) == led1
    from worker.backfill_budget_legacy import build_plan

    plan = await build_plan(sf, tz=TZ, period=period, cutoff=cutoff)
    assert plan.fixes == [] and plan.already_done >= 4

    # 5. 진짜 불일치는 여전히 잡힌다 — 원장 1 µ$, Redis 카운터 $0.001, 원장 없는 행.
    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.execute("UPDATE budget.budget_usages SET used_usd = used_usd + 0.000001 "
                           "WHERE scope='TEAM' AND scope_id=$1", TEAM)
        await conn.execute(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
            "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
            "completed_at,client) VALUES ('lost-ledger',$1,$2,$3,$4,'BEDROCK',100,10,0.00045,"
            "10,'SUCCESS',$5,$5,'other')", U2, TEAM2, DEPT, MODEL, _utc(today, 0, 1))
    finally:
        await conn.close()
    redis.add(f"budget:user:{{{U3}}}:{period}", Decimal("0.001"))
    hit = await rc.reconcile(sf, redis, report_date=day)
    c = hit["checks"]
    assert hit["status"] == "mismatch"
    assert c["budget_vs_raw"]["mismatches"] >= 3  # TEAM 1 µ$ + U2·TEAM2 원장 누락
    assert c["redis_vs_db"]["mismatches"] == 1
    # 보정을 다시 돌려도 진짜 불일치를 덮지 않는다(이미 감사된 스코프는 건너뛴다).
    assert await backfill(["--cutoff", cut, "--period", period, "--apply"],
                          session_factory=sf, redis=redis) == 0
    again = await rc.reconcile(sf, redis, report_date=day)
    assert again["checks"]["budget_vs_raw"]["mismatches"] >= 3


async def test_unexplained_residue_is_reported_not_fixed(sf, url):
    import asyncpg

    from worker.backfill_budget_legacy import apply_plan, build_plan

    _, _, day = _days()
    period = f"{day.year}-{day.month:02d}"
    redis = FakeRedis()
    conn = await asyncpg.connect(_raw(url))
    try:
        await _legacy_rows(conn, redis, period, [
            dict(rid="x1", user=U1, team=TEAM, client="other", i=100, o=10,
                 cost=Decimal("0.000450"), wsn=0, ws=Decimal("0"), at=_utc(day, 9))])
        # 원장이 반올림으로 설명할 수 없을 만큼 작다($0.0005 vs 행 1개 상한 $0.00005).
        await conn.execute("UPDATE budget.budget_usages SET used_usd = 0 "
                           "WHERE scope_id = ANY($1::uuid[])", [U1, TEAM])
    finally:
        await conn.close()
    plan = await build_plan(sf, tz=TZ, period=period, cutoff=_utc(day, 12))
    assert [f.kind for f in plan.fixes] == []
    assert {u["scope"] for u in plan.unexplained} == {"USER", "TEAM"}
    res = await apply_plan(sf, plan)
    assert res["applied"] == 0 and res["unexplained"] == 2


async def test_boundary_rows_block_apply_until_allowed(sf, url):
    import asyncpg

    from worker.backfill_budget_legacy import run as backfill

    _, _, day = _days()
    period = f"{day.year}-{day.month:02d}"
    cutoff = _utc(day, 12)
    conn = await asyncpg.connect(_raw(url))
    try:
        await _legacy_rows(conn, FakeRedis(), period, [
            dict(rid="edge", user=U1, team=TEAM, client="other", i=100, o=10,
                 cost=Decimal("0.000450"), wsn=1, ws=Decimal("0.007000"),
                 at=cutoff - timedelta(minutes=2))])
    finally:
        await conn.close()
    args = ["--cutoff", cutoff.isoformat(), "--period", period, "--apply", "--no-redis"]
    led0 = await _ledger(url)
    assert await backfill(args, session_factory=sf) == 3
    assert await _ledger(url) == led0
    assert await backfill([*args, "--allow-boundary"], session_factory=sf) == 0
    assert (await _ledger(url))[("USER", U1, None)] == led0[("USER", U1, None)] + Decimal(
        "0.007000") - Decimal("0.000050")  # 웹 검색 + 반올림(0.0005 → 0.00045)


async def test_in_progress_day_detects_rollup_drift_and_stale_hours(sf, url):
    import asyncpg

    from worker import reconciliation as rc
    from worker.daily_aggregator import aggregate_hour, hour_buckets

    now, today, _ = _days()
    done = [(s, e) for s, e in hour_buckets(ZoneInfo(TZ), today) if e <= now]
    if len(done) < 4:
        pytest.skip("오늘 끝난 시간이 4개 미만")
    redis = FakeRedis()
    period = f"{today.year}-{today.month:02d}"
    (s1, _), (s2, e2) = done[-1], done[-4]
    await _wave3_flush(sf, redis, period, [
        _entry("h1", s1 + timedelta(minutes=1), U1, TEAM),
        _entry("h2", s2 + timedelta(minutes=1), U1, TEAM)])
    async with sf() as s:
        await aggregate_hour(s, TZ, today, s1, done[-1][1])
    async with sf() as s:
        r1 = await rc._r1(s, TZ, today, 48, now=now, hourly_stale_sec=7200)
        await s.commit()
    # s2 는 3시간 넘게 시간 롤업이 없다 → stale(불일치 1), s1 은 일치.
    assert (r1["state"], r1["hours_checked"], r1["hours_pending"], r1["hours_stale"]) == (
        "hourly", 1, 1, 1), r1
    assert r1["mismatches"] == 1
    async with sf() as s:
        await aggregate_hour(s, TZ, today, s2, e2)
    conn = await asyncpg.connect(_raw(url))
    try:  # 시간 롤업 변조 → 잡힌다
        await conn.execute("UPDATE usage.hourly_aggregates SET total_cost_usd = "
                           "total_cost_usd + 0.000001 WHERE hour_start=$1", s1)
    finally:
        await conn.close()
    async with sf() as s:
        r1 = await rc._r1(s, TZ, today, 48, now=now, hourly_stale_sec=7200)
        await s.commit()
    assert (r1["hours_checked"], r1["hours_stale"], r1["mismatches"]) == (2, 0, 1), r1
    # 시간 롤업을 끈 배포 — 진행 중인 날은 비교에서 뺀다.
    async with sf() as s:
        r1 = await rc._r1(s, TZ, today, 48, now=now, hourly_enabled=False)
        await s.commit()
    assert (r1["state"], r1["mismatches"]) == ("in_progress_excluded", 0)


async def test_metrics_counter_and_last_success(sf, url):
    from worker import reconciliation as rc

    _, _, day = _days()
    m = MagicMock()
    rc.LAST_SUCCESS.clear()
    await _wave3_flush(sf, FakeRedis(), f"{day.year}-{day.month:02d}",
                       [_entry("m1", _utc(day, 9), U1, TEAM)])
    res = await rc.reconcile(sf, None, report_date=day, metrics=m)  # 일 집계 없음 → 시간 모드
    assert res["status"] in ("ok", "mismatch")
    assert rc.LAST_SUCCESS["ts"] > 0
    calls = {c.args[1]["check"]: c.args[0]
             for c in m.reconciliation_mismatch_total.add.call_args_list}
    assert set(calls) == set(res["checks"])
    assert sum(calls.values()) == res["mismatch_count"]
    rc.LAST_SUCCESS.clear()
    await rc.load_last_success(sf)
    assert rc.LAST_SUCCESS["ts"] > 0
    # 오류 실행은 마지막 성공 시각을 옮기지 않는다.
    rc.LAST_SUCCESS["ts"] = 1.0

    def _broken():
        raise RuntimeError("db down")

    res = await rc.reconcile(_broken, None, report_date=day, metrics=m)
    assert res["status"] == "error" and rc.LAST_SUCCESS["ts"] == 1.0

