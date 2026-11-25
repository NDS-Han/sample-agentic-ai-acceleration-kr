# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregates v2/v3 (migration 0060/0061) — 실 PG 의미 검증.

PROOF_DSN(0061 head 스키마의 DB, postgresql+asyncpg://…) 이 있을 때만 돈다. 그 DB 를
템플릿으로 전용 DB 를 만들어 쓰고 지운다.

검증:
  * 리포팅 tz 날짜 경계 — America/New_York DST 전환일(23h/25h) 포함
  * 롤업 == raw (입도 전체), gateway /v1/usage/me·/team 합계(전 status) 불변
  * 레거시 입도 행 교체, 재집계 멱등
  * 늦은 로그 → dirty 표시(rollup_dirty_marks) → 야간/시간 잡이 나이와 무관하게 재집계
  * dirty 표시는 진행 중인 재집계를 **기다리지 않고**, 재집계 커밋 뒤에도 남는다(REPEATABLE READ)
  * 시간 롤업 == raw(시간 버킷), 일 집계가 그 날짜의 시간 행을 정리, 시간 잡 end-to-end
  * skip-forward: 한 날짜가 실패(타임아웃)해도 다음 날짜를 집계하고 failed_at 을 남긴다
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


def _settings(tz: str, **kw):
    base = dict(reporting_timezone=tz, daily_usage_agg_recent_days=2,
                daily_usage_agg_backfill_lookback_days=30, daily_usage_agg_command_timeout=60.0,
                daily_usage_agg_nightly_lock_wait_sec=0, daily_usage_agg_failed_retry_sec=10800,
                daily_usage_agg_hourly_bridge_days=2, rollup_dirty_margin_sec=120)
    base.update(kw)
    return MagicMock(**base)


@pytest.fixture(autouse=True)
def _agg_settings(monkeypatch):
    from worker import daily_aggregator as da

    monkeypatch.setattr(da, "get_settings", lambda: _settings("UTC"))


@pytest.fixture
async def factory(url):
    """집계 세션 — 운영 집계 엔진과 같은 REPEATABLE READ(db.create_aggregator_engine)."""
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    conn = await asyncpg.connect(_raw(url))
    await conn.execute(
        "TRUNCATE usage.usage_logs, usage.daily_aggregates, usage.daily_aggregate_days, "
        "usage.monthly_aggregates, usage.monthly_aggregate_months, usage.hourly_aggregates, "
        "usage.hourly_aggregate_hours, usage.rollup_dirty_marks CASCADE")
    await conn.close()
    engine = create_async_engine(url, isolation_level="REPEATABLE READ")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def flush_factory(url):
    """flush 세션 — 워커 flush 엔진과 같은 READ COMMITTED."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
    reg = await _fetch(url, "SELECT count(*) FILTER (WHERE NOT dirty AND grain_version=3 "
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
    s = _settings(tz)

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


async def test_late_log_marks_dirty_and_nightly_recomputes_any_age(url, factory, flush_factory,
                                                                  monkeypatch):
    from worker import daily_aggregator as da

    tz = "Asia/Kolkata"
    monkeypatch.setattr(da, "get_settings", lambda: _settings(tz))
    today = datetime.now(ZoneInfo(tz)).date()
    old = today - timedelta(days=60)          # 최근 2일·lookback 30일 밖
    async with factory() as s:
        await da.recompute_days(s, tz, [old], today_local=today)
    assert not await _fetch(url, "SELECT 1 FROM usage.rollup_dirty_marks")

    noon = datetime.combine(old, datetime.min.time(), tzinfo=ZoneInfo(tz)) + timedelta(hours=12)
    now = datetime.now(ZoneInfo(tz))
    await _flush(flush_factory, [_entry(noon, "late-1"), _entry(now, "today-1")], tz)
    marks = await _fetch(url, "SELECT date, hour_start FROM usage.rollup_dirty_marks")
    assert {(r["date"], r["hour_start"] is None) for r in marks} == {(old, True), (old, False)}
    # 레지스트리는 건드리지 않는다(재집계 행 잠금을 기다리는 경로 제거).
    assert not (await _fetch(url, "SELECT dirty FROM usage.daily_aggregate_days "
                                  "WHERE date=$1", old))[0]["dirty"]

    await da.run_daily_aggregation(factory)
    row = await _fetch(url, "SELECT dirty, grain_version FROM usage.daily_aggregate_days "
                            "WHERE date=$1", old)
    assert (row[0]["dirty"], row[0]["grain_version"]) == (False, 3)
    assert not await _fetch(url, "SELECT 1 FROM usage.rollup_dirty_marks WHERE date=$1", old)
    agg = await _fetch(url, "SELECT call_count, client FROM usage.daily_aggregates "
                            "WHERE date=$1 AND model_alias='ru-late'", old)
    assert [(r["call_count"], r["client"]) for r in agg] == [(1, "cowork")]
    # lookback 은 빈 날짜도 레지스트리 행(clean)을 남긴다 — 월 누적의 전제.
    n = await _fetch(url, "SELECT count(*) n FROM usage.daily_aggregate_days WHERE date >= $1 "
                          "AND date < $2 AND NOT dirty AND grain_version=3",
                     today - timedelta(days=30), today)
    assert n[0]["n"] == 30


async def test_dirty_mark_does_not_wait_and_survives_inflight_recompute(
        url, factory, flush_factory):
    """재집계 트랜잭션이 진행 중이어도 늦은 로그의 flush 는 기다리지 않고 커밋되고, 그 표시는
    재집계(REPEATABLE READ 스냅샷이 못 본 행)의 커밋 뒤에도 남는다 → 다음 잡이 다시 집계."""
    from sqlalchemy import text

    from worker import batch_flusher as bf
    from worker import daily_aggregator as da

    tz = "UTC"
    day = date(2026, 8, 3)
    start, end = da._day_window(ZoneInfo(tz), day)
    async with factory() as j, flush_factory() as f:
        await j.execute(text(da._LOCK_DAY_SQL), {"day": day})
        await j.execute(text(da._DELETE_DAY_SQL), {"day": day})
        await j.execute(text(da._AGG_SQL), {"day": day, "start": start, "end": end})

        t0 = asyncio.get_running_loop().time()
        await f.execute(text(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
            "provider,input_tokens,output_tokens,cost_usd,latency_ms,status,requested_at,"
            "completed_at) VALUES ('race-1',:u,:t,:d,'ru-race','BEDROCK',1,1,0.1,1,"
            "'SUCCESS',:at,:at)"), {"u": U1, "t": TEAM_A, "d": DEPT, "at": start})
        await f.execute(bf._INSERT_DIRTY_MARKS, {"days": [day, day], "hours": [None, start]})
        await asyncio.wait_for(f.commit(), 2)          # 잠금 대기 없음
        assert asyncio.get_running_loop().time() - t0 < 1.0

        await j.execute(text(da._CLEAR_DAY_MARKS_SQL), {"day": day})
        await j.execute(text(da._MARK_DAY_SQL), {"day": day, "tz": tz, "grain": 3})
        await j.commit()
    marks = await _fetch(url, "SELECT hour_start FROM usage.rollup_dirty_marks WHERE date=$1", day)
    assert len(marks) == 2  # 스냅샷 뒤 표시는 지워지지 않았다
    agg = await _fetch(url, "SELECT 1 FROM usage.daily_aggregates WHERE model_alias='ru-race'")
    assert agg == []        # 재집계는 그 행을 못 봤다 — 그래서 표시가 남아야 한다
    async with factory() as s:
        await da.aggregate_day(s, tz, day)
    assert await _fetch(url, "SELECT 1 FROM usage.daily_aggregates WHERE model_alias='ru-race'")
    assert not await _fetch(url, "SELECT 1 FROM usage.rollup_dirty_marks")


_HOUR_RAW = """
SELECT date_bin('1 hour', requested_at, $1::timestamptz) hs, user_id, team_id, model_alias,
       COALESCE(client,'other') client, status::text status, sum(input_tokens) i,
       sum(cost_usd) c, count(*) n, sum(web_search_count) wq, sum(web_search_cost_usd) wc,
       COALESCE(sum(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL), 0) wu
FROM usage.usage_logs WHERE requested_at >= $1 AND requested_at < $2 GROUP BY 1,2,3,4,5,6
"""
_HOUR_ROLLUP = """
SELECT hour_start hs, user_id, team_id, model_alias, client, status::text status,
       input_tokens i, total_cost_usd c, call_count n, web_search_count wq,
       web_search_cost_usd wc, web_search_untracked wu
FROM usage.hourly_aggregates WHERE date = $1
"""


async def test_hourly_rollup_equals_raw_and_daily_supersedes(url, factory):
    """시간 롤업 == raw(30분 오프셋 tz), 웹 검색 3값 포함. 일 집계가 그 날짜 시간 행을 지운다."""
    import asyncpg

    from worker import daily_aggregator as da

    tz = "Asia/Kolkata"
    day = date(2026, 11, 10)
    z = ZoneInfo(tz)
    await _insert(url, _mixed_rows(datetime(2026, 11, 10, tzinfo=z)))
    conn = await asyncpg.connect(_raw(url))
    try:  # 웹 검색: 추적(비용 있음) · 미추적(NULL) · 0 이 섞이게
        await conn.execute("UPDATE usage.usage_logs SET web_search_count = (latency_ms % 3), "
                           "web_search_cost_usd = CASE WHEN latency_ms % 2 = 0 "
                           "THEN (latency_ms % 3) * 0.007 END")
    finally:
        await conn.close()
    buckets = da.hour_buckets(z, day)
    async with factory() as s:
        for b0, b1 in buckets:
            await da.aggregate_hour(s, tz, day, b0, b1)
    s0, e0 = da._day_window(z, day)
    raw = await _fetch(url, _HOUR_RAW, s0, e0)
    assert _norm(raw) == _norm(await _fetch(url, _HOUR_ROLLUP, day))
    reg = await _fetch(url, "SELECT count(*) n FROM usage.hourly_aggregate_hours WHERE date=$1 "
                            "AND grain_version=3 AND tz=$2", day, tz)
    assert reg[0]["n"] == 24
    async with factory() as s:
        await da.aggregate_day(s, tz, day)
    assert not await _fetch(url, "SELECT 1 FROM usage.hourly_aggregates WHERE date=$1", day)
    assert not await _fetch(url, "SELECT 1 FROM usage.hourly_aggregate_hours WHERE date=$1", day)
    # 일 롤업 웹 검색 3값 == raw
    raw = await _fetch(url, "SELECT sum(web_search_count) q, sum(web_search_cost_usd) c, "
                            "sum(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL) u "
                            "FROM usage.usage_logs WHERE requested_at >= $1 AND requested_at < $2",
                       s0, e0)
    roll = await _fetch(url, "SELECT sum(web_search_count) q, sum(web_search_cost_usd) c, "
                             "sum(web_search_untracked) u FROM usage.daily_aggregates "
                             "WHERE date=$1",
                        day)
    assert tuple(raw[0].values()) == tuple(roll[0].values())


async def test_hourly_job_end_to_end_with_late_row(url, factory, flush_factory, monkeypatch):
    """시간 잡: 오늘의 완료된 시간을 채우고, 늦은 행 표시가 있는 시간만 다음 회차에 다시 집계."""
    from worker import daily_aggregator as da

    # 지금 로컬 시각이 03~22시인 tz 를 고른다(오늘 완료된 시간이 몇 개 있고 자정 근처가 아님).
    tz = next(t for t in ("UTC", "Asia/Kolkata", "Pacific/Kiritimati", "America/New_York",
                          "Pacific/Honolulu") if 3 <= datetime.now(ZoneInfo(t)).hour <= 22)
    z = ZoneInfo(tz)
    now = datetime.now(z)
    today = now.date()
    monkeypatch.setattr(da, "get_settings", lambda: _settings(tz))
    t1 = datetime(today.year, today.month, today.day, 1, 15, tzinfo=z).astimezone(UTC)
    hb = da.bucket_of(z, t1)[1]
    await _insert(url, [(t1, U1, TEAM_A, "ru-h", "cowork", "SUCCESS", 5, 5, 0, 0, "0.1", 9)])
    await da.run_hourly_rollup(factory)
    reg = {r["hour_start"] for r in await _fetch(
        url, "SELECT hour_start FROM usage.hourly_aggregate_hours WHERE date=$1", today)}
    assert len(reg) == now.hour  # 0 .. now.hour-1 (진행 중인 시간 제외)
    stamp = await _fetch(url, "SELECT hour_start, aggregated_at FROM usage.hourly_aggregate_hours")
    # 늦은 행(01:30) — 표시가 생기고, 다음 회차는 그 시간만 다시 집계한다.
    await _flush(flush_factory, [_entry(t1 + timedelta(minutes=15), "late-h")], tz)
    assert await _fetch(url, "SELECT 1 FROM usage.rollup_dirty_marks WHERE hour_start=$1",
                        hb)
    await da.run_hourly_rollup(factory)
    after = {r["hour_start"]: r["aggregated_at"] for r in await _fetch(
        url, "SELECT hour_start, aggregated_at FROM usage.hourly_aggregate_hours")}
    changed = [h for h, at in ((r["hour_start"], r["aggregated_at"]) for r in stamp)
               if after[h] != at]
    assert changed == [hb]
    n = await _fetch(url, "SELECT sum(call_count) n FROM usage.hourly_aggregates "
                          "WHERE hour_start=$1",
                     hb)
    assert n[0]["n"] == 2
    assert not await _fetch(url, "SELECT 1 FROM usage.rollup_dirty_marks")


async def test_skip_forward_on_failed_day(url, factory, monkeypatch):
    """한 날짜가 statement_timeout 으로 실패해도 나머지 날짜는 집계되고 failed_at 이 남는다."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from worker import daily_aggregator as da

    monkeypatch.setattr(da, "get_settings",
                        lambda: _settings("UTC", daily_usage_agg_command_timeout=1))
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    await _fetch(url, "INSERT INTO usage.daily_aggregates (date,user_id,team_id,dept_id,"
                      "model_alias,total_tokens,total_cost_usd,request_count) "
                      "VALUES ('2026-06-02',$1,$2,$3,'legacy',1,1,1)", U1, TEAM_A, DEPT)
    holder = create_async_engine(url)
    try:
        async with holder.connect() as h:  # 6/2 롤업 행을 잠그고 놓지 않는다 → DELETE 가 타임아웃
            await h.execute(text_("SELECT 1 FROM usage.daily_aggregates "
                                  "WHERE date = '2026-06-02' FOR UPDATE"))
            async with factory() as s:
                await da.recompute_days(s, "UTC", days, today_local=date(2026, 6, 10))
            await h.rollback()
    finally:
        await holder.dispose()
    reg = {r["date"]: (r["aggregated_at"] is not None, r["failed_at"] is not None, r["last_error"])
           for r in await _fetch(url, "SELECT date, aggregated_at, failed_at, last_error "
                                      "FROM usage.daily_aggregate_days")}
    assert reg[date(2026, 6, 1)][:2] == (True, False)
    assert reg[date(2026, 6, 3)][:2] == (True, False)
    assert reg[date(2026, 6, 2)][:2] == (False, True)
    assert "timeout" in reg[date(2026, 6, 2)][2]
    # 재시도에서 성공하면 failed_at 이 지워진다.
    async with factory() as s:
        await da.recompute_days(s, "UTC", [date(2026, 6, 2)], today_local=date(2026, 6, 10))
    row = await _fetch(url, "SELECT failed_at FROM usage.daily_aggregate_days "
                            "WHERE date='2026-06-02'")
    assert row[0]["failed_at"] is None


def text_(sql):
    from sqlalchemy import text

    return text(sql)


async def test_nightly_skips_when_lock_held_and_backfill_cli_resumes(url, factory, monkeypatch):
    from sqlalchemy import text

    from worker import backfill_daily_aggregates as cli
    from worker import daily_aggregator as da

    tz = "UTC"
    settings = _settings(tz)
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


async def test_hourly_job_rebuilds_stale_month(url, factory, monkeypatch):
    """일 재집계가 월 재구성 전에 끊긴 달(aggregated_at > built_at)은 시간 잡이 다시 만든다."""
    from worker import daily_aggregator as da

    tz = "UTC"
    monkeypatch.setattr(da, "get_settings", lambda: _settings(tz))
    today = datetime.now(UTC).date()
    day = today - timedelta(days=3)
    if day.month != (today - timedelta(days=1)).month or day.day == 1:
        pytest.skip("월 경계 근처 — 같은 달의 지난 날짜가 필요하다")
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5)
    await _insert(url, [(at, U1, TEAM_A, "ru-sm", "cowork", "SUCCESS", 1, 1, 0, 0, "0.1", 1)])
    first = day.replace(day=1)
    async with factory() as s:
        days = [first + timedelta(days=i) for i in range((today - first).days)]
        await da.recompute_days(s, tz, days, today_local=today)
    sql = "SELECT built_at FROM usage.monthly_aggregate_months"
    built = (await _fetch(url, sql))[0]["built_at"]
    # 월 재구성 없이 하루만 다시 집계(중단된 백필 흉내) → 월 누적이 낡는다
    async with factory() as s:
        await da.aggregate_day(s, tz, day)
    await da.run_hourly_rollup(factory)
    after = (await _fetch(url, sql))[0]["built_at"]
    assert after > built
