# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""wave 2 — 분석 읽기 격리: 분석 풀·문장 상한·캐시 single-flight·ROI 잡 플래그·월 목록.

실 PG 의 하이브리드 == raw 는 tests/regression/test_usage_rollup_realpg.py.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import read_cache


@pytest.fixture(autouse=True)
def _clear_local():
    read_cache.local_lru.clear()
    yield
    read_cache.local_lru.clear()


class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.locks: dict[str, str] = {}
        self.gets = 0

    async def get(self, k):
        self.gets += 1
        return self.store.get(k)

    async def setex(self, k, ttl, v):
        self.store[k] = v

    async def set(self, k, v, nx=False, px=None):
        if nx and k in self.locks:
            return False
        self.locks[k] = v
        return True

    async def eval(self, script, n, key, token):
        if self.locks.get(key) == token:
            del self.locks[key]
            return 1
        return 0


# ── single-flight ────────────────────────────────────────────────────────────


async def test_concurrent_same_key_requests_share_one_computation():
    redis = _Redis()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"v": 42}

    tasks = [asyncio.create_task(read_cache.cached(redis, "k", compute, ttl=30))
             for _ in range(20)]
    await started.wait()
    await asyncio.sleep(0.05)
    release.set()
    out = await asyncio.gather(*tasks)
    assert calls == 1
    assert all(o == {"v": 42} for o in out)
    assert json.loads(redis.store["k"]) == {"v": 42}
    assert redis.locks == {}  # 잠금 해제


async def test_failure_propagates_to_all_waiters_and_is_not_cached():
    redis = _Redis()
    started = asyncio.Event()

    async def compute():
        started.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("statement timeout")

    t1 = asyncio.create_task(read_cache.cached(redis, "k", compute, ttl=30))
    await started.wait()
    t2 = asyncio.create_task(read_cache.cached(redis, "k", compute, ttl=30))
    for t in (t1, t2):
        with pytest.raises(RuntimeError):
            await t
    assert "k" not in redis.store and redis.locks == {}
    # 다음 요청은 다시 계산한다(실패를 캐시하지 않는다).
    async def ok():
        return {"ok": 1}
    assert await read_cache.cached(redis, "k", ok, ttl=30) == {"ok": 1}


async def test_other_pod_holding_the_lock_is_waited_for():
    """다른 pod 가 잠금을 쥐고 계산 중이면 결과가 Redis 에 오를 때까지 기다린다(DB 를 또 치지 않는다)."""
    redis = _Redis()
    redis.locks["k:lock"] = "other-pod"
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return {"mine": True}

    async def other_pod_finishes():
        await asyncio.sleep(0.25)
        redis.store["k"] = json.dumps({"theirs": True})

    asyncio.create_task(other_pod_finishes())
    got = await read_cache.cached(redis, "k", compute, ttl=30, wait=3)
    assert got == {"theirs": True} and calls == 0


async def test_lock_wait_times_out_then_computes_locally():
    redis = _Redis()
    redis.locks["k:lock"] = "stuck"

    async def compute():
        return {"mine": True}

    assert await read_cache.cached(redis, "k", compute, ttl=30, wait=0.3) == {"mine": True}


async def test_redis_errors_fail_open():
    class _Broken:
        async def get(self, k):
            raise ConnectionError("down")

        async def setex(self, *a):
            raise ConnectionError("down")

        async def set(self, *a, **k):
            raise ConnectionError("down")

        async def eval(self, *a):
            raise ConnectionError("down")

    async def compute():
        return {"x": 1}

    assert await read_cache.cached(_Broken(), "k", compute, ttl=30) == {"x": 1}
    assert await read_cache.cached(None, "k", compute, ttl=30) == {"x": 1}


async def test_large_values_are_parsed_off_the_event_loop(monkeypatch):
    big = json.dumps({"users": [[str(uuid.uuid4()), "n" * 50] for _ in range(4000)]})
    assert len(big) > read_cache._THREAD_JSON_BYTES
    redis = _Redis()
    redis.store["k"] = big
    used = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        used.append(fn)
        return await real(fn, *a, **k)

    monkeypatch.setattr(read_cache.asyncio, "to_thread", spy)

    async def compute():
        raise AssertionError("must hit cache")

    out = await read_cache.cached(redis, "k", compute, ttl=30, keep_local=True)
    assert len(out["users"]) == 4000
    assert json.loads in used
    # 로컬 LRU — 다음 요청은 Redis 도 안 본다.
    gets = redis.gets
    await read_cache.cached(redis, "k", compute, ttl=30, keep_local=True)
    assert redis.gets == gets


def test_ttl_short_for_ranges_including_today():
    from datetime import date

    s = SimpleNamespace(ANALYTICS_CACHE_TTL_SEC=30, ANALYTICS_CACHE_TTL_PAST_SEC=300)
    assert read_cache.ttl_for(date(2026, 11, 30), date(2026, 11, 24), s) == 30
    assert read_cache.ttl_for(date(2026, 10, 31), date(2026, 11, 24), s) == 300


# ── 분석 풀 · 문장 상한 ──────────────────────────────────────────────────────


def test_analytics_pool_is_separate_and_small():
    from app.core import db

    assert db.analytics_engine is not db.engine
    assert db.analytics_engine.pool is not db.engine.pool
    settings = db.get_settings()
    assert db.analytics_engine.pool.size() == settings.ANALYTICS_DB_POOL_SIZE
    assert settings.ANALYTICS_DB_POOL_SIZE + settings.ANALYTICS_DB_MAX_OVERFLOW <= 10


def test_statement_timeout_is_set_local_not_session_set():
    """RDS Proxy 는 명시 트랜잭션 안의 SET LOCAL 로는 pin 하지 않는다 — 세션 SET·set_config 금지."""
    from app.core import db

    sql = db.statement_timeout_sql()
    assert sql.startswith("SET LOCAL statement_timeout = ")
    assert int(sql.rsplit(" ", 1)[1]) == db.get_settings().ANALYTICS_STATEMENT_TIMEOUT_MS
    import inspect

    src = inspect.getsource(db)
    assert "set_config(" not in src.split("def statement_timeout_sql", 1)[1].split('"""', 2)[2]


def test_after_begin_hook_runs_set_local_on_each_transaction():
    from app.core import db

    conn = MagicMock()
    db._analytics_after_begin(None, None, conn)
    conn.exec_driver_sql.assert_called_once_with(db.statement_timeout_sql())


async def test_analytics_session_does_not_connect_until_first_query():
    """DB 를 쓰지 않는 요청(검증 실패 등)은 분석 풀 커넥션을 잡지 않는다 — after_begin 은 지연 실행."""
    from app.core import db

    gen = db.get_analytics_session()
    session = await gen.__anext__()
    assert not session.in_transaction()
    assert db.analytics_engine.pool.checkedout() == 0
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


def test_auth_exchange_stays_on_the_default_pool_and_analytics_moved_off_it():
    """/v1/auth/exchange 는 기본 풀, 분석 읽기 엔드포인트는 전부 분석 풀."""
    import inspect

    from app.routers import analytics, auth_oidc, dashboard, productivity, users, web_search_quota

    assert "get_analytics_session" not in inspect.getsource(auth_oidc)
    for fn in (dashboard.dashboard_summary, dashboard.dashboard_kpi, dashboard.model_share,
               dashboard.client_share, dashboard.top_users, dashboard.top_teams,
               dashboard.dashboard_web_search, dashboard.dashboard_periods,
               analytics.get_analytics, analytics.get_model_cost_analytics,
               analytics.get_user_models, analytics.get_user_model_detail,
               analytics.export_analytics, analytics.get_usage_by_user,
               productivity.get_productivity_analytics, users.get_users_activity,
               web_search_quota.get_overview, web_search_quota.get_user_effective,
               web_search_quota.get_team_effective):
        assert "Depends(get_analytics_session)" in inspect.getsource(fn), fn.__name__


# ── ROI 잡 플래그 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [False, True])
def test_roi_job_scheduled_only_when_enabled(monkeypatch, enabled):
    from app.scheduler import main as sched

    settings = sched.get_settings().model_copy(update={"ROI_AGGREGATION_ENABLED": enabled})
    monkeypatch.setattr(sched, "get_settings", lambda: settings)
    added: list[str] = []

    class _Sched:
        def __init__(self, *a, **k):
            pass

        def add_job(self, fn, trigger, id, **kw):  # noqa: A002
            added.append(id)

        def start(self):
            raise SystemExit  # main() 루프에 들어가지 않고 빠져나온다

        def shutdown(self, *a, **k):
            pass

    monkeypatch.setattr(sched, "AsyncIOScheduler", _Sched)
    with pytest.raises(SystemExit):
        sched.main()
    assert ("roi_aggregation" in added) is enabled
    assert "key_expiry" in added


def test_roi_flag_defaults_off():
    from app.core.config import Settings

    assert Settings.model_fields["ROI_AGGREGATION_ENABLED"].default is False


# ── 월 목록 ─────────────────────────────────────────────────────────────────


async def test_periods_from_index_endpoints_not_full_scan(monkeypatch):
    from app.routers import dashboard as dash

    monkeypatch.setattr(dash, "reporting_timezone", lambda: "Asia/Kolkata")
    row = SimpleNamespace(lo=datetime(2026, 8, 31, 19, 0, tzinfo=UTC),   # 09-01 00:30 IST
                          hi=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    res = MagicMock()
    res.one.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=res)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    out = await dash.dashboard_periods(req, None, session)
    assert out == {"periods": ["2026-11", "2026-10", "2026-09"]}
    sql = str(session.execute.await_args.args[0])
    assert "min(requested_at)" in sql and "max(requested_at)" in sql
    assert "DISTINCT" not in sql.upper() and "to_char" not in sql


async def test_periods_empty_table():
    from app.routers import dashboard as dash

    res = MagicMock()
    res.one.return_value = SimpleNamespace(lo=None, hi=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=res)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    assert await dash.dashboard_periods(req, None, session) == {"periods": []}


# ── 범위 가드 ───────────────────────────────────────────────────────────────


def test_rollup_scope_refuses_empty_team_set():
    from datetime import date

    from app.services import usage_rollup as ur

    with pytest.raises(ValueError):
        ur.RollupScope(start=date(2026, 11, 1), end=date(2026, 11, 2), teams=())


def test_hour_buckets_match_worker_definition():
    """admin-api 와 워커의 시간 버킷 정의가 같아야 한다(로컬 자정 + k 시간)."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from app.services import usage_rollup as ur

    for tz in ("Asia/Kolkata", "America/New_York", "UTC"):
        for d in (date(2026, 3, 8), date(2026, 11, 1), date(2026, 11, 24)):
            b = ur.hour_buckets(ZoneInfo(tz), d)
            assert b[0][0] == datetime.combine(d, datetime.min.time(),
                                               tzinfo=ZoneInfo(tz)).astimezone(UTC)
            assert all(e - s <= __import__("datetime").timedelta(hours=1) for s, e in b)
            assert all(b[i][1] == b[i + 1][0] for i in range(len(b) - 1))
