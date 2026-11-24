# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregates v2 (migration 0060) — 실 PG 의미 검증.

PROOF_DSN(0060 head 스키마의 DB, postgresql+asyncpg://…) 이 있을 때만 돈다. 그 DB 를
템플릿으로 전용 DB 를 만들어 쓰고 지운다.

검증:
  * 리포팅 tz 날짜 경계 — America/New_York DST 전환일(23h/25h) 포함
  * 롤업 == raw (입도 전체), gateway /v1/usage/me·/team 합계(전 status) 불변
  * 레거시 입도 행 교체, 재집계 멱등
  * 늦은 로그 → dirty → 야간 잡이 나이와 무관하게 재집계
  * dirty 표시가 진행 중인 재집계의 행 잠금을 기다린다(경합으로 dirty 가 지워지지 않음)
  * 월 누적 = 일별 합, 신뢰 불가 날짜가 있으면 만들지 않음
  * advisory lock 공유(야간 잡 스킵), 백필 CLI 재개·dry-run
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_rollupproof"
ORG = "a4444444-4444-4444-4444-444444444444"
DEPT = "a3333333-3333-3333-3333-333333333333"
TEAM_A = "a2222222-2222-2222-2222-22222222222a"
TEAM_B = "a2222222-2222-2222-2222-22222222222b"
U1 = "a1111111-1111-1111-1111-111111111111"
U2 = "a1111111-1111-1111-1111-111111111112"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG(0060 head) 필요")


def _split(dsn):
    pre, _, name = dsn.rpartition("/")
    return pre, name


def _raw(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def url():
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                 "WHERE datname=:d AND pid<>pg_backend_pid()"), {"d": name})
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    u = f"{pre}/{owned}"
    conn = await asyncpg.connect(_raw(u))
    try:
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'ru-org') "
                           "ON CONFLICT DO NOTHING", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'ru-d') "
                           "ON CONFLICT DO NOTHING", DEPT, ORG)
        for t, n in ((TEAM_A, "ru-a"), (TEAM_B, "ru-b")):
            await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,$3) "
                               "ON CONFLICT DO NOTHING", t, DEPT, n)
        for uid, n in ((U1, "ru1"), (U2, "ru2")):
            await conn.execute(
                "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
                "VALUES ($1,$2,$3,$4,'DEVELOPER',$4) ON CONFLICT DO NOTHING",
                uid, TEAM_A, f"{n}@example.invalid", n)
    finally:
        await conn.close()
    yield u
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture
async def factory(url):
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    conn = await asyncpg.connect(_raw(url))
    await conn.execute(
        "TRUNCATE usage.usage_logs, usage.daily_aggregates, usage.daily_aggregate_days, "
        "usage.monthly_aggregates, usage.monthly_aggregate_months CASCADE")
    await conn.close()
    engine = create_async_engine(url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _insert(url, rows):
    """rows: (requested_at, user, team, model, client, status, in, out, cw, cr, cost, latency)."""
    import asyncpg

    conn = await asyncpg.connect(_raw(url))
    try:
        await conn.executemany(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,provider,"
            "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens,cost_usd,latency_ms,"
            "status,requested_at,completed_at,client) VALUES "
            "($1,$2,$3,$4,$5,'BEDROCK',$6,$7,$8,$9,$10,$11,$12::usage.usage_status,$13,$13,$14)",
            [(str(uuid.uuid4()), r[1], r[2], DEPT, r[3], r[6], r[7], r[8], r[9], Decimal(r[10]),
              r[11], r[5], r[0], r[4]) for r in rows])
    finally:
        await conn.close()


async def _fetch(url, sql, *args):
    import asyncpg

    conn = await asyncpg.connect(_raw(url))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def _z(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


_GRAIN_RAW = """
SELECT (requested_at AT TIME ZONE $1)::date d, user_id, team_id, dept_id, model_alias,
       COALESCE(client,'other') client, status::text status,
       sum(input_tokens) i, sum(output_tokens) o, sum(cache_creation_tokens) cw,
       sum(cache_read_tokens) cr, sum(cost_usd) c, count(*) n, sum(latency_ms) lat,
       count(*) FILTER (WHERE input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens>0
                        OR cost_usd>0) rq
FROM usage.usage_logs GROUP BY 1,2,3,4,5,6,7
"""
_GRAIN_ROLLUP = """
SELECT date d, user_id, team_id, dept_id, model_alias, client, status::text status,
       input_tokens i, output_tokens o, cache_creation_tokens cw, cache_read_tokens cr,
       total_cost_usd c, call_count n, latency_ms_sum lat, request_count rq
FROM usage.daily_aggregates
"""


def _norm(rows):
    return sorted(tuple(str(v) for v in r.values()) for r in rows)


async def test_dst_new_york_day_boundaries(url, factory):
    from worker import daily_aggregator as da

    tz = "America/New_York"
    m = ("ru-m", "claude-code", "SUCCESS", 1, 1, 0, 0, "0.01", 10)
    await _insert(url, [
        (_z("2026-03-08T04:59:00"), U1, TEAM_A, *m),  # 03-07 23:59 EST
        (_z("2026-03-09T03:59:00"), U1, TEAM_A, *m),  # 03-08 23:59 EDT (23h day)
        (_z("2026-03-09T04:00:00"), U1, TEAM_A, *m),  # 03-09 00:00 EDT
        (_z("2026-11-01T04:30:00"), U1, TEAM_A, *m),  # 11-01 00:30 EDT
        (_z("2026-11-02T04:30:00"), U1, TEAM_A, *m),  # 11-01 23:30 EST (25h day)
        (_z("2026-11-02T05:10:00"), U1, TEAM_A, *m),  # 11-02 00:10 EST
    ])
    days = [date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 9),
            date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]
    async with factory() as s:
        await da.recompute_days(s, tz, days, today_local=date(2026, 11, 20))
    got = {r["date"]: r["n"] for r in await _fetch(
        url, "SELECT date, sum(call_count) n FROM usage.daily_aggregates GROUP BY 1")}
    assert got == {date(2026, 3, 7): 1, date(2026, 3, 8): 1, date(2026, 3, 9): 1,
                   date(2026, 11, 1): 2, date(2026, 11, 2): 1}
    assert _norm(await _fetch(url, _GRAIN_RAW, tz)) == _norm(await _fetch(url, _GRAIN_ROLLUP))
    reg = await _fetch(url, "SELECT count(*) FILTER (WHERE NOT dirty AND grain_version=2 "
                            "AND tz=$1) ok, count(*) n FROM usage.daily_aggregate_days", tz)
    assert reg[0]["ok"] == reg[0]["n"] == 6  # 빈 날짜(10-31)도 레지스트리 행이 생긴다


def _mixed_rows(day0: datetime):
    """3일 · 2 사용자 · 팀 이동(U1 이 둘째 날 정오에 A→B) · 앱 NULL/3종 · status 3종."""
    rows = []
    clients = ["claude-code", "cowork", None, "codex"]
    statuses = ["SUCCESS", "SUCCESS", "SUCCESS", "ERROR", "TIMEOUT"]
    k = 0
    for d in range(3):
        for h in range(0, 24, 3):
            for uid in (U1, U2):
                at = day0 + timedelta(days=d, hours=h, minutes=7)
                team = TEAM_B if (uid == U1 and (d, h) >= (1, 12)) else TEAM_A
                st = statuses[k % 5]
                zero = st != "SUCCESS" and k % 2 == 0   # 0 토큰 마커 행
                rows.append((at, uid, team, f"ru-m{k % 3}", clients[k % 4], st,
                             0 if zero else 100 + k, 0 if zero else 10 + k, k % 7, k % 11,
                             "0" if zero else f"0.{k:04d}", 50 + k))
                k += 1
    return rows


async def test_rollup_equals_raw_and_gateway_totals_unchanged(url, factory):
    from worker import daily_aggregator as da

    tz = "Asia/Kolkata"
    day0 = datetime(2026, 11, 10, tzinfo=ZoneInfo(tz))
    await _insert(url, _mixed_rows(day0))
    # 레거시 입도 행 — 재집계가 지워야 한다.
    await _fetch(url, "INSERT INTO usage.daily_aggregates (date,user_id,team_id,dept_id,"
                      "model_alias,total_tokens,total_cost_usd,request_count) "
                      "VALUES ('2026-11-10',$1,$2,$3,'legacy',999,9,9)", U1, TEAM_A, DEPT)
    days = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12),
            date(2026, 11, 13)]
    for _ in range(2):  # 멱등
        async with factory() as s:
            await da.recompute_days(s, tz, days, today_local=date(2026, 11, 20))
    assert _norm(await _fetch(url, _GRAIN_RAW, tz)) == _norm(await _fetch(url, _GRAIN_ROLLUP))
    assert not await _fetch(url, "SELECT 1 FROM usage.daily_aggregates WHERE status IS NULL")

    # gateway /v1/usage/me — (user, date) 별 total_tokens·total_cost_usd 합(전 status).
    me_raw = await _fetch(url, """
        SELECT user_id, (requested_at AT TIME ZONE $1)::date d,
               sum(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens) t,
               sum(cost_usd) c FROM usage.usage_logs GROUP BY 1,2""", tz)
    me_rollup = await _fetch(url, """
        SELECT user_id, date d, sum(total_tokens) t, sum(total_cost_usd) c
        FROM usage.daily_aggregates GROUP BY 1,2""")
    assert _norm(me_raw) == _norm(me_rollup)
    # gateway /v1/usage/team — 팀별 월 합계. 팀 이동자는 요청 시점 팀으로 정확히 귀속된다.
    team_raw = await _fetch(url, "SELECT team_id, sum(cost_usd) c FROM usage.usage_logs "
                                 "GROUP BY 1")
    team_rollup = await _fetch(url, "SELECT team_id, sum(total_cost_usd) c "
                                    "FROM usage.daily_aggregates GROUP BY 1")
    assert _norm(team_raw) == _norm(team_rollup)

    # 월 누적 = 그 달 일별 합(SUCCESS·전 status 모두 같은 입도로).
    async with factory() as s:
        await da.recompute_days(
            s, tz, [date(2026, 11, 1) + timedelta(days=i) for i in range(19)],
            today_local=date(2026, 11, 20))
    month = await _fetch(url, """
        SELECT user_id, team_id, model_alias, client, status::text, sum(total_tokens) t,
               sum(total_cost_usd) c, sum(call_count) n, sum(latency_ms_sum) l
        FROM usage.daily_aggregates GROUP BY 1,2,3,4,5""")
    mtd = await _fetch(url, """
        SELECT user_id, team_id, model_alias, client, status::text, total_tokens t,
               total_cost_usd c, call_count n, latency_ms_sum l
        FROM usage.monthly_aggregates WHERE period = '2026-11-01'""")
    assert _norm(month) == _norm(mtd)
    reg = await _fetch(url, "SELECT covered_through, tz FROM usage.monthly_aggregate_months")
    assert [(r["covered_through"], r["tz"]) for r in reg] == [(date(2026, 11, 19), tz)]


async def test_month_not_built_with_untrusted_day(url, factory):
    from worker import daily_aggregator as da

    async with factory() as s:
        await da.recompute_days(s, "UTC", [date(2026, 9, 1) + timedelta(days=i)
                                           for i in range(30) if i != 14],
                                today_local=date(2026, 11, 20))
        assert await da.rebuild_month(s, "UTC", date(2026, 9, 1), date(2026, 11, 20)) is None
        await da.aggregate_day(s, "UTC", date(2026, 9, 15))
        assert await da.rebuild_month(s, "UTC", date(2026, 9, 1), date(2026, 11, 20)) == 0
    # tz 가 다르면 신뢰하지 않는다.
    async with factory() as s:
        assert await da.rebuild_month(
            s, "Asia/Seoul", date(2026, 9, 1), date(2026, 11, 20)) is None


class _Pipe:
    def __getattr__(self, _n):
        return lambda *a, **k: None

    async def execute(self):
        return []


async def _flush(factory, entries, tz):
    import worker.batch_flusher as bf

    redis = MagicMock()
    redis.pipeline = lambda *a, **k: _Pipe()
    s = MagicMock()
    s.reporting_timezone = tz
    orig = bf.get_settings
    bf.get_settings = lambda: s
    try:
        await bf.BatchFlusher(session_factory=factory, redis=redis).flush(entries)
    finally:
        bf.get_settings = orig


def _entry(at: datetime, rid: str):
    from worker.schemas.cost_stream import CostStreamEntry

    iso = at.isoformat()
    return CostStreamEntry(
        request_id=rid, user_id=U2, team_id=TEAM_A, dept_id=DEPT, model_alias="ru-late",
        provider="BEDROCK", input_tokens=7, output_tokens=3, cost_usd=Decimal("0.5"),
        latency_ms=40, status="SUCCESS", requested_at=iso, completed_at=iso,
        period=iso[:7], date=iso[:10], client="cowork",
    )


async def test_late_log_marks_dirty_and_nightly_recomputes_any_age(url, factory, monkeypatch):
    from worker import daily_aggregator as da

    tz = "Asia/Kolkata"
    today = datetime.now(ZoneInfo(tz)).date()
    old = today - timedelta(days=60)          # 최근 8일·lookback 30일 밖
    async with factory() as s:
        await da.recompute_days(s, tz, [old], today_local=today)
    assert not (await _fetch(url, "SELECT dirty FROM usage.daily_aggregate_days "
                                  "WHERE date=$1", old))[0]["dirty"]

    noon = datetime.combine(old, datetime.min.time(), tzinfo=ZoneInfo(tz)) + timedelta(hours=12)
    now = datetime.now(ZoneInfo(tz))
    await _flush(factory, [_entry(noon, "late-1"), _entry(now, "today-1")], tz)
    reg = {r["date"]: r["dirty"] for r in await _fetch(
        url, "SELECT date, dirty FROM usage.daily_aggregate_days")}
    assert reg[old] is True
    assert today not in reg  # 오늘 로그는 표시하지 않는다

    settings = MagicMock(reporting_timezone=tz, daily_usage_agg_recent_days=8,
                         daily_usage_agg_backfill_lookback_days=30)
    monkeypatch.setattr(da, "get_settings", lambda: settings)
    await da.run_daily_aggregation(factory)
    row = await _fetch(url, "SELECT dirty, grain_version FROM usage.daily_aggregate_days "
                            "WHERE date=$1", old)
    assert (row[0]["dirty"], row[0]["grain_version"]) == (False, 2)
    agg = await _fetch(url, "SELECT call_count, client FROM usage.daily_aggregates "
                            "WHERE date=$1 AND model_alias='ru-late'", old)
    assert [(r["call_count"], r["client"]) for r in agg] == [(1, "cowork")]
    # 최근 8일·lookback 은 빈 날짜도 레지스트리 행(clean)을 남긴다 — 월 누적의 전제.
    n = await _fetch(url, "SELECT count(*) n FROM usage.daily_aggregate_days WHERE date >= $1 "
                          "AND date < $2 AND NOT dirty AND grain_version=2",
                     today - timedelta(days=30), today)
    assert n[0]["n"] == 30


async def test_dirty_mark_waits_for_inflight_recompute(url, factory):
    """재집계 트랜잭션이 레지스트리 행을 쥔 동안 들어온 늦은 로그의 dirty 표시는 그
    커밋 뒤에 적용된다 — 재집계 스냅샷이 못 본 행이 clean 으로 묻히지 않는다."""
    from sqlalchemy import text

    from worker import batch_flusher as bf
    from worker import daily_aggregator as da

    tz = "UTC"
    day = date(2026, 8, 3)
    start, end = da._day_window(ZoneInfo(tz), day)
    async with factory() as j, factory() as f:
        await j.execute(text(da._LOCK_DAY_SQL), {"day": day})
        await j.execute(text(da._DELETE_DAY_SQL), {"day": day})
        await j.execute(text(da._AGG_SQL), {"day": day, "start": start, "end": end})

        async def _late():
            await f.execute(text(
                "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
                "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
                "completed_at) VALUES ('race-1',:u,:t,:d,'ru-race','BEDROCK',1,1,0.1,1,"
                "'SUCCESS',:at,:at)"), {"u": U1, "t": TEAM_A, "d": DEPT, "at": start})
            await f.execute(bf._MARK_LATE_DAYS_DIRTY, {"days": [day]})
            await f.commit()

        task = asyncio.create_task(_late())
        await asyncio.sleep(0.5)
        assert not task.done()  # 행 잠금 대기 중
        await j.execute(text(da._MARK_DAY_SQL), {"day": day, "tz": tz, "grain": 2})
        await j.commit()
        await asyncio.wait_for(task, 5)
    row = await _fetch(url, "SELECT dirty FROM usage.daily_aggregate_days WHERE date=$1", day)
    assert row[0]["dirty"] is True


async def test_nightly_skips_when_lock_held_and_backfill_cli_resumes(url, factory, monkeypatch):
    from sqlalchemy import text

    from worker import backfill_daily_aggregates as cli
    from worker import daily_aggregator as da

    tz = "UTC"
    settings = MagicMock(reporting_timezone=tz, daily_usage_agg_recent_days=8,
                         daily_usage_agg_backfill_lookback_days=30)
    monkeypatch.setattr(da, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    await _insert(url, [(_z("2026-07-02T10:00:00"), U1, TEAM_A, "ru-bf", None, "SUCCESS",
                         5, 5, 0, 0, "0.1", 9)])

    engine = factory.kw["bind"]
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(727001)"))
        await da.run_daily_aggregation(factory)
        assert await cli.run(["--from", "2026-07-01", "--to", "2026-07-03", "--wait-lock", "0"],
                             session_factory=factory) == 2
        await holder.execute(text("SELECT pg_advisory_unlock(727001)"))
        await holder.commit()
    assert not await _fetch(url, "SELECT 1 FROM usage.daily_aggregate_days")

    assert await cli.run(["--from", "2026-07-01", "--to", "2026-07-03", "--dry-run"],
                         session_factory=factory) == 0
    assert not await _fetch(url, "SELECT 1 FROM usage.daily_aggregate_days")
    assert await cli.run([], session_factory=factory) == 0  # 기본: 최초 달 1일 ~ 어제
    first = await _fetch(url, "SELECT min(date) a, max(date) b, count(*) n "
                              "FROM usage.daily_aggregate_days")
    assert first[0]["a"] == date(2026, 7, 1)
    assert await _fetch(url, "SELECT period FROM usage.monthly_aggregate_months "
                             "WHERE period='2026-07-01'")
    stamp = await _fetch(url, "SELECT max(aggregated_at) t FROM usage.daily_aggregate_days")
    assert await cli.run([], session_factory=factory) == 0  # 재실행 — 끝난 날짜는 건너뜀
    again = await _fetch(url, "SELECT max(aggregated_at) t FROM usage.daily_aggregate_days")
    assert again[0]["t"] == stamp[0]["t"]
