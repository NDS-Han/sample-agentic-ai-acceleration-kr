# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""사용자 × 모델 표 — 하이브리드(월 누적 + 일별 롤업 + raw) == raw, 실 PostgreSQL.

PROOF_DSN 이 있을 때만 돈다. 자기 소유 DB(<dbname>_umproof)를 만들고 init SQL 을 적용한다.

검증 축:
  * 하이브리드 결과 == usage_logs 직접 GROUP BY (SUCCESS·리포팅 tz 일자·앱·팀) — 여러 구간
  * 신뢰할 수 없는 소스는 쓰지 않는다: dirty 날짜·레지스트리 없는 날짜·tz 다른 날짜·
    월 재구성 뒤 재집계된 날짜가 낀 달 — 이 소스들의 행을 일부러 **오염**시켜 두고도 같아야 한다
  * 긴 custom 구간은 통째 포함된 달을 월 누적에서 읽는다(src 분류로 확인)
  * America/New_York DST(11-01 25시간) 날짜 경계
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_umproof"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"
TZ = "America/New_York"
TODAY = date(2026, 11, 24)

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

ORG = uuid.UUID("b4444444-4444-4444-4444-444444444444")
DEPT = uuid.UUID("b3333333-3333-3333-3333-333333333333")
T1 = uuid.UUID("b2222222-2222-2222-2222-222222222221")
T2 = uuid.UUID("b2222222-2222-2222-2222-222222222222")
USERS = [uuid.UUID(f"b1111111-1111-1111-1111-11111111111{i}") for i in range(4)]
MODELS = [f"um-m{i}" for i in range(7)]


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
    """2026-09-01 ~ 오늘 정오(뉴욕). 사용자·모델·앱·상태·팀(U0 는 10-20 에 T1→T2 이동)이 섞인다."""
    tz = ZoneInfo(TZ)
    out = []
    k = 0
    day = date(2026, 9, 1)
    clients = ["claude-code", "cowork", None, "codex"]
    statuses = ["SUCCESS"] * 6 + ["ERROR", "TIMEOUT"]
    while day <= TODAY:
        for h in (1, 9, 17, 23):
            if day == TODAY and h > 12:
                continue
            for ui, uid in enumerate(USERS):
                at = datetime(day.year, day.month, day.day, h, 30, tzinfo=tz)
                team = T2 if (ui == 0 and day >= date(2026, 10, 20)) or ui == 3 else T1
                # 모델 사용량을 비대칭으로 — 상위 5 와 Other 가 분명히 갈리게.
                model = MODELS[(k * (ui + 1)) % 7 if k % 3 else ui % 2]
                out.append((at.astimezone(UTC), uid, team, model, clients[(k + ui) % 4],
                            statuses[k % 8], 100 + k % 50, 20 + k % 13, k % 7, k % 11,
                            Decimal(f"0.{k % 97:03d}"), 100 + k % 400))
                k += 1
        day += timedelta(days=1)
    # DST 경계 탐침: 10-31 23:30 EDT(=11-01T03:30Z) 와 11-01 23:30 EST(=11-02T04:30Z)
    for at, tok in ((datetime(2026, 11, 1, 3, 30, tzinfo=UTC), 7_000_000),
                    (datetime(2026, 11, 2, 4, 30, tzinfo=UTC), 9_000_000)):
        out.append((at, USERS[1], T1, MODELS[0], "claude-code", "SUCCESS", tok, 0, 0, 0,
                    Decimal("1"), 10))
    return out


_DAILY_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias, client, status, input_tokens, output_tokens,
   cache_creation_tokens, cache_read_tokens, total_tokens, total_cost_usd, request_count,
   call_count, latency_ms_sum)
SELECT (requested_at AT TIME ZONE $1)::date, user_id, team_id, dept_id, model_alias,
       COALESCE(client,'other'), status, sum(input_tokens), sum(output_tokens),
       sum(cache_creation_tokens), sum(cache_read_tokens),
       sum(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens), sum(cost_usd),
       count(*), count(*), sum(latency_ms)
FROM usage.usage_logs WHERE (requested_at AT TIME ZONE $1)::date < $2
GROUP BY 1,2,3,4,5,6,7
"""

_MONTH_SQL = """
INSERT INTO usage.monthly_aggregates
  (period, user_id, team_id, dept_id, model_alias, client, status, input_tokens, output_tokens,
   cache_creation_tokens, cache_read_tokens, total_tokens, total_cost_usd, call_count,
   request_count, latency_ms_sum)
SELECT $1::date, user_id, team_id, dept_id, model_alias, client, status, sum(input_tokens),
       sum(output_tokens), sum(cache_creation_tokens), sum(cache_read_tokens), sum(total_tokens),
       sum(total_cost_usd), sum(call_count), sum(request_count), sum(latency_ms_sum)
FROM usage.daily_aggregates WHERE date BETWEEN $1 AND $2 GROUP BY 2,3,4,5,6,7
"""


async def _seed(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        await conn.execute("INSERT INTO auth.organizations (id,name) VALUES ($1,'um-org')", ORG)
        await conn.execute("INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'um-d')",
                           DEPT, ORG)
        for t, n in ((T1, "Alpha"), (T2, "Beta")):
            await conn.execute("INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,$3)",
                               t, DEPT, n)
        for i, u in enumerate(USERS):
            await conn.execute(
                "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
                "VALUES ($1,$2,$3,$4,'DEVELOPER',$3)",
                u, T2 if i in (0, 3) else T1, f"um{i}@example.invalid", f"User {i}")
        await conn.executemany(
            "INSERT INTO usage.usage_logs (request_id,user_id,team_id,dept_id,model_alias,"
            "provider,input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens,"
            "cost_usd,latency_ms,status,requested_at,completed_at,client) VALUES "
            "($1,$2,$3,$4,$5,'BEDROCK',$6,$7,$8,$9,$10,$11,$12::usage.usage_status,$13,$13,$14)",
            [(str(uuid.uuid4()), r[1], r[2], DEPT, r[3], r[6], r[7], r[8], r[9], r[10], r[11],
              r[5], r[0], r[4]) for r in _rows()])
        # 일별 롤업 + 레지스트리(오늘 이전 전부 clean)
        await conn.execute(_DAILY_SQL, TZ, TODAY)
        await conn.execute(
            "INSERT INTO usage.daily_aggregate_days (date,tz,grain_version,dirty,aggregated_at) "
            "SELECT g::date, $1, 3, false, now() - interval '2 hours' "
            "FROM generate_series('2026-09-01'::date, $2::date - 1, interval '1 day') g",
            TZ, TODAY)
        # 월 누적: 9월(이후 9-15 가 재집계돼 stale), 10월(정상), 11월 MTD(11-10 dirty 로 무효)
        for p, through in ((date(2026, 9, 1), date(2026, 9, 30)),
                           (date(2026, 10, 1), date(2026, 10, 31)),
                           (date(2026, 11, 1), date(2026, 11, 23))):
            await conn.execute(_MONTH_SQL, p, through)
            await conn.execute(
                "INSERT INTO usage.monthly_aggregate_months (period,tz,covered_through,built_at,"
                "grain_version) VALUES ($1,$2,$3,now()-interval '1 hour',3)",
                p, TZ, through)
        # ── 신뢰할 수 없는 소스를 오염시킨다 — 하이브리드가 이것들을 읽으면 raw 와 달라진다.
        # 11-10: dirty (늦은 로그) — 일별 행 오염
        await conn.execute("UPDATE usage.daily_aggregate_days SET dirty=true WHERE date='2026-11-10'")
        await conn.execute("UPDATE usage.daily_aggregates SET input_tokens=input_tokens*100 "
                           "WHERE date='2026-11-10'")
        # 11-15: 레지스트리 행 없음(백필 전)
        await conn.execute("DELETE FROM usage.daily_aggregate_days WHERE date='2026-11-15'")
        await conn.execute("UPDATE usage.daily_aggregates SET output_tokens=output_tokens*100 "
                           "WHERE date='2026-11-15'")
        # 11-05: 다른 tz 로 집계됨
        await conn.execute("UPDATE usage.daily_aggregate_days SET tz='Asia/Seoul' "
                           "WHERE date='2026-11-05'")
        await conn.execute("UPDATE usage.daily_aggregates SET cache_read_tokens=cache_read_tokens+9999 "
                           "WHERE date='2026-11-05'")
        # 11월 MTD 월 누적 오염(11-10 dirty 라 쓰이면 안 된다)
        await conn.execute("UPDATE usage.monthly_aggregates SET input_tokens=input_tokens*1000 "
                           "WHERE period='2026-11-01'")
        # 9월: 월 재구성 뒤 9-15 가 다시 집계됨 → 9월 월 누적 stale(오염) → 일별로 내려가야
        await conn.execute("UPDATE usage.daily_aggregate_days SET aggregated_at=now() "
                           "WHERE date='2026-09-15'")
        await conn.execute("UPDATE usage.monthly_aggregates SET output_tokens=output_tokens*1000 "
                           "WHERE period='2026-09-01'")
        # 오늘(11-24)의 롤업이 있어도 쓰이면 안 된다 — 오늘은 항상 raw.
        await conn.execute(
            "INSERT INTO usage.daily_aggregate_days (date,tz,grain_version,dirty,aggregated_at) "
            "VALUES ($1,$2,3,false,now())", TODAY, TZ)
        await conn.execute(
            "INSERT INTO usage.daily_aggregates (date,user_id,team_id,dept_id,model_alias,client,"
            "status,input_tokens,total_tokens) VALUES ($1,$2,$3,$4,'um-m0','claude-code',"
            "'SUCCESS',123456789,123456789)", TODAY, USERS[0], T1, DEPT)
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
async def session(owned_dsn, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services import user_model_usage as umu

    from app.services import usage_rollup as ur

    monkeypatch.setattr(umu, "reporting_timezone", lambda: TZ)
    monkeypatch.setattr(umu, "_today", lambda tz: TODAY)
    monkeypatch.setattr(ur, "reporting_timezone", lambda: TZ)
    engine = create_async_engine(owned_dsn)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


_RAW = """
SELECT user_id::text, model_alias, count(*) n, sum(input_tokens) i, sum(output_tokens) o,
       sum(cache_read_tokens) cr, sum(cache_creation_tokens) cw, sum(cost_usd) c,
       sum(latency_ms) l
FROM usage.usage_logs
WHERE status='SUCCESS' AND (requested_at AT TIME ZONE :tz)::date BETWEEN :s AND :e
  {extra}
GROUP BY 1,2
"""


async def _raw(session, s: date, e: date, *, teams=None, client=None):
    from sqlalchemy import text

    extra = ""
    params = {"tz": TZ, "s": s, "e": e}
    if teams:
        extra += " AND team_id = ANY(CAST(:teams AS uuid[]))"
        params["teams"] = [str(t) for t in teams]
    if client:
        extra += " AND COALESCE(client,'other') = :client"
        params["client"] = client
    rows = (await session.execute(text(_RAW.format(extra=extra)), params)).all()
    return {(r.user_id, r.model_alias): (int(r.n), int(r.i), int(r.o), int(r.cr), int(r.cw),
                                         Decimal(r.c).quantize(Decimal("0.000001")))
            for r in rows}


async def _hybrid(session, rng, *, teams=None, client=None):
    import csv
    import io

    from app.services import user_model_usage as umu

    content = await umu.export_csv(session, rng, teams, client)
    out = {}
    for r in csv.DictReader(io.StringIO(content)):
        out[(r["user_id"], r["model"])] = (
            int(r["requests"]), int(r["input_tokens"]), int(r["output_tokens"]),
            int(r["cache_read_tokens"]), int(r["cache_write_tokens"]), Decimal(r["cost_usd"]))
    return out


async def _kinds(session, rng) -> dict[date, str]:
    from sqlalchemy import text

    from app.services import usage_rollup as ur
    from app.services import user_model_usage as umu

    sc = umu._scope(rng, None, None)
    sql = f"WITH {ur._class_ctes(False).lstrip()} SELECT d, kind FROM r_src"
    return {r.d: r.kind for r in (await session.execute(text(sql), ur.bind(sc))).all()}


@pytest.mark.parametrize("period,start,end", [
    ("2026-11", None, None),                      # MTD: 오늘 raw · dirty/미등록/tz 다름 → raw
    ("2026-10", None, None),                      # 10월 = 월 누적
    ("2026-09", None, None),                      # 9월 월 누적 stale → 일별
    (None, "2026-09-01", "2026-11-24"),           # 긴 구간 — 10월은 월 누적
    (None, "2026-10-15", "2026-11-03"),           # 부분 달 + DST 전환
    (None, "2026-11-01", "2026-11-01"),           # 25시간 하루
])
async def test_hybrid_equals_raw(session, period, start, end):
    from app.services import user_model_usage as umu

    rng = umu.resolve_range(period, start, end)
    raw = await _raw(session, rng.start, min(rng.end, TODAY))
    assert raw, "대조군이 비었다"
    assert await _hybrid(session, rng) == raw


async def test_source_routing_uses_month_layer_only_when_trusted(session):
    from app.services import user_model_usage as umu

    kinds = await _kinds(session, umu.resolve_range(None, "2026-09-01", "2026-11-24"))
    assert {kinds[date(2026, 10, d)] for d in range(1, 32)} == {"m"}   # 10월 통째
    assert {kinds[date(2026, 9, d)] for d in range(1, 31)} == {"d"}    # stale 9월 → 일별
    # 신뢰 불가 날짜·오늘 = 'h'(시간 롤업이 있으면 그것, 없으면 그 하루만 시간 버킷 raw)
    assert kinds[date(2026, 11, 10)] == "h"   # dirty
    assert kinds[date(2026, 11, 15)] == "h"   # 레지스트리 없음
    assert kinds[date(2026, 11, 5)] == "h"    # tz 다름
    assert kinds[TODAY] == "h"                # 오늘
    assert kinds[date(2026, 11, 20)] == "d"   # 11월 MTD 는 무효 → 일별
    # 구간이 달 중간에서 끝나면 그 달은 월 누적을 쓰지 않는다.
    part = await _kinds(session, umu.resolve_range(None, "2026-10-01", "2026-10-30"))
    assert set(part.values()) == {"d"}


async def test_dst_day_boundary_new_york(session):
    """11-01(EDT→EST, 25시간): 10-31 23:30 EDT 탐침은 빠지고 11-01 23:30 EST 탐침은 들어온다."""
    from app.services import user_model_usage as umu

    got = await _hybrid(session, umu.resolve_range(None, "2026-11-01", "2026-11-01"))
    m0 = got[(str(USERS[1]), MODELS[0])]
    assert 9_000_000 <= m0[1] < 9_100_000
    oct31 = await _hybrid(session, umu.resolve_range(None, "2026-10-31", "2026-10-31"))
    assert 7_000_000 <= oct31[(str(USERS[1]), MODELS[0])][1] < 7_100_000


async def test_team_scope_and_app_filter_equal_raw(session):
    from app.services import user_model_usage as umu

    rng = umu.resolve_range("2026-10", None, None)   # U0 가 10-20 에 팀 이동
    assert await _hybrid(session, rng, teams=[T1]) == await _raw(
        session, rng.start, rng.end, teams=[T1])
    rng = umu.resolve_range(None, "2026-09-01", "2026-11-24")
    for client in ("other", "claude-code"):
        assert await _hybrid(session, rng, client=client) == await _raw(
            session, rng.start, rng.end, client=client)


async def test_table_top5_other_and_totals_match_raw(session):
    from app.services import user_model_usage as umu

    rng = umu.resolve_range("2026-11", None, None)
    raw = await _raw(session, rng.start, TODAY)
    payload = await umu.fetch_scope_payload(session, rng, None, None)
    resp = umu.page_from_payload(payload, page_size=50)
    by_model: dict[str, int] = {}
    for (_, m), v in raw.items():
        by_model[m] = by_model.get(m, 0) + v[1] + v[2] + v[3] + v[4]
    want_top = sorted(by_model, key=lambda m: (-by_model[m], m))[:5]
    assert [m["model"] for m in resp["top_models"]] == want_top
    assert {m["model"] for m in resp["other_models"]} == set(by_model) - set(want_top)
    assert resp["total_users"] == len(USERS)
    for row in resp["rows"]:
        mine = {m: v for (u, m), v in raw.items() if u == row["user_id"]}
        other = [v for m, v in mine.items() if m not in want_top]
        assert row["total"]["total_tokens"] == sum(v[1] + v[2] + v[3] + v[4] for v in mine.values())
        assert row["total"]["requests"] == sum(v[0] for v in mine.values())
        for m in want_top:
            if m in mine:
                assert row["cells"][m]["input_tokens"] == mine[m][1]
            else:
                assert m not in row["cells"]
        assert (row["other"] or {}).get("total_tokens", 0) == sum(
            v[1] + v[2] + v[3] + v[4] for v in other)


async def test_detail_and_scope_visibility(session):
    from app.core.exceptions import NotFoundError
    from app.services import user_model_usage as umu

    rng = umu.resolve_range("2026-10", None, None)
    d = await umu.get_user_detail(session, USERS[1], rng, None, None)
    raw = await _raw(session, rng.start, rng.end)
    mine = {m: v for (u, m), v in raw.items() if u == str(USERS[1])}
    assert {m["model"] for m in d["models"]} == set(mine)
    assert d["total"]["requests"] == sum(v[0] for v in mine.values())
    assert d["models"] == sorted(d["models"], key=lambda m: -m["total_tokens"])
    assert all(m["avg_latency_ms"] for m in d["models"])
    assert d["email"] == "um1@example.invalid" and d["team_name"] == "Alpha"
    # U3 은 T2 소속·T2 에서만 사용 — T1 리더 스코프에서는 보이지 않는다(이름·이메일 미노출).
    with pytest.raises(NotFoundError):
        await umu.get_user_detail(session, USERS[3], rng, [T1], None)
    with pytest.raises(NotFoundError):
        await umu.get_user_detail(session, uuid.uuid4(), rng, None, None)
    # U0 은 T2 로 옮겼지만 10월 초 T1 시절 사용분은 T1 스코프에서 보인다.
    d0 = await umu.get_user_detail(session, USERS[0], rng, [T1], None)
    assert d0["models"] and d0["total"]["requests"] < (
        await umu.get_user_detail(session, USERS[0], rng, None, None))["total"]["requests"]
