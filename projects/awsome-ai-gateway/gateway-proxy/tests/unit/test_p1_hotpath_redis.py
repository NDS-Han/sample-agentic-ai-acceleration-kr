# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""p1-hotpath-redis: fewer Redis round trips, EVALSHA, shared Redis breaker, bounded fallback.

Covers:
- ``redis_client._EvalShaMixin``: EVALSHA first, EVAL once on NOSCRIPT, switchable.
- ``hot_path.run_redis`` / ``REDIS_BREAKER``: which errors open it, open → no call.
- independent reads go out concurrently (one round trip): rate-limit config GETs, VK auth GETs,
  TPM settle window markers, budget config EXISTS pair.
- breaker open → rate-limit config read skipped, router admission skips Redis limits/reservation,
  in-memory fallback uses the last known limits (else configured defaults), cost events spool.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import MaxConnectionsError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.redis_client import _EvalShaMixin, _script_sha
from app.services import hot_path
from app.services.hot_path import REDIS_BREAKER, HotRedisUnavailable, run_redis


class _Barrier:
    """Counts concurrent awaiters; every call waits until ``n`` calls are in flight at once."""

    def __init__(self, n: int, timeout: float = 1.0) -> None:
        self.n = n
        self.timeout = timeout
        self.inflight = 0
        self.max_inflight = 0
        self._ev = asyncio.Event()

    async def wait(self) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self.inflight >= self.n:
            self._ev.set()
        try:
            await asyncio.wait_for(self._ev.wait(), self.timeout)
        finally:
            self.inflight -= 1


# ── EVALSHA ───────────────────────────────────────────────────────────────────


class _ShaFake(_EvalShaMixin, fakeredis.aioredis.FakeRedis):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent: list[str] = []

    async def execute_command(self, *args, **options):
        self.sent.append(str(args[0]).upper())
        return await super().execute_command(*args, **options)


@pytest.mark.asyncio
async def test_evalsha_first_then_eval_on_noscript_then_evalsha():
    r = _ShaFake()
    script = "return redis.call('INCR', KEYS[1])"
    assert await r.eval(script, 1, "k") == 1
    assert r.sent == ["EVALSHA", "EVAL"]  # NOSCRIPT → one EVAL loads it
    r.sent.clear()
    assert await r.eval(script, 1, "k") == 2
    assert r.sent == ["EVALSHA"]  # now cached on the server
    assert (await r.script_exists(_script_sha(script)))[0]


@pytest.mark.asyncio
async def test_evalsha_disabled_sends_eval():
    r = _ShaFake()
    r.evalsha_enabled = False
    assert await r.eval("return 7", 0) == 7
    assert r.sent == ["EVAL"]


@pytest.mark.asyncio
async def test_evalsha_script_error_is_not_retried_as_eval():
    r = _ShaFake()
    script = "return redis.call('NOPE_CMD')"
    await r.script_load(script)
    with pytest.raises(ResponseError):
        await r.eval(script, 0)
    assert r.sent == ["SCRIPT LOAD", "EVALSHA"] or r.sent[-1] == "EVALSHA"
    assert "EVAL" not in r.sent


def test_create_client_classes_keep_isinstance():
    from redis.asyncio import Redis
    from redis.asyncio.cluster import RedisCluster

    from app.redis_client import GatewayRedis, GatewayRedisCluster

    assert issubclass(GatewayRedis, Redis)
    assert issubclass(GatewayRedisCluster, RedisCluster)


# ── shared breaker ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,trips", [
    (RedisConnectionError("down"), True),
    (RedisTimeoutError("slow"), True),
    (OSError("reset"), True),
    (MaxConnectionsError("pool"), False),   # local back-pressure, not an outage
    (ResponseError("script"), False),       # script/data error
    (ValueError("bad json"), False),
])
async def test_run_redis_trips_only_on_infra_errors(exc, trips):
    async def boom():
        raise exc

    with pytest.raises(type(exc)):
        await run_redis(boom, timeout=1.0, what="t")
    assert REDIS_BREAKER.available() is (not trips)


@pytest.mark.asyncio
async def test_run_redis_deadline_and_open_breaker_skips_call():
    calls = 0

    async def hang():
        nonlocal calls
        calls += 1
        await asyncio.sleep(5)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TimeoutError):
        await run_redis(hang, timeout=0.05, what="t")
    assert loop.time() - t0 < 1.0
    assert not REDIS_BREAKER.available()
    with pytest.raises(HotRedisUnavailable):
        await run_redis(hang, timeout=0.05, what="t")
    assert calls == 1  # second call never reached Redis
    assert isinstance(HotRedisUnavailable("x"), RedisConnectionError)


def test_breaker_cooldown_reopens_after_window():
    now = [100.0]
    b = hot_path.DbBreaker(cooldown_sec=2.0, clock=lambda: now[0], name="redis")
    b.trip("x")
    assert not b.available()
    now[0] += 2.01
    assert b.available()


def test_redis_breaker_single_probe_half_open():
    now = [100.0]
    b = hot_path.RedisBreaker(cooldown_sec=2.0, clock=lambda: now[0])
    assert b.available() and not b.is_open()
    b.trip("hang")
    assert not b.available() and b.is_open()
    b.record_success()            # late success of a call from before the trip: ignored
    assert b.is_open()
    now[0] += 2.01
    assert b.available()          # the probe
    assert not b.available()      # everyone else still waits for the probe
    assert b.is_open()
    b.trip("probe failed")        # probe failed → open again for a full cooldown
    now[0] += 1.0
    assert not b.available()
    now[0] += 1.01
    assert b.available()          # next probe
    b.record_success()            # probe answered → closed for everyone
    assert b.available() and b.available() and not b.is_open()


def test_redis_breaker_probe_that_never_reports_frees_the_slot():
    now = [0.0]
    b = hot_path.RedisBreaker(cooldown_sec=2.0, clock=lambda: now[0])
    b.trip("x")
    now[0] = 2.5
    assert b.available()
    now[0] = 4.0
    assert not b.available()
    now[0] = 4.6
    assert b.available()


@pytest.mark.asyncio
async def test_run_redis_success_closes_after_probe():
    b = hot_path.RedisBreaker(cooldown_sec=0.0)
    b.trip("x")

    async def ok():
        return 1

    assert await run_redis(ok, timeout=1.0, breaker=b) == 1
    assert not b.is_open()


def test_budget_and_model_cache_share_the_redis_breaker():
    from app.config import get_settings
    from app.services import budget_degraded, model_cache

    saved = budget_degraded.DEGRADED
    try:
        budget_degraded.configure(get_settings())
        assert budget_degraded.DEGRADED.redis_breaker is REDIS_BREAKER
        cache = model_cache.from_settings(get_settings(), session_factory=None)
        assert cache.redis_breaker is REDIS_BREAKER
    finally:
        budget_degraded.DEGRADED = saved


# ── rate-limit config: one round trip, breaker-aware, last known ─────────────


@pytest.mark.asyncio
async def test_rl_config_gets_are_concurrent():
    from app.services.rate_limit_config_loader import last_known_limits, load_all_scope_limits

    barrier = _Barrier(3)
    vals = {
        "rl:config:USER:u1:m": json.dumps({"rpm": 7, "tpm": 70}),
        "rl:config:TEAM:t1:m": json.dumps({"rpm": 70, "tpm": 700, "agg": True}),
        "rl:config:GLOBAL:NULL:m": json.dumps({"rpm": 700, "tpm": 7000}),
    }

    async def get(key):
        await barrier.wait()  # would time out if the three GETs were sequential
        return vals[key]

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=get)
    res = await load_all_scope_limits(redis=redis, db=None, user_id="u1", team_id="t1",
                                      model_alias="m")
    assert barrier.max_inflight == 3
    assert (res.user.rpm, res.team.rpm, res.global_.rpm) == (7, 70, 700)
    assert last_known_limits("TEAM", "t1", "m").model_agnostic is True
    assert last_known_limits("GLOBAL", None, "m").rpm == 700


@pytest.mark.asyncio
async def test_rl_config_breaker_open_skips_redis_reads_and_writes():
    from app.services.rate_limit_config_loader import load_all_scope_limits

    REDIS_BREAKER.trip("test")
    redis = MagicMock()
    redis.get = AsyncMock()
    redis.set = AsyncMock()
    res = await load_all_scope_limits(redis=redis, db=None, user_id="u1", team_id="t1",
                                      model_alias="m")
    redis.get.assert_not_called()
    redis.set.assert_not_called()
    assert res.user.rpm is None  # no DB either → unlimited, as before


@pytest.mark.asyncio
async def test_rl_config_hang_trips_breaker_fast(monkeypatch):
    from app.config import get_settings
    from app.services.rate_limit_config_loader import load_all_scope_limits

    get_settings.cache_clear()
    monkeypatch.setenv("HOT_REDIS_TIMEOUT_SEC", "0.05")
    try:
        async def hang(_k):
            await asyncio.sleep(5)

        redis = MagicMock()
        redis.get = AsyncMock(side_effect=hang)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await load_all_scope_limits(redis=redis, db=None, user_id="u1", team_id="t1",
                                    model_alias="m")
        assert loop.time() - t0 < 1.0
        assert not REDIS_BREAKER.available()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_settle_tpm_marker_gets_are_concurrent():
    from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor
    from app.services.rate_limit_service import RateLimitService

    barrier = _Barrier(3)
    bucket = 1000

    async def get(_key):
        await barrier.wait()
        return str(bucket).encode()

    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=get)
    redis.pipeline = MagicMock(return_value=pipe)
    desc = [ScopeDescriptor(scope=s, scope_id=i, model_alias="m", tpm_limit=10)
            for s, i in ((RateLimitScope.USER, "u"), (RateLimitScope.TEAM, "t"),
                         (RateLimitScope.GLOBAL, "*"))]
    await RateLimitService().settle_tpm(redis, desc, 100, 40, reserved_at=bucket * 60 + 5)
    assert barrier.max_inflight == 3
    assert pipe.incrby.call_count == 3  # every scope still settled into its reserved bucket


# ── VK auth: two GETs in one round trip, breaker → local value ────────────────


TOKEN = "vk-bbbbbbbb0000"
KH = hashlib.sha256(TOKEN.encode()).hexdigest()


def _ctx():
    from app.schemas.domain import AuthContext, AuthType, Role

    return AuthContext(user_id="u1", team_id="t1", dept_id="", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY, key_id=None, key_prefix="vk-bbbbbbbb",
                       allowed_models=None, allowed_clients=None, sso_subject="s")


def _caches():
    from app.services import auth_service as auth_mod

    c = auth_mod.AuthLocalCaches(ttl=45, max_stale=300, max_entries=100,
                                 redis_fallback_timeout=0.2, db_timeout=0.2)
    auth_mod.configure_auth_local_caches(c)
    return c


@pytest.mark.asyncio
async def test_vk_auth_cache_and_mapping_gets_are_concurrent():
    from app.services import auth_service as auth_mod

    caches = _caches()
    caches.active.put("u1", True)
    barrier = _Barrier(2)
    store = {f"key:cache:vk:{KH}": _ctx().model_dump_json(), f"key:vk:{KH}": "u1"}

    async def get(key):
        await barrier.wait()
        return store.get(key)

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=get)
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert ctx.user_id == "u1" and barrier.max_inflight == 2


@pytest.mark.asyncio
async def test_vk_auth_revoked_mapping_still_rejected():
    from app.services import auth_service as auth_mod

    caches = _caches()
    caches.active.put("u1", True)
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda k: _ctx().model_dump_json()
                          if k.startswith("key:cache:") else None)
    redis.delete = AsyncMock()
    with pytest.raises(PermissionError):
        await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)


@pytest.mark.asyncio
async def test_vk_auth_breaker_open_serves_known_key_without_redis():
    from app.services import auth_service as auth_mod

    caches = _caches()
    caches.ctx.put(KH, _ctx())
    REDIS_BREAKER.trip("test")
    redis = MagicMock()
    redis.get = AsyncMock()
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {TOKEN}", redis, None)
    assert ctx.user_id == "u1"
    redis.get.assert_not_called()


# ── budget check: config EXISTS pair in one round trip ────────────────────────


@pytest.mark.asyncio
async def test_budget_config_exists_pair_is_concurrent():
    from app.services.budget_service import BudgetService

    barrier = _Barrier(2)

    async def exists(_key):
        await barrier.wait()
        return 1

    team_ok = json.dumps({"allowed": True, "config_present": True, "used": "0", "limit": "10",
                          "policy": "hard_block", "reason": None})
    redis = MagicMock()
    redis.exists = AsyncMock(side_effect=exists)
    redis.mget = AsyncMock(side_effect=lambda keys: [None] * len(keys))
    redis.eval = AsyncMock(return_value=team_ok)
    redis.get = AsyncMock(return_value=None)
    await BudgetService().check_budget(redis, None, "u1", "t1", "2026-11")
    assert barrier.max_inflight == 2


# ── breaker open: admission + bounded fallback ───────────────────────────────


def _auth(user_id="u1", team_id="t1"):
    from app.schemas.domain import AuthContext, AuthType, Role

    return AuthContext(user_id=user_id, team_id=team_id, dept_id="d1", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY, key_id="k1")


def _model_config():
    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="m1", alias="m1", provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE, endpoint="us-east-1",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0")),
        status=ModelStatus.ACTIVE,
    )


@pytest.fixture
def _fresh_limiter():
    from app.middleware import rate_limit as rl
    from app.services.rate_limit_service import InMemoryRateLimiter

    saved = rl._in_memory_limiter
    rl._in_memory_limiter = InMemoryRateLimiter(worker_count=1)
    yield rl
    rl._in_memory_limiter = saved


async def _drive_mw(rl, state):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    out = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await rl.RateLimitMiddleware(app)({"type": "http", "path": "/model/m1/invoke",
                                        "state": state}, receive, send)
    return out["status"]


@pytest.mark.asyncio
async def test_middleware_fallback_on_open_breaker_uses_last_known_limit(_fresh_limiter):
    from app.services.rate_limit_config_loader import ScopeLimits, remember_limits

    rl = _fresh_limiter
    remember_limits("USER", "u1", "m1", ScopeLimits(rpm=2))
    REDIS_BREAKER.trip("test")
    statuses = []
    for _ in range(3):
        state = {"auth_context": _auth(), "_redis": object(), "model_config": _model_config()}
        statuses.append(await _drive_mw(rl, state))
        assert state["_rl_fallback_checked"] is True
    assert statuses == [200, 200, 429]


@pytest.mark.asyncio
async def test_middleware_fallback_unknown_or_unlimited_uses_default(_fresh_limiter, monkeypatch):
    from app.services.rate_limit_config_loader import ScopeLimits, remember_limits

    rl = _fresh_limiter
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 1)
    remember_limits("USER", "u1", "m1", ScopeLimits(rpm=None))  # configured unlimited
    REDIS_BREAKER.trip("test")
    st = lambda: {"auth_context": _auth(), "_redis": object(), "model_config": _model_config()}  # noqa: E731
    assert await _drive_mw(rl, st()) == 200
    assert await _drive_mw(rl, st()) == 429  # fallback stays bounded


@pytest.mark.asyncio
async def test_middleware_passes_through_when_redis_healthy(_fresh_limiter, monkeypatch):
    rl = _fresh_limiter
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 1)
    for _ in range(3):
        state = {"auth_context": _auth(), "_redis": object(), "model_config": _model_config()}
        assert await _drive_mw(rl, state) == 200
        assert "_rl_fallback_checked" not in state


@pytest.mark.asyncio
async def test_admission_breaker_open_skips_redis_and_enforces_fallback(
    _fresh_limiter, monkeypatch,
):
    from app.services import rate_limit_enforcement as enf

    rl = _fresh_limiter
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 1)
    REDIS_BREAKER.trip("test")
    redis = MagicMock()
    redis.get = AsyncMock()
    redis.eval = AsyncMock()

    async def admit():
        return await enf._enforce_rate_limits(
            redis=redis, auth_context=_auth(), model_config=_model_config(),
            body={"max_tokens": 10, "messages": []}, state={}, request_id="r1",
        )

    assert await admit() is None              # within the fallback limit, no Redis at all
    rejected = await admit()                  # middleware did not check → counted here
    assert rejected is not None and rejected.status_code == 429
    assert rejected.headers["x-ratelimit-degraded"] == "true"
    redis.get.assert_not_called()
    redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_admission_breaker_open_does_not_double_count(_fresh_limiter, monkeypatch):
    from app.services import rate_limit_enforcement as enf

    rl = _fresh_limiter
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 1)
    REDIS_BREAKER.trip("test")
    state = {"_rl_fallback_checked": True}
    for _ in range(3):
        assert await enf._enforce_rate_limits(
            redis=MagicMock(), auth_context=_auth(), model_config=_model_config(),
            body={}, state=state, request_id="r1",
        ) is None


@pytest.mark.asyncio
async def test_admission_breaker_open_fail_closed_still_blocks(_fresh_limiter, monkeypatch):
    from app.config import get_settings
    from app.services import rate_limit_enforcement as enf
    from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

    get_settings.cache_clear()
    monkeypatch.setenv("RL_FAIL_MODE", "closed")

    async def _limits(**_k):
        return AllScopeLimits(user=ScopeLimits(rpm=100))

    monkeypatch.setattr(enf, "load_all_scope_limits", _limits)
    from app.services.lua_loader import LuaScriptLoader

    monkeypatch.setattr(LuaScriptLoader, "get", staticmethod(lambda _n: "-- s"))
    REDIS_BREAKER.trip("test")
    redis = MagicMock()
    redis.eval = AsyncMock()
    try:
        resp = await enf._enforce_rate_limits(
            redis=redis, auth_context=_auth(), model_config=_model_config(),
            body={}, state={"_rl_fallback_checked": True}, request_id="r1",
        )
        assert resp is not None and resp.status_code == 429  # closed policy unchanged
        redis.eval.assert_not_called()                        # but no Redis wait
    finally:
        get_settings.cache_clear()


# ── cost events: breaker open → spool, not XADD ──────────────────────────────


@pytest.mark.asyncio
async def test_publish_event_spools_when_breaker_open():
    from app.services.cost_recorder import CostRecorder

    spool = MagicMock()
    rec = CostRecorder(spool=spool)
    redis = MagicMock()
    redis.xadd = AsyncMock()
    REDIS_BREAKER.trip("test")
    assert await rec.publish_event(redis, "{}", request_id="r") is False
    redis.xadd.assert_not_called()
    spool.enqueue.assert_called_once_with("{}")


@pytest.mark.asyncio
async def test_publish_event_xadd_failure_trips_breaker_and_spools():
    from app.services.cost_recorder import CostRecorder

    spool = MagicMock()
    rec = CostRecorder(spool=spool)
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=RedisConnectionError("down"))
    assert await rec.publish_event(redis, "{}", request_id="r") is False
    assert not REDIS_BREAKER.available()
    spool.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_publish_event_without_spool_still_tries_xadd():
    from app.services.cost_recorder import CostRecorder

    rec = CostRecorder(spool=None)
    redis = MagicMock()
    redis.xadd = AsyncMock()
    REDIS_BREAKER.trip("test")
    assert await rec.publish_event(redis, "{}", request_id="r") is True


# ── downgrade rules: breaker open → last known, no Redis wait ─────────────────


@pytest.mark.asyncio
async def test_downgrade_loader_skips_redis_when_breaker_open():
    import uuid

    from app.services.downgrade_loader import DowngradePolicyLoader, DowngradeRule

    loader = DowngradePolicyLoader()
    tid = uuid.uuid4()
    rule = DowngradeRule(from_alias="a", to_alias="b", threshold_pct=80)
    loader._last.put(str(tid), [rule])
    redis = MagicMock()
    redis.get = AsyncMock()
    redis.setex = AsyncMock()
    REDIS_BREAKER.trip("test")
    assert await loader.get_active_rules(redis, None, tid) == [rule]
    redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_downgrade_loader_redis_error_trips_breaker():
    import uuid

    from app.services.downgrade_loader import DowngradePolicyLoader

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=RedisConnectionError("down"))
    assert await DowngradePolicyLoader().get_active_rules(redis, None, uuid.uuid4()) == []
    assert not REDIS_BREAKER.available()


# ── XREVIEW #1: only infra errors trip the shared breaker ─────────────────────

_TRIP_CASES = [
    (MaxConnectionsError("pool"), False),
    (ResponseError("WRONGTYPE"), False),
    (RedisConnectionError("down"), True),
    (RedisTimeoutError("slow"), True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,trips", _TRIP_CASES)
async def test_budget_redis_path_trips_only_on_infra_errors(exc, trips):
    from app.services import budget_degraded
    from app.services.budget_service import BudgetService

    breaker = budget_degraded.DEGRADED.redis_breaker
    breaker.reset()
    try:
        redis = MagicMock()
        redis.exists = AsyncMock(side_effect=exc)
        redis.mget = AsyncMock(side_effect=exc)
        redis.eval = AsyncMock(side_effect=exc)
        redis.get = AsyncMock(side_effect=exc)
        await BudgetService().check_budget(redis, None, "u1", "t1", "2026-11")
        assert breaker.is_open() is trips
    finally:
        breaker.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,trips", _TRIP_CASES)
async def test_model_cache_redis_call_trips_only_on_infra_errors(exc, trips):
    from app.config import get_settings
    from app.services import model_cache

    cache = model_cache.from_settings(get_settings(), session_factory=None)
    cache.redis_breaker.reset()
    try:
        async def boom():
            raise exc

        out, failed = await cache.redis_call(boom, what="x")
        assert (out, failed) == (None, True)
        assert cache.redis_breaker.is_open() is trips
    finally:
        cache.redis_breaker.reset()
