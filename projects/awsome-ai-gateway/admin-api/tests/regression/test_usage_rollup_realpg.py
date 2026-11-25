# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""하이브리드 롤업 읽기 == raw — 실 PostgreSQL 속성 테스트 (wave 2).

PROOF_DSN 이 있을 때만 돈다. 자기 소유 DB(<dbname>_hybridproof)에 db/init 을 적용하고,
**cost-recorder-worker 의 실제 집계 코드**(daily_aggregator: 일/시간/월 롤업)로 롤업을 만든 뒤
admin-api 하이브리드 읽기(services/usage_rollup·usage_reads)가 usage_logs 를 직접 GROUP BY 한
결과와 **입도 단위로** 같은지 무작위 범위·필터로 확인한다.

축:
  * 리포팅 tz = Asia/Kolkata(+05:30) — 날짜·시간 버킷이 UTC 정시와 어긋난다. 날짜 경계(자정 ±1s),
    월 경계(10-31/11-01), 시간 경계 탐침 행
  * custom 구간(≤366일, 9월 이전 빈 날짜 포함) · 월 period · 앱(client, NULL→other) · 팀 스코프
    (1팀·여러 팀, 월중 팀 이동 사용자) · success_only · by_day
  * 신뢰할 수 없는 소스를 **오염**시켜 둔다 — 쓰이면 raw 와 달라진다:
      늦은 행(날짜·시간 표시), 실패한 날짜(failed_at, 레지스트리 aggregated_at 없음), grain 2 날짜,
      tz 다른 날짜, dirty 플래그, 월 재구성 뒤 다시 집계된 날짜(월 누적 stale), 표시된 시간의 시간 롤업
  * 오늘: 완료된 시간 = 시간 롤업, 진행 중인 시간 = raw
  * delayed_days 는 정확히 "신뢰 불가 과거 날짜 ∩ 구간", 신뢰 가능한 달의 raw 버킷은 오늘 진행 중 시간뿐
  * 엔드포인트 수준 합계(totals·client-share·top-users·일별 웹 검색·Analytics 개요·모델 상세)도 raw 와 같다
"""
from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_hybridproof"
_ROOT = Path(__file__).resolve().parents[3]
_INIT_DIR = _ROOT / "db" / "init"
_WORKER_SRC = _ROOT / "cost-recorder-worker" / "src"

TZ = "Asia/Kolkata"
Z = ZoneInfo(TZ)
TODAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 14, 20, tzinfo=Z).astimezone(UTC)  # 14:20 IST — 14시 진행 중
START = date(2026, 9, 25)

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

ORG = uuid.UUID("c4444444-4444-4444-4444-444444444444")
DEPT = uuid.UUID("c3333333-3333-3333-3333-333333333333")
TEAMS = [uuid.UUID(f"c2222222-2222-2222-2222-22222222222{i}") for i in range(3)]
USERS = [uuid.UUID(f"c1111111-1111-1111-1111-11111111111{i}") for i in range(6)]
MODELS = ["hp-m0", "hp-m1", "hp-m2", "hp-m3"]
CLIENTS = ["claude-code", "cowork", "codex", None]
STATUSES = ["SUCCESS"] * 7 + ["ERROR", "TIMEOUT"]

#: 신뢰할 수 없게 만든 과거 날짜(→ delayed_days 와 정확히 같아야 한다).
LATE_DAY = date(2026, 11, 10)       # 늦은 행 + 날짜·시간 표시
FAILED_DAY = date(2026, 11, 12)     # 집계 실패(failed_at), aggregated_at NULL
GRAIN2_DAY = date(2026, 10, 20)     # 0060 grain 2 — 웹 검색 열 없음
TZ_DAY = date(2026, 10, 7)          # 다른 tz 로 집계됨
DIRTY_DAY = date(2026, 11, 3)       # 레거시 dirty 플래그
YESTERDAY = TODAY - timedelta(days=1)  # 야간 잡 전 — 일 집계 없음, 시간 롤업으로 다리
UNTRUSTED = {LATE_DAY, FAILED_DAY, GRAIN2_DAY, TZ_DAY, DIRTY_DAY}
STALE_MONTH_DAY = date(2026, 9, 28)  # 9월 월 누적 재구성 뒤 다시 집계됨 → 9월은 일별로


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


def _worker():
    """cost-recorder-worker 의 실제 집계 모듈(같은 저장소). 없으면 건너뛴다."""
    if str(_WORKER_SRC) not in sys.path:
        sys.path.insert(0, str(_WORKER_SRC))
    try:
        from worker import daily_aggregator as da  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cost-recorder-worker 모듈을 불러올 수 없다: {exc}")
    return da


def _row(rng: random.Random, at: datetime, *, user=None, team=None, late=False):
    ui = rng.randrange(len(USERS)) if user is None else user
    u = USERS[ui]
    # U0 은 10-25 에 T0→T1 이동(요청 시점 귀속이 raw 와 같아야 한다). U5 는 T2.
    t = (TEAMS[1] if at.astimezone(Z).date() >= date(2026, 10, 25) else TEAMS[0]) if ui == 0 \
        else TEAMS[2] if ui == 5 else TEAMS[ui % 2]
    if team is not None:
        t = team
    ws = rng.choice([0, 0, 0, 1, 2])
    tracked = rng.random() < 0.6
    return {
        "request_id": f"hp-{uuid.uuid4()}", "user_id": u, "team_id": t,
        "model_alias": rng.choice(MODELS) if not late else "hp-late",
        "client": rng.choice(CLIENTS), "status": rng.choice(STATUSES),
        "input_tokens": rng.randrange(0, 5000), "output_tokens": rng.randrange(0, 900),
        "cache_creation_tokens": rng.randrange(0, 300), "cache_read_tokens": rng.randrange(0, 4000),
        "cost_usd": Decimal(rng.randrange(0, 80000)) / Decimal(1_000_000),
        "latency_ms": rng.randrange(100, 9000), "web_search_count": ws,
        "web_search_cost_usd": (Decimal(ws) * Decimal("0.007")) if tracked else None,
        "requested_at": at,
    }


def _dataset(seed: int = 7):
    rng = random.Random(seed)
    rows = []
    d = START
    while d <= TODAY:
        day0 = datetime(d.year, d.month, d.day, tzinfo=Z)
        n = 18 if d != TODAY else 30
        for _ in range(n):
            at = day0 + timedelta(seconds=rng.randrange(0, 86400))
            if d == TODAY and at.astimezone(UTC) >= NOW:
                continue
            rows.append(_row(rng, at))
        # 경계 탐침 — 자정 ±1s, 정시 ±1s(로컬 시간 버킷 경계 = UTC xx:30)
        for at in (day0 + timedelta(seconds=1), day0 - timedelta(seconds=1),
                   day0 + timedelta(hours=7) - timedelta(seconds=1),
                   day0 + timedelta(hours=7)):
            if at.astimezone(UTC) < NOW and at.astimezone(Z).date() >= START:
                rows.append(_row(rng, at))
        d += timedelta(days=1)
    # 오늘의 진행 중인 시간(14시)과 직전 완료 시간 경계
    for m in (0, 1, 19):
        rows.append(_row(rng, datetime(2026, 11, 24, 14, m, tzinfo=Z)))
    rows.append(_row(rng, datetime(2026, 11, 24, 13, 59, 59, tzinfo=Z)))
    return rows, rng


_INSERT = (
    "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,provider,"
    "input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens,cost_usd,latency_ms,"
    "status,requested_at,completed_at,client,web_search_count,web_search_cost_usd) VALUES "
    "($1,$2,$3,$4,$5,'BEDROCK',$6,$7,$8,$9,$10,$11,$12::usage.usage_status,$13,$13,$14,$15,$16)"
)


async def _insert(conn, rows):
    await conn.executemany(_INSERT, [
        (r["request_id"], r["user_id"], r["team_id"], DEPT, r["model_alias"], r["input_tokens"],
         r["output_tokens"], r["cache_creation_tokens"], r["cache_read_tokens"], r["cost_usd"],
         r["latency_ms"], r["status"], r["requested_at"], r["client"], r["web_search_count"],
         r["web_search_cost_usd"]) for r in rows])


async def _build(dsn: str) -> None:
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    da = _worker()
    rows, rng = _dataset()
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'hp-org')", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'hp-d')",
                           DEPT, ORG)
        for i, t in enumerate(TEAMS):
            await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,$3)",
                               t, DEPT, f"HP Team {i}")
        for i, u in enumerate(USERS):
            await conn.execute(
                "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
                "VALUES ($1,$2,$3,$4,'DEVELOPER',$3)",
                u, TEAMS[1] if i == 0 else TEAMS[2] if i == 5 else TEAMS[i % 2],
                f"hp{i}@example.invalid", f"HP User {i}")
        await _insert(conn, rows)
    finally:
        await conn.close()

    # ── 워커 실제 코드로 롤업 생성(REPEATABLE READ — 운영 집계 엔진과 같은 격리 수준)
    engine = create_async_engine(dsn, isolation_level="REPEATABLE READ")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            # 첫 달 1일부터(빈 날짜도 레지스트리 행) — 백필 CLI 기본값과 같다. 그래야 9월 월 누적이 생긴다.
            first = START.replace(day=1)
            days = [first + timedelta(days=i) for i in range((YESTERDAY - first).days)]
            await da.recompute_days(s, TZ, days, today_local=TODAY)  # 어제는 집계하지 않는다
            for d in (YESTERDAY, TODAY):
                for b0, b1 in da.hour_buckets(Z, d):
                    if b1 <= NOW:
                        await da.aggregate_hour(s, TZ, d, b0, b1)
    finally:
        await engine.dispose()

    # ── 신뢰할 수 없는 상태 만들기 + 그 소스 오염
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        # 늦은 행 — LATE_DAY 와 오늘 03시(이미 시간 집계됨). 표시는 batch_flusher 와 같은 SQL.
        late = [_row(rng, datetime(2026, 11, 10, 15, 5, tzinfo=Z), late=True),
                _row(rng, datetime(2026, 11, 24, 3, 40, tzinfo=Z), late=True)]
        await _insert(conn, late)
        marks = []
        for r in late:
            d, hs, _he = da.bucket_of(Z, r["requested_at"])
            marks.append((d, hs))
            if d < TODAY:
                marks.append((d, None))
        await conn.execute(
            "INSERT INTO usage.rollup_dirty_marks (date, hour_start) "
            "SELECT * FROM unnest($1::date[], $2::timestamptz[])",
            [m[0] for m in marks], [m[1] for m in marks])
        # 표시된 오늘 03시 시간 롤업 오염(쓰이면 안 된다)
        await conn.execute("UPDATE usage.hourly_aggregates SET input_tokens = input_tokens + 777 "
                           "WHERE hour_start = $1", marks[-1][1])
        # 실패한 날짜 — 이전 시도의 부분 결과가 남아 있다고 가정(오염) + failed_at
        await conn.execute("UPDATE usage.daily_aggregate_days SET aggregated_at = NULL, "
                           "failed_at = now(), last_error = 'statement timeout' WHERE date = $1",
                           FAILED_DAY)
        await conn.execute("UPDATE usage.daily_aggregates SET output_tokens = output_tokens * 9 "
                           "WHERE date = $1", FAILED_DAY)
        # grain 2(웹 검색 열이 0) 날짜
        await conn.execute("UPDATE usage.daily_aggregate_days SET grain_version = 2 WHERE date=$1",
                           GRAIN2_DAY)
        await conn.execute("UPDATE usage.daily_aggregates SET web_search_count = 0, "
                           "web_search_cost_usd = NULL WHERE date = $1", GRAIN2_DAY)
        # 다른 tz
        await conn.execute("UPDATE usage.daily_aggregate_days SET tz = 'Asia/Seoul' WHERE date=$1",
                           TZ_DAY)
        await conn.execute("UPDATE usage.daily_aggregates SET cache_read_tokens = 1 WHERE date=$1",
                           TZ_DAY)
        # 레거시 dirty 플래그
        await conn.execute("UPDATE usage.daily_aggregate_days SET dirty = true WHERE date=$1",
                           DIRTY_DAY)
        await conn.execute("UPDATE usage.daily_aggregates SET total_cost_usd = 99 WHERE date=$1",
                           DIRTY_DAY)
        # 늦은 날짜의 일 롤업 행도 오염(표시가 있으니 쓰이면 안 된다)
        await conn.execute("UPDATE usage.daily_aggregates SET call_count = call_count + 5 "
                           "WHERE date=$1", LATE_DAY)
        # 9월 월 누적 stale — 재구성 뒤 9-28 이 다시 집계됨(aggregated_at > built_at)
        await conn.execute("UPDATE usage.daily_aggregate_days SET aggregated_at = now() + "
                           "interval '1 hour' WHERE date = $1", STALE_MONTH_DAY)
        await conn.execute("UPDATE usage.monthly_aggregates SET input_tokens = input_tokens * 3 "
                           "WHERE period = '2026-09-01'")
        # 10월·11월 월 누적은 GRAIN2/TZ/DIRTY/… 날짜 때문에 쓰이지 않아야 한다 — 오염
        await conn.execute("UPDATE usage.monthly_aggregates SET latency_ms_sum = latency_ms_sum + 1 "
                           "WHERE period IN ('2026-10-01','2026-11-01')")
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
            except Exception as e:  # noqa: BLE001 — pgvector 없는 이미지·롤 생성 등
                print(f"  [init] {f.name}: {type(e).__name__}: {e}")
    finally:
        await conn.close()
    await _build(dsn)
    yield dsn
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')


@pytest.fixture
async def session(owned_dsn, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services import usage_rollup as ur
    from app.services import user_model_usage as umu

    monkeypatch.setattr(ur, "reporting_timezone", lambda: TZ)
    monkeypatch.setattr(ur, "reporting_today", lambda tz=None: TODAY)
    monkeypatch.setattr(umu, "reporting_timezone", lambda: TZ)
    monkeypatch.setattr(umu, "_today", lambda tz: TODAY)
    monkeypatch.setattr("app.core.usage_filters.reporting_timezone", lambda: TZ)
    engine = create_async_engine(owned_dsn)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


# ── raw 대조군 ───────────────────────────────────────────────────────────────

_MEASURES = ("i", "o", "cw", "cr", "c", "n", "rq", "lat", "wq", "wc", "wu")

_RAW_GRAIN = """
SELECT {day} user_id::text, team_id::text, model_alias, COALESCE(client,'other') AS client,
       status::text AS status,
       sum(input_tokens) i, sum(output_tokens) o, sum(cache_creation_tokens) cw,
       sum(cache_read_tokens) cr, sum(cost_usd) c, count(*) n,
       count(*) FILTER (WHERE input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens>0
                        OR cost_usd > 0) rq,
       sum(latency_ms) lat, sum(web_search_count) wq, sum(web_search_cost_usd) wc,
       COALESCE(sum(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL), 0) wu
FROM usage.usage_logs
WHERE requested_at >= :s AND requested_at < :e {extra}
GROUP BY {gb}
"""

_HYB_GRAIN = """
SELECT {day} user_id::text, team_id::text, model_alias, client, status::text AS status,
       sum(i) i, sum(o) o, sum(cw) cw, sum(cr) cr, sum(c) c, sum(n) n, sum(rq) rq, sum(lat) lat,
       sum(wq) wq, sum(wc) wc, sum(wu) wu
FROM x GROUP BY {gb}
"""


def _norm(rows, by_day: bool):
    out = {}
    for r in rows:
        m = r._mapping
        key = ((str(m["day"]),) if by_day else ()) + (
            m["user_id"], m["team_id"], m["model_alias"], m["client"], m["status"])
        vals = []
        for k in _MEASURES:
            v = m[k]
            vals.append(None if v is None else Decimal(str(v)).normalize())
        out[key] = tuple(vals)
    return out


def _bounds(s: date, e: date):
    lo = datetime(s.year, s.month, s.day, tzinfo=Z).astimezone(UTC)
    e1 = e + timedelta(days=1)
    hi = datetime(e1.year, e1.month, e1.day, tzinfo=Z).astimezone(UTC)
    return lo, hi


async def _raw(session, sc):
    from sqlalchemy import text

    lo, hi = _bounds(sc.start, min(sc.end, TODAY))
    extra, p = "", {"s": lo, "e": hi, "tz": TZ}
    if sc.teams is not None:
        extra += " AND team_id = ANY(CAST(:teams AS uuid[]))"
        p["teams"] = [str(t) for t in sc.teams]
    if sc.users is not None:
        extra += " AND user_id = ANY(CAST(:users AS uuid[]))"
        p["users"] = [str(u) for u in sc.users]
    if sc.client is not None:
        extra += " AND COALESCE(client,'other') = :client"
        p["client"] = sc.client
    if sc.success_only:
        extra += " AND status = 'SUCCESS'"
    day = "(requested_at AT TIME ZONE :tz)::date AS day," if sc.by_day else ""
    gb = "1,2,3,4,5,6" if sc.by_day else "1,2,3,4,5"
    rows = (await session.execute(text(_RAW_GRAIN.format(day=day, extra=extra, gb=gb)), p)).all()
    return _norm(rows, sc.by_day)


async def _hyb(session, sc):
    from app.services import usage_rollup as ur

    day = "day," if sc.by_day else ""
    gb = "1,2,3,4,5,6" if sc.by_day else "1,2,3,4,5"
    rows = (await ur.execute(session, sc, _HYB_GRAIN.format(day=day, gb=gb))).all()
    return _norm(rows, sc.by_day)


def _scope(start, end, *, teams=None, users=None, client=None, success_only=False, by_day=False):
    from app.services import usage_rollup as ur

    return ur.RollupScope(start=start, end=end,
                          teams=tuple(teams) if teams is not None else None,
                          users=tuple(users) if users is not None else None,
                          client=client, success_only=success_only, by_day=by_day,
                          today=TODAY, tz=TZ, now=NOW)


def _random_scopes(n: int, seed: int = 11):
    rng = random.Random(seed)
    out = []
    months = [(date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31)),
              (date(2026, 11, 1), date(2026, 11, 30))]
    for _ in range(n):
        kind = rng.random()
        if kind < 0.35:
            s, e = rng.choice(months)
        elif kind < 0.45:
            # 최대 길이 근처(≤366일) — 데이터 이전 빈 날짜가 대부분
            e = TODAY - timedelta(days=rng.randrange(0, 5))
            s = e - timedelta(days=rng.randrange(300, 366))
        else:
            s = START + timedelta(days=rng.randrange(0, 60))
            e = s + timedelta(days=rng.randrange(0, 45))
        teams = rng.choice([None, None, [rng.choice(TEAMS)], rng.sample(TEAMS, 2)])
        users = rng.choice([None, None, None, [rng.choice(USERS)]])
        client = rng.choice([None, None, "claude-code", "cowork", "codex", "other"])
        out.append(_scope(s, e, teams=teams, users=users, client=client,
                          success_only=rng.random() < 0.4, by_day=rng.random() < 0.5))
    return out


# ── 속성: x == raw (입도 단위) ──────────────────────────────────────────────


@pytest.mark.parametrize("sc", _random_scopes(60), ids=lambda sc: sc.cache_part()[:60])
async def test_hybrid_grain_equals_raw_random(session, sc):
    assert await _hyb(session, sc) == await _raw(session, sc)


@pytest.mark.parametrize("start,end", [
    (date(2026, 10, 31), date(2026, 11, 1)),     # 월 경계(IST)
    (date(2026, 11, 1), date(2026, 11, 30)),     # 이번 달 전체(MTD + 어제 시간 다리 + 오늘)
    (date(2026, 10, 1), date(2026, 10, 31)),     # GRAIN2/TZ 날짜가 낀 달
    (date(2026, 9, 1), date(2026, 9, 30)),       # 월 누적 stale → 일별
    (TODAY, TODAY),                              # 오늘만 — 완료 시간 롤업 + 14시 raw
    (YESTERDAY, YESTERDAY),                      # 야간 잡 전 — 시간 롤업 다리
    (LATE_DAY, LATE_DAY),                        # 늦은 행 날짜 — 그 하루만 raw
    (date(2025, 11, 25), TODAY),                 # 366일
])
@pytest.mark.parametrize("by_day", [False, True])
async def test_hybrid_grain_equals_raw_boundaries(session, start, end, by_day):
    sc = _scope(start, end, by_day=by_day)
    raw = await _raw(session, sc)
    assert raw, "대조군이 비었다"
    assert await _hyb(session, sc) == raw


async def test_late_rows_are_counted(session):
    """늦은 행(모델 hp-late) 2건이 하이브리드 결과에 정확히 들어간다(표시 → raw/재집계)."""
    sc = _scope(date(2026, 11, 1), TODAY)
    got = await _hyb(session, sc)
    assert sum(v[5] for k, v in got.items() if k[2] == "hp-late") == 2


# ── 라우팅·메타 ─────────────────────────────────────────────────────────────


async def _classes(session, sc):
    from sqlalchemy import text

    from app.services import usage_rollup as ur

    sql = ("WITH " + ur._class_ctes(sc.by_day).lstrip()
           + "\nSELECT (SELECT array_agg(d ORDER BY d) FROM r_src WHERE kind='m') m,"
             " (SELECT array_agg(d ORDER BY d) FROM r_src WHERE kind='d') dd,"
             " (SELECT array_agg(DISTINCT d ORDER BY d) FROM r_bk WHERE NOT good) rawd,"
             " (SELECT count(*) FROM r_bk WHERE NOT good AND d = CAST(:r_today AS date)) live,"
             " (SELECT count(*) FROM r_bk WHERE NOT good) raw_buckets")
    return (await session.execute(text(sql), ur.bind(sc))).one()


async def test_routing_and_delayed_days(session):
    from app.services import usage_rollup as ur

    sc = _scope(date(2026, 9, 1), TODAY)
    c = await _classes(session, sc)
    # 9월(stale) → 일별, 10·11월 → 월 누적 불가(신뢰 불가 날짜) → 일별, 신뢰 불가 날짜 → 시간/raw
    assert not c.m
    raw_days = set(c.rawd or [])
    assert raw_days == UNTRUSTED | {TODAY}, raw_days
    # 오늘: 완료된 0~13시는 시간 롤업(03시는 표시돼 raw), 14시 진행 중 raw
    assert c.live == 2
    # 신뢰 불가 과거 날짜 = 그 하루의 24 버킷만(월 전체 raw 스캔 없음)
    assert c.raw_buckets == 24 * len(UNTRUSTED) + 2
    meta = await ur.fetch_meta(session, sc)
    assert set(meta.delayed_days) == UNTRUSTED
    assert meta.data_as_of is not None
    # 어제는 시간 롤업으로 다리(raw 아님, 지연 아님)
    assert YESTERDAY not in raw_days


async def test_month_layer_used_when_fully_trusted(session, owned_dsn):
    """신뢰 가능한 달은 월 누적 — 깨끗한 완결 달이 없으므로 9월 stale 을 잠시 풀어 라우팅을 확인."""
    import asyncpg

    sc = _scope(date(2026, 9, 1), date(2026, 9, 30))
    c = await _classes(session, sc)
    assert not c.m  # stale
    # 9-28 의 aggregated_at 을 되돌리면(재구성 이후 재집계 없음) 9월은 월 누적이어야 한다 —
    # 그러나 월 누적은 오염시켜 두었으므로 값 비교는 하지 않고 라우팅만 본다(아래에서 원복).
    conn = await asyncpg.connect(_asyncpg(owned_dsn))
    try:
        await conn.execute("UPDATE usage.daily_aggregate_days SET aggregated_at = aggregated_at - "
                           "interval '2 hours' WHERE date = $1", STALE_MONTH_DAY)
        # 첫 사용량(09-25) 이전 날짜는 분류에서 빠진다 — 월 누적이 데이터 있는 6일을 덮는다.
        c2 = await _classes(session, sc)
        assert c2.m == [START + timedelta(days=i) for i in range(6)] and not c2.rawd
        # by_day 는 월 누적을 쓰지 않는다(일 버킷이 필요)
        c3 = await _classes(session, _scope(date(2026, 9, 1), date(2026, 9, 30), by_day=True))
        assert not c3.m and len(c3.dd) == 6
    finally:
        await conn.execute("UPDATE usage.daily_aggregate_days SET aggregated_at = now() + "
                           "interval '1 hour' WHERE date = $1", STALE_MONTH_DAY)
        await conn.close()


# ── 엔드포인트 수준 합계 ─────────────────────────────────────────────────────


async def _raw_scalar(session, sql, **p):
    from sqlalchemy import text

    return (await session.execute(text(sql), p)).one()


async def test_totals_client_share_top_users_ws_daily_equal_raw(session):
    from sqlalchemy import text

    from app.services import usage_reads as reads

    for teams in (None, [TEAMS[0]], TEAMS[1:]):
        for client in (None, "cowork", "other"):
            sc = _scope(date(2026, 11, 1), date(2026, 11, 30), teams=teams, client=client)
            lo, hi = _bounds(sc.start, TODAY)
            f, p = "", {"s": lo, "e": hi}
            if teams:
                f += " AND team_id = ANY(CAST(:t AS uuid[]))"
                p["t"] = [str(t) for t in teams]
            if client:
                f += " AND COALESCE(client,'other') = :c"
                p["c"] = client
            want = await _raw_scalar(session, f"""
                SELECT count(*) FILTER (WHERE status='SUCCESS') ok,
                       count(*) FILTER (WHERE status IN ('ERROR','TIMEOUT')) err,
                       COALESCE(sum(cost_usd) FILTER (WHERE status='SUCCESS'),0) cost,
                       count(DISTINCT user_id) FILTER (WHERE status='SUCCESS') au,
                       COALESCE(sum(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens)
                                FILTER (WHERE status='SUCCESS'),0) tok,
                       COALESCE(sum(web_search_count),0) wq, sum(web_search_cost_usd) wc,
                       COALESCE(sum(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL),0) wu
                FROM usage.usage_logs WHERE requested_at >= :s AND requested_at < :e {f}""", **p)
            t = await reads.totals(session, sc)
            assert (t.total_requests, t.error_requests, t.total_cost, t.active_users,
                    t.total_tokens, t.ws_queries, t.ws_cost, t.ws_untracked) == tuple(want)

            day_sc = _scope(sc.start, sc.end, teams=teams, client=client, by_day=True)
            want_daily = (await session.execute(text(f"""
                SELECT (requested_at AT TIME ZONE 'Asia/Kolkata')::date d, sum(web_search_count) q
                FROM usage.usage_logs WHERE requested_at >= :s AND requested_at < :e
                  AND web_search_count > 0 {f} GROUP BY 1 ORDER BY 1"""), p)).all()
            got_daily = await reads.ws_daily(session, day_sc)
            assert [(r.day, int(r.q)) for r in got_daily] == [(r.d, int(r.q)) for r in want_daily]

        sc = _scope(date(2026, 11, 1), date(2026, 11, 30), teams=teams)
        lo, hi = _bounds(sc.start, TODAY)
        f, p = "", {"s": lo, "e": hi}
        if teams:
            f += " AND l.team_id = ANY(CAST(:t AS uuid[]))"
            p["t"] = [str(t) for t in teams]
        want_cs = (await session.execute(text(f"""
            SELECT COALESCE(client,'other') client,
                   COALESCE(sum(cost_usd) FILTER (WHERE status='SUCCESS'),0) cost,
                   count(*) FILTER (WHERE status='SUCCESS') n, COALESCE(sum(web_search_count),0) wq
            FROM usage.usage_logs l WHERE requested_at >= :s AND requested_at < :e {f}
            GROUP BY 1 ORDER BY sum(cost_usd) FILTER (WHERE status='SUCCESS') DESC, 1"""), p)).all()
        got_cs = await reads.client_share(session, sc)
        assert [(r.client, r.cost_usd, r.call_count, r.ws_queries) for r in got_cs] == \
            [tuple(r) for r in want_cs]

        want_top = (await session.execute(text(f"""
            SELECT l.user_id, sum(cost_usd) c, count(*) n FROM usage.usage_logs l
            WHERE requested_at >= :s AND requested_at < :e AND status='SUCCESS' {f}
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3"""), p)).all()
        got_top = await reads.top_users(
            session, _scope(sc.start, sc.end, teams=teams, success_only=True), 3)
        assert [(r.user_id, r.cost_usd, r.call_count) for r in got_top] == \
            [tuple(r) for r in want_top]


async def test_analytics_overview_and_model_detail_equal_raw(session):
    from sqlalchemy import text

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.services.analytics_service import AnalyticsService

    admin = CurrentUser(user_id=uuid.uuid4(), email="a@x", role=UserRole.ADMIN, team_id=None)
    svc = AnalyticsService()
    for kw in ({"period": "2026-11"}, {"period": "2026-10"},
               {"period": "2026-11", "start_date": "2026-10-28", "end_date": "2026-11-05"}):
        res = await svc.get_analytics(session, actor=admin, group_by="user", **kw)
        s = date.fromisoformat(kw.get("start_date", kw["period"] + "-01"))
        e = (date.fromisoformat(kw["end_date"]) if "end_date" in kw
             else (date(2026, 11, 30) if kw["period"] == "2026-11" else date(2026, 10, 31)))
        lo, hi = _bounds(s, min(e, TODAY))
        p = {"s": lo, "e": hi}
        base = "FROM usage.usage_logs WHERE requested_at >= :s AND requested_at < :e AND status='SUCCESS'"
        by_model = (await session.execute(text(
            f"SELECT model_alias, sum(cost_usd) c, count(*) n {base} GROUP BY 1 "
            f"ORDER BY 2 DESC, 1"), p)).all()
        assert [(b.model, b.cost_usd, b.requests) for b in res.by_model] == \
            [(r.model_alias, r.c, r.n) for r in by_model]
        by_team = (await session.execute(text(
            f"SELECT team_id::text, sum(cost_usd) c, count(DISTINCT user_id) u {base} "
            f"GROUP BY 1 ORDER BY 2 DESC, 1"), p)).all()
        assert [(b.team_id, b.cost_usd, b.active_users) for b in res.by_team] == \
            [tuple(r) for r in by_team]
        trends = (await session.execute(text(
            f"SELECT (requested_at AT TIME ZONE 'Asia/Kolkata')::date d, sum(cost_usd) c, "
            f"count(*) n {base} GROUP BY 1 ORDER BY 1"), p)).all()
        assert [(t.date, t.cost_usd, t.requests) for t in res.trends] == \
            [(str(r.d), r.c, r.n) for r in trends]
        tok = (await session.execute(text(
            f"SELECT sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) cr, "
            f"sum(cache_creation_tokens) cw {base}"), p)).one()
        tb = res.token_breakdown
        assert (tb.input_tokens, tb.output_tokens, tb.cache_read_tokens,
                tb.cache_write_tokens) == tuple(tok)
        users = (await session.execute(text(
            f"SELECT user_id::text, sum(cost_usd) c {base} GROUP BY 1 ORDER BY 2 DESC"), p)).all()
        assert {(u.user_id, u.cost_usd) for u in res.by_user} == {tuple(r) for r in users}
        assert set(res.delayed_days) == {d.isoformat() for d in UNTRUSTED if s <= d <= e}

        md = await svc.get_model_cost_detail(session, actor=admin, **kw)
        want = (await session.execute(text(
            f"SELECT model_alias, count(*) n, round(sum(cost_usd), 4) c, sum(input_tokens) i, "
            f"round(avg(latency_ms)) lat {base} GROUP BY 1 ORDER BY sum(cost_usd) DESC, 1"),
            p)).all()
        assert [(m["model_alias"], m["request_count"], m["total_cost_usd"], m["input_tokens"],
                 m["avg_latency_ms"]) for m in md["models"]] == \
            [(r.model_alias, r.n, float(r.c), r.i, int(r.lat)) for r in want]


async def test_user_models_table_equals_raw(session):
    """사용자 × 모델 CSV(같은 헬퍼) == raw — 팀 스코프·앱 필터 포함."""
    import csv
    import io

    from sqlalchemy import text

    from app.services import user_model_usage as umu

    for teams, client in ((None, None), ([TEAMS[0]], None), (None, "other")):
        rng_ = umu.resolve_range("2026-11", None, None)
        content = await umu.export_csv(session, rng_, teams, client)
        got = {(r["user_id"], r["model"]): (int(r["requests"]), int(r["input_tokens"]),
                                           Decimal(r["cost_usd"]))
               for r in csv.DictReader(io.StringIO(content))}
        lo, hi = _bounds(date(2026, 11, 1), TODAY)
        f, p = "", {"s": lo, "e": hi}
        if teams:
            f += " AND team_id = ANY(CAST(:t AS uuid[]))"
            p["t"] = [str(t) for t in teams]
        if client:
            f += " AND COALESCE(client,'other') = :c"
            p["c"] = client
        want = {(r.u, r.m): (r.n, r.i, Decimal(r.c).quantize(Decimal("0.000001")))
                for r in (await session.execute(text(f"""
                    SELECT user_id::text u, model_alias m, count(*) n, sum(input_tokens) i,
                           sum(cost_usd) c FROM usage.usage_logs
                    WHERE requested_at >= :s AND requested_at < :e AND status='SUCCESS' {f}
                    GROUP BY 1,2"""), p)).all()}
        assert got == want
