# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Wave 1 hot-path isolation: slow/unavailable DB or Redis must not slow or fail proxy requests.

Covers: process-local readiness (A1), local auth caches + deadline + fail-to-cache (A2),
downgrade / web-search degraded handling and background counter rebuild (A5).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.routers import health as health_router
from app.schemas.domain import AuthContext, AuthType, DegradationLevel, Role
from app.services import auth_service as auth_mod
from app.services import hot_path
from app.services import web_search_quota as wsq
from app.services.downgrade_loader import DowngradePolicyLoader, DowngradeRule

# ── fixtures / fakes ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_globals():
    hot_path.DB_BREAKER.reset()
    hot_path.DB_BREAKER._dm = None
    hot_path._HOT_SESSION_FACTORY = None
    auth_mod.configure_auth_local_caches(None)
    wsq._LAST_COUNTER.clear()
    yield
    hot_path.DB_BREAKER.reset()
    hot_path.DB_BREAKER._dm = None
    hot_path._HOT_SESSION_FACTORY = None
    auth_mod.configure_auth_local_caches(None)
    wsq._LAST_COUNTER.clear()


def _session_factory(execute):
    """Session factory whose sessions run ``execute(stmt, params)`` (may sleep/raise)."""

    @asynccontextmanager
    async def sf():
        yield SimpleNamespace(execute=execute)

    return sf


def _slow(seconds: float):
    async def execute(*_a, **_k):
        await asyncio.sleep(seconds)
        raise AssertionError("should have timed out")

    return execute


def _caches(**kw):
    c = auth_mod.AuthLocalCaches(
        ttl=kw.get("ttl", 45), max_stale=kw.get("max_stale", 300), max_entries=100,
        redis_fallback_timeout=kw.get("redis_timeout", 0.2), db_timeout=kw.get("db_timeout", 0.2),
    )
    auth_mod.configure_auth_local_caches(c)
    return c


def _ctx(user_id="u1"):
    return AuthContext(user_id=user_id, team_id="t1", dept_id="", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY, key_id=None, key_prefix="vk-aaaaaaaa",
                       allowed_models=None, allowed_clients=None, sso_subject="s")


TOKEN = "vk-aaaaaaaa0000"
KH = hashlib.sha256(TOKEN.encode()).hexdigest()


def _redis(store: dict | None = None, *, get_exc=None, hang: float = 0.0):
    store = dict(store or {})
    r = MagicMock()

    async def get(key):
        if hang:
            await asyncio.sleep(hang)
        if get_exc is not None:
            raise get_exc
        return store.get(key)

    r.get = AsyncMock(side_effect=get)
    r.delete = AsyncMock()
    r.setex = AsyncMock()
    return r


# ── A1 readiness ─────────────────────────────────────────────────────────────


async def _ready(level, **state):
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=level)
    pool = MagicMock()
    pool.checkedout.return_value = state.pop("checked_out", 0)
    pool.size.return_value = 20
    pool.overflow.return_value = 0
    app.state.db_engine = SimpleNamespace(pool=pool)
    for k, v in state.items():
        setattr(app.state, k, v)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/health/ready")


@pytest.mark.parametrize("level", list(DegradationLevel))
async def test_readiness_ignores_shared_dependencies(level):
    resp = await _ready(level, started=True, shutting_down=False, checked_out=999)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["degradation_level"] == level.value  # still reported
    assert body["pool_saturated"] is True


async def test_readiness_503_while_starting_or_shutting_down():
    assert (await _ready(DegradationLevel.HEALTHY)).status_code == 503
    resp = await _ready(DegradationLevel.HEALTHY, started=True, shutting_down=True)
    assert resp.status_code == 503 and resp.json()["status"] == "shutting_down"


# ── primitives ───────────────────────────────────────────────────────────────


def test_local_cache_fresh_stale_lru():
    now = [0.0]
    c = hot_path.LocalCache(ttl=10, max_stale=100, max_entries=2, clock=lambda: now[0])
    c.put("a", 1)
    now[0] = 5
    assert c.get_fresh("a") == 1
    now[0] = 50
    assert c.get_fresh("a") is None and c.get_stale("a") == 1
    now[0] = 200
    assert c.get_stale("a") is None and len(c) == 0
    c.put("x", 1), c.put("y", 2), c.put("z", 3)
    assert c.get_stale("x") is None and c.get_stale("z") == 3


def test_breaker_cooldown_and_degraded():
    now = [0.0]
    b = hot_path.DbBreaker(cooldown_sec=2, clock=lambda: now[0])
    assert b.available()
    b.trip("t")
    assert not b.available()
    now[0] = 2.1
    assert b.available()
    b.attach(SimpleNamespace(level=DegradationLevel.DB_DEGRADED))
    assert not b.available()


async def test_run_db_times_out_fast_and_trips_breaker():
    t0 = time.monotonic()
    with pytest.raises(hot_path.HotDbUnavailable):
        await hot_path.run_db(lambda s: s.execute(), timeout=0.1,
                              session_factory=_session_factory(_slow(5)))
    assert time.monotonic() - t0 < 1.0
    assert not hot_path.DB_BREAKER.available()
    # while open: skipped immediately, no DB call
    called = AsyncMock()
    with pytest.raises(hot_path.HotDbUnavailable):
        await hot_path.run_db(lambda s: called(), timeout=0.1,
                              session_factory=_session_factory(called))
    called.assert_not_called()


# ── A2 auth ──────────────────────────────────────────────────────────────────


async def test_cache_hit_uses_local_is_active_without_db():
    caches = _caches()
    caches.active.put("u1", True)
    redis = _redis({f"key:cache:vk:{KH}": _ctx().model_dump_json(), f"key:vk:{KH}": "u1"})
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("no DB on the hot path"))
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, db)
    assert ctx.user_id == "u1" and ctx.key_prefix == TOKEN[:11]


async def test_cache_hit_with_slow_db_does_not_wait():
    _caches(db_timeout=0.1)
    hot_path.configure(session_factory=_session_factory(_slow(5)))
    redis = _redis({f"key:cache:vk:{KH}": _ctx().model_dump_json(), f"key:vk:{KH}": "u1"})
    t0 = time.monotonic()
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert ctx.user_id == "u1"
    assert time.monotonic() - t0 < 0.5


async def test_unknown_key_with_slow_db_fails_closed_fast():
    _caches(db_timeout=0.1)
    hot_path.configure(session_factory=_session_factory(_slow(5)))
    redis = _redis({f"key:vk:{KH}": "u1"})  # mapping exists, no AuthContext cache
    t0 = time.monotonic()
    with pytest.raises(auth_mod.AuthBackendUnavailableError, match="not cached locally"):
        await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert time.monotonic() - t0 < 0.5


async def test_known_key_served_from_local_when_db_unavailable():
    caches = _caches()
    caches.ctx.put(KH, _ctx())
    hot_path.DB_BREAKER.trip("test")
    redis = _redis({f"key:vk:{KH}": "u1"})
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert ctx.user_id == "u1" and caches.stale_served == 1


async def test_redis_error_or_hang_served_from_local_cache():
    caches = _caches(redis_timeout=0.1)
    caches.ctx.put(KH, _ctx())
    ctx = await auth_mod.VKAuthStrategy().authenticate(
        f"Bearer {TOKEN}", _redis(get_exc=RedisConnectionError("down")), None)
    assert ctx.user_id == "u1"
    t0 = time.monotonic()
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", _redis(hang=5), None)
    assert ctx.user_id == "u1" and time.monotonic() - t0 < 0.5
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", None, None)
    assert ctx.user_id == "u1"


async def test_redis_down_unknown_key_is_503_not_401():
    _caches()
    with pytest.raises(auth_mod.AuthBackendUnavailableError):
        await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", None, None)
    with pytest.raises(RedisConnectionError):
        await auth_mod.VKAuthStrategy().authenticate(
            f"Bearer {TOKEN}", _redis(get_exc=RedisConnectionError("down")), None)


async def test_revoked_key_is_rejected_and_evicted_locally():
    caches = _caches()
    caches.ctx.put(KH, _ctx())
    redis = _redis({f"key:cache:vk:{KH}": _ctx().model_dump_json()})  # mapping gone = revoked
    with pytest.raises(PermissionError):
        await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert caches.ctx.get_stale(KH) is None


def test_invalidation_message_evicts_users_and_keys():
    caches = _caches()
    caches.ctx.put("h1", _ctx("u1"))
    caches.ctx.put("h2", _ctx("u2"))
    caches.ctx.put("h3", _ctx("u3"))
    caches.active.put("u2", True)
    caches.invalidate({"key_hashes": ["h1"], "user_ids": ["u2"]})
    assert caches.ctx.get_stale("h1") is None and caches.ctx.get_stale("h2") is None
    assert caches.active.get_stale("u2") is None and caches.ctx.get_stale("h3") is not None
    caches.invalidate({"all": True})
    assert len(caches.ctx) == 0


async def test_legacy_behaviour_without_local_caches():
    # not configured → per-request DB is_active as before
    redis = _redis({f"key:cache:vk:{KH}": _ctx().model_dump_json(), f"key:vk:{KH}": "u1"})
    res = MagicMock()
    res.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, db)
    db.execute.assert_awaited_once()


async def test_middleware_both_degraded_serves_known_key():
    from app.middleware.auth import AuthMiddleware

    caches = _caches()
    caches.ctx.put(KH, _ctx())
    seen = {}

    async def inner(scope, receive, send):
        seen["ctx"] = scope["state"]["auth_context"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = AuthMiddleware(inner)
    sent = []

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "path": "/model/x/invoke-with-response-stream",
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
             "state": {"_degradation_manager":
                       SimpleNamespace(level=DegradationLevel.BOTH_DEGRADED),
                       "_redis": _redis(), "_session_factory": None}}
    await mw(scope, AsyncMock(), send)
    assert sent[0]["status"] == 200 and seen["ctx"].user_id == "u1"


# ── A5 downgrade ─────────────────────────────────────────────────────────────


async def test_downgrade_loader_keeps_last_known_rules():
    loader = DowngradePolicyLoader()
    import uuid

    tid = uuid.uuid4()
    rules = [DowngradeRule("a", "b", 80)]
    redis = _redis({f"budget:downgrade:team:{tid}": json.dumps(
        [{"from_alias": "a", "to_alias": "b", "threshold_pct": 80}])})
    assert await loader.get_active_rules(redis, None, tid) == rules
    # Redis now empty and DB unavailable → last known rules, not []
    assert await loader.get_active_rules(_redis(), None, tid) == rules
    assert loader.last_known(tid) == rules


async def test_bounded_lookup_times_out_and_trips_breaker(monkeypatch):
    from app.middleware import downgrade as dmw

    monkeypatch.setattr(dmw, "DB_BREAKER", hot_path.DB_BREAKER)
    sf = _session_factory(_slow(5))

    async def fn(db):
        await db.execute()

    t0 = time.monotonic()
    with pytest.raises(hot_path.HotDbUnavailable):
        await dmw._bounded_lookup(sf, fn, "rules")
    assert time.monotonic() - t0 < 1.0 and not hot_path.DB_BREAKER.available()
    # breaker open → fn(None) without touching the DB
    got = await dmw._bounded_lookup(sf, AsyncMock(return_value="no-db"), "rules")
    assert got == "no-db"


# ── A5 web search ────────────────────────────────────────────────────────────


async def test_policy_loader_slow_db_returns_default_fast_and_negative_caches():
    loader = wsq.PolicyLoader()
    t0 = time.monotonic()
    p = await loader.load(_session_factory(_slow(5)), "u1", "t1", default_cap=5)
    assert time.monotonic() - t0 < 1.5
    assert p.user_limit is None and p.per_request_cap == 5
    hot_path.DB_BREAKER.reset()
    calls = AsyncMock()
    assert await loader.load(_session_factory(calls), "u1", "t1", default_cap=5) == p
    calls.assert_not_called()  # negative-cached


async def test_policy_loader_lru_does_not_clear_everything():
    loader = wsq.PolicyLoader(max_entries=2)
    res = MagicMock()
    res.all.return_value = []
    sf = _session_factory(AsyncMock(return_value=res))
    for u in ("u1", "u2", "u3"):
        await loader.load(sf, u, None, default_cap=5)
    assert len(loader._cache) == 2 and ("u1", "") not in loader._cache


async def test_missing_counter_does_not_block_and_rebuilds_in_background():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sum(*_a, **_k):
        started.set()
        await release.wait()
        r = MagicMock()
        r.scalar.return_value = 7
        return r

    store: dict = {}
    redis = _redis(store)
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=7)
    sf = _session_factory(slow_sum)
    t0 = time.monotonic()
    v = await wsq.ensure_counter(redis, "wsq:user:{u1}:2026-11", sf, wsq._SUM_USER_SQL,
                                 "u1", "2026-11")
    assert v == 0 and time.monotonic() - t0 < 0.2  # did not wait for the SUM
    await asyncio.wait_for(started.wait(), 1)
    # second miss while the rebuild runs: no second rebuild (single-flight)
    await wsq.ensure_counter(redis, "wsq:user:{u1}:2026-11", sf, wsq._SUM_USER_SQL, "u1", "2026-11")
    assert redis.set.await_count == 1
    release.set()
    for _ in range(50):
        if wsq._LAST_COUNTER.get("wsq:user:{u1}:2026-11") == 7:
            break
        await asyncio.sleep(0.01)
    assert wsq._LAST_COUNTER["wsq:user:{u1}:2026-11"] == 7


async def test_rebuild_skipped_when_another_pod_holds_the_lock():
    redis = _redis()
    redis.set = AsyncMock(return_value=None)  # NX failed
    sf = _session_factory(AsyncMock(side_effect=AssertionError("no DB")))
    assert await wsq.rebuild_counter(redis, "k", sf, wsq._SUM_USER_SQL, "u1", "2026-11") is None


async def test_gate_fails_open_fast_on_redis_hang():
    policy_rows = MagicMock()
    policy_rows.all.return_value = [("ORG", 1, None)]
    sf = _session_factory(AsyncMock(return_value=policy_rows))
    wsq.POLICY_LOADER._cache.clear()
    t0 = time.monotonic()
    gate = await wsq.gate_web_search(state={}, redis=_redis(hang=5), session_factory=sf,
                                     auth_context=_ctx(), app_enabled=True, default_cap=5)
    assert gate.enabled and not gate.monthly_limit_reached
    assert time.monotonic() - t0 < 1.5
