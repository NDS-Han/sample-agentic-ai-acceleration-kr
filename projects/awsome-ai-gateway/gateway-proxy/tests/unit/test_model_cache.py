# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Wave 1d: router model/catalog cache on the hot path (services/model_cache.py).

Covers: local hit (no Redis/DB), stale-while-revalidate background refresh, bounded Redis and
DB reads on a miss, last known value during DB/Redis faults, max-stale expiry, negative TTL,
INACTIVE / provider mismatch semantics, pub/sub invalidation (+ generation guard against an
in-flight refresh), subscription-down refresh interval, refresh back-off, alias_provider,
model list, routing profile, and the 503 contract through the auth middleware.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.services import hot_path
from app.services import model_cache as mc
from app.services import router_service as rs
from app.services.model_cache import ModelBackendUnavailableError, ModelCatalogCache
from app.services.router_service import ModelInactiveError, RouterService
from app.services.routing_profile_loader import RoutingProfileLoader

# ── fakes ─────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeRedis:
    def __init__(self, store: dict | None = None, *, hang: float = 0.0, exc=None) -> None:
        self.store = dict(store or {})
        self.hang = hang
        self.exc = exc
        self.gets: list[str] = []
        self.setex_calls: list[tuple] = []
        self.deleted: list[str] = []

    async def _fault(self):
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.exc is not None:
            raise self.exc

    async def get(self, key):
        self.gets.append(key)
        await self._fault()
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        await self._fault()
        self.setex_calls.append((key, ttl))
        self.store[key] = value

    async def delete(self, key):
        await self._fault()
        self.deleted.append(key)
        self.store.pop(key, None)


def _row(alias="claude-sonnet-4-6", pmid="global.anthropic.claude-sonnet-4-6",
         provider="BEDROCK", status="ACTIVE"):
    return SimpleNamespace(
        alias=alias, provider=provider, provider_model_id=pmid, endpoint_url=None,
        api_format="BEDROCK_NATIVE", status=status, description=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC), allowed_clients=None,
        account_role_arn=None, external_id=None, region=None,
    )


def _schema(alias="claude-sonnet-4-6", pmid="global.anthropic.claude-sonnet-4-6",
            provider=ProviderType.BEDROCK, status=ModelStatus.ACTIVE, price="0.003"):
    return ModelConfigSchema(
        provider_model_id=pmid, alias=alias, provider=provider,
        api_format=ApiFormat.BEDROCK_NATIVE, endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal(price), output_per_1k=Decimal("0.015")),
        status=status, created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class FakeDb:
    """Catalog 'database' behind the patched router_service query helpers."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {"claude-sonnet-4-6": _row()}
        self.price = Decimal("0.003")
        self.delay = 0.0
        self.exc: Exception | None = None
        self.calls = 0

    async def _gate(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc

    async def find_alias(self, _s, ref, provider_values):
        await self._gate()
        row = self.rows.get(ref)
        if row is not None and row.provider in provider_values:
            return row, False
        for r in sorted(self.rows.values(), key=lambda r: (r.status != "ACTIVE", r.alias)):
            if r.provider_model_id == ref and r.provider in provider_values:
                return r, True
        return None, True

    async def pricing(self, _s, _alias):
        p = MagicMock()
        p.input_price_per_1k_tokens = self.price
        p.output_price_per_1k_tokens = Decimal("0.015")
        p.cache_creation_5m_price_per_1k_tokens = Decimal("0")
        p.cache_creation_1h_price_per_1k_tokens = Decimal("0")
        p.cache_read_price_per_1k_tokens = Decimal("0")
        p.long_context_threshold_tokens = None
        p.long_context_input_price_per_1k_tokens = None
        p.long_context_output_price_per_1k_tokens = None
        p.long_context_cache_creation_5m_price_per_1k_tokens = None
        p.long_context_cache_creation_1h_price_per_1k_tokens = None
        p.long_context_cache_read_price_per_1k_tokens = None
        return p

    async def alias_provider(self, _s, alias):
        await self._gate()
        row = self.rows.get(alias)
        return ProviderType(row.provider) if row else None

    async def active_models(self, _s):
        await self._gate()
        return [rs._orm_to_schema(r, await self.pricing(None, r.alias))
                for r in self.rows.values() if r.status == "ACTIVE"]


@pytest.fixture
def fdb(monkeypatch):
    f = FakeDb()
    monkeypatch.setattr(rs, "_db_find_alias", f.find_alias)
    monkeypatch.setattr(rs, "_fetch_latest_pricing", f.pricing)
    monkeypatch.setattr(rs, "_db_alias_provider", f.alias_provider)
    monkeypatch.setattr(rs, "_db_active_models", f.active_models)
    return f


@asynccontextmanager
async def _session():
    yield SimpleNamespace()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(clock):
    c = ModelCatalogCache(session_factory=_session, clock=clock, db_timeout_sec=0.3,
                          redis_timeout_sec=0.2, refresh_timeout_sec=0.5)
    mc.configure(c)
    yield c
    mc.configure(None)


DB = object()  # "caller has a session" marker — the cache uses its own short sessions
SONNET = "claude-sonnet-4-6"


async def _drain():
    for _ in range(5):
        tasks = [t for t in hot_path._INFLIGHT.values() if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def _bedrock(redis, db=DB, ref=SONNET):
    return await RouterService().resolve_bedrock_model(redis, db, ref)


# ── local hit / refresh ───────────────────────────────────────────────────────


async def test_local_hit_does_not_touch_redis_or_db(cache, fdb):
    redis = FakeRedis()
    first = await _bedrock(redis)
    assert first.alias == SONNET and fdb.calls == 1
    redis.gets.clear()
    for _ in range(5):
        assert (await _bedrock(redis)).alias == SONNET
    assert redis.gets == [] and fdb.calls == 1


async def test_db_load_writes_redis_like_legacy(cache, fdb):
    redis = FakeRedis()
    await _bedrock(redis)
    assert (f"model:{SONNET}", rs.MODEL_CACHE_TTL) in redis.setex_calls
    # alias path never writes the pmid key (E2E S3-H2)
    assert not any(k.startswith("model:pmid:") for k, _ in redis.setex_calls)
    cached = await _bedrock(FakeRedis(), ref="global.anthropic.claude-sonnet-4-6")
    assert cached.alias == SONNET


async def test_stale_entry_served_and_refreshed_in_background(cache, fdb, clock):
    redis = FakeRedis()
    await _bedrock(redis)
    fdb.price = Decimal("0.004")
    clock.t += 61
    stale = await _bedrock(redis)
    assert stale.pricing.input_per_1k == Decimal("0.003")  # served immediately
    await _drain()
    fresh = await _bedrock(redis)
    assert fresh.pricing.input_per_1k == Decimal("0.004") and fdb.calls == 2


async def test_redis_hit_is_followed_by_one_db_refresh(cache, fdb):
    redis = FakeRedis({f"model:{SONNET}": _schema(price="0.009").model_dump_json()})
    got = await _bedrock(redis)
    assert got.pricing.input_per_1k == Decimal("0.009") and fdb.calls == 0
    await _drain()
    assert fdb.calls == 1
    assert (await _bedrock(redis)).pricing.input_per_1k == Decimal("0.003")


async def test_refresh_failure_backs_off(cache, fdb, clock):
    await _bedrock(FakeRedis())
    fdb.exc = OSError("db down")
    clock.t += 61
    await _bedrock(FakeRedis())
    await _drain()
    assert fdb.calls == 2
    hot_path.DB_BREAKER.reset()
    await _bedrock(FakeRedis())  # inside refresh_retry_sec → no new refresh
    await _drain()
    assert fdb.calls == 2
    clock.t += 11
    await _bedrock(FakeRedis())
    await _drain()
    assert fdb.calls == 3


async def test_unsubscribed_uses_shorter_refresh(cache, fdb, clock):
    connected = {"v": True}
    cache.subscribed = lambda: connected["v"]
    await _bedrock(FakeRedis())
    clock.t += 11
    await _bedrock(FakeRedis())
    await _drain()
    assert fdb.calls == 1  # subscribed: 60 s
    connected["v"] = False
    await _bedrock(FakeRedis())
    await _drain()
    assert fdb.calls == 2  # unsubscribed: 10 s


# ── bounded reads on a miss ───────────────────────────────────────────────────


async def test_miss_with_redis_hang_is_bounded_and_uses_db(cache, fdb):
    redis = FakeRedis(hang=5.0)
    t0 = time.monotonic()
    got = await _bedrock(redis)
    assert got.alias == SONNET and time.monotonic() - t0 < 1.5
    assert not cache.redis_breaker.available()
    # next miss skips Redis entirely while the Redis breaker is open
    fdb.rows["claude-haiku-4-5"] = _row(alias="claude-haiku-4-5", pmid="haiku")
    t0 = time.monotonic()
    await _bedrock(redis, ref="claude-haiku-4-5")
    assert time.monotonic() - t0 < 0.15


async def test_miss_with_slow_db_is_bounded_and_trips_breaker(cache, fdb):
    fdb.delay = 5.0
    t0 = time.monotonic()
    with pytest.raises(ModelBackendUnavailableError):
        await _bedrock(FakeRedis())
    assert time.monotonic() - t0 < 1.0
    assert not hot_path.DB_BREAKER.available()


async def test_unknown_model_with_redis_and_db_down_is_503_not_404(cache, fdb):
    fdb.exc = OSError("db down")
    with pytest.raises(ModelBackendUnavailableError) as ei:
        await _bedrock(FakeRedis(exc=RedisConnectionError("reset")))
    assert isinstance(ei.value, ConnectionError) and not isinstance(ei.value, LookupError)


async def test_db_none_keeps_legacy_contract(cache, fdb):
    with pytest.raises(LookupError, match="DB unavailable"):
        await _bedrock(FakeRedis(), db=None)
    with pytest.raises(ModelBackendUnavailableError):
        await _bedrock(FakeRedis(exc=RedisConnectionError("x")), db=None)
    assert fdb.calls == 0


# ── last known value on faults ────────────────────────────────────────────────


async def test_known_model_served_during_db_and_redis_outage(cache, fdb, clock):
    await _bedrock(FakeRedis())
    fdb.exc = OSError("db down")
    down = FakeRedis(exc=RedisConnectionError("reset"))
    clock.t += 600
    for db in (DB, None):
        assert (await _bedrock(down, db=db)).alias == SONNET
    await _drain()


async def test_last_known_expires_after_max_stale(cache, fdb, clock):
    await _bedrock(FakeRedis())
    fdb.exc = OSError("db down")
    clock.t += 3601
    with pytest.raises(ModelBackendUnavailableError):
        await _bedrock(FakeRedis(exc=RedisConnectionError("reset")))


async def test_known_model_served_while_db_breaker_open(cache, fdb, clock):
    await _bedrock(FakeRedis())
    hot_path.DB_BREAKER.trip("test")
    clock.t += 120
    assert (await _bedrock(FakeRedis())).alias == SONNET
    await _drain()
    assert fdb.calls == 1  # no refresh while the breaker is open


# ── semantics ─────────────────────────────────────────────────────────────────


async def test_unknown_model_negative_cached_for_15s(cache, fdb, clock):
    redis = FakeRedis()
    with pytest.raises(LookupError):
        await _bedrock(redis, ref="nope")
    assert ("model:nx:BEDROCK:nope", 15) in redis.setex_calls
    with pytest.raises(LookupError):
        await _bedrock(FakeRedis(), ref="nope")
    assert fdb.calls == 1
    fdb.rows["nope"] = _row(alias="nope", pmid="nope-pmid")
    clock.t += 16
    with pytest.raises(LookupError):
        await _bedrock(FakeRedis(), ref="nope")  # stale nx served, refresh scheduled
    await _drain()
    assert (await _bedrock(FakeRedis(), ref="nope")).alias == "nope"


async def test_inactive_raises_model_inactive_from_db_and_cache(cache, fdb):
    fdb.rows[SONNET].status = "INACTIVE"
    for _ in range(2):
        with pytest.raises(ModelInactiveError):
            await _bedrock(FakeRedis())
    assert fdb.calls == 1


async def test_redis_entry_inactive_or_other_provider(cache, fdb):
    inactive = _schema(status=ModelStatus.INACTIVE).model_dump_json()
    with pytest.raises(ModelInactiveError):
        await _bedrock(FakeRedis({f"model:{SONNET}": inactive}))
    mc.get().reset()
    other = _schema(alias="cowork-opus", provider=ProviderType.BEDROCK_MANTLE).model_dump_json()
    with pytest.raises(LookupError):
        await _bedrock(FakeRedis({"model:cowork-opus": other}), ref="cowork-opus")


async def test_provider_sets_are_cached_separately(cache, fdb):
    fdb.rows["gpt"] = _row(alias="gpt", pmid="openai.gpt", provider="BEDROCK_MANTLE_OPENAI")
    with pytest.raises(LookupError):
        await _bedrock(FakeRedis(), ref="gpt")
    got = await RouterService().resolve_codex_model(FakeRedis(), DB, "gpt")
    assert got.provider == ProviderType.BEDROCK_MANTLE_OPENAI


# ── invalidation ──────────────────────────────────────────────────────────────


async def test_invalidation_by_alias_and_pmid(cache, fdb):
    await _bedrock(FakeRedis())
    await _bedrock(FakeRedis(), ref="global.anthropic.claude-sonnet-4-6")
    fdb.price = Decimal("0.005")
    cache.invalidate({"model_refs": ["global.anthropic.claude-sonnet-4-6"], "model_list": True,
                      "routing_clients": []})
    for ref in (SONNET, "global.anthropic.claude-sonnet-4-6"):
        assert (await _bedrock(FakeRedis(), ref=ref)).pricing.input_per_1k == Decimal("0.005")


async def test_invalidation_kill_switch_is_immediate(cache, fdb):
    await _bedrock(FakeRedis())
    fdb.rows[SONNET].status = "INACTIVE"
    cache.invalidate({"model_refs": [SONNET]})
    with pytest.raises(ModelInactiveError):
        await _bedrock(FakeRedis())


async def test_invalidation_clears_negative_entry(cache, fdb):
    with pytest.raises(LookupError):
        await _bedrock(FakeRedis(), ref="new-model")
    fdb.rows["new-model"] = _row(alias="new-model", pmid="new-pmid")
    cache.invalidate({"model_refs": ["new-model"]})
    assert (await _bedrock(FakeRedis(), ref="new-model")).alias == "new-model"


async def test_invalidation_beats_inflight_refresh(cache, fdb, clock):
    await _bedrock(FakeRedis())
    clock.t += 61
    fdb.delay = 0.05  # refresh reads the OLD row ...
    await _bedrock(FakeRedis())
    fdb.rows[SONNET].status = "INACTIVE"
    cache.invalidate({"model_refs": [SONNET]})  # ... admin commit + publish lands meanwhile
    await _drain()
    fdb.delay = 0.0
    with pytest.raises(ModelInactiveError):
        await _bedrock(FakeRedis())


async def test_invalidate_all_and_unrelated(cache, fdb):
    await _bedrock(FakeRedis())
    cache.invalidate({"model_refs": ["other"]})
    assert len(cache.cache) == 1
    cache.invalidate({"all": True})
    assert len(cache.cache) == 0


async def test_listener_delivers_payload_to_cache(cache, fdb, monkeypatch):
    import redis.asyncio as aioredis

    msg = {"type": "message", "data": json.dumps({"model_refs": [SONNET]}).encode()}

    class PubSub:
        async def subscribe(self, _ch):
            pass

        async def listen(self):
            yield {"type": "subscribe"}
            yield msg
            await asyncio.sleep(30)

    class Client:
        def pubsub(self):
            return PubSub()

        async def aclose(self):
            pass

    monkeypatch.setattr(aioredis.Redis, "from_url", classmethod(lambda cls, *a, **k: Client()))
    await _bedrock(FakeRedis())
    assert len(cache.cache) == 1
    listener = hot_path.AuthInvalidationListener("redis://x", "gateway:model:invalidate",
                                                 cache.invalidate)
    cache.subscribed = lambda: listener.connected
    listener.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(cache.cache) == 0:
            break
    assert len(cache.cache) == 0 and listener.connected
    await listener.stop()
    assert not listener.connected


# ── alias_provider / list / routing profile ───────────────────────────────────


async def test_alias_provider_cached_and_bounded(cache, fdb):
    svc = RouterService()
    assert await svc.alias_provider(FakeRedis(), DB, SONNET) == ProviderType.BEDROCK
    assert await svc.alias_provider(FakeRedis(), DB, "unknown") is None
    fdb.exc = OSError("down")
    assert await svc.alias_provider(FakeRedis(exc=RedisConnectionError("x")), DB, SONNET) \
        == ProviderType.BEDROCK
    with pytest.raises(ModelBackendUnavailableError):
        await svc.alias_provider(FakeRedis(exc=RedisConnectionError("x")), DB, "never-seen")


async def test_list_active_models_cached(cache, fdb):
    svc = RouterService()
    redis = FakeRedis()
    first = await svc.list_active_models(redis, DB)
    assert [m.alias for m in first] == [SONNET]
    first.clear()  # caller mutation must not poison the cache
    fdb.exc = OSError("down")
    assert [m.alias for m in await svc.list_active_models(redis, DB)] == [SONNET]
    cache.invalidate({"model_list": True})
    with pytest.raises(ModelBackendUnavailableError):
        await svc.list_active_models(FakeRedis(exc=RedisConnectionError("x")), DB)
    cache.redis_breaker.reset()
    assert await svc.list_active_models(FakeRedis(), None) == []  # legacy db=None contract


def _profile_row(client="cowork", enabled=True):
    return SimpleNamespace(client=client, backend="mantle", account_role_arn=None,
                           region="ap-northeast-1", default_model="cowork-opus",
                           external_id=None, enabled=enabled, web_search_enabled=True)


def _profile_session(rows: dict, calls: list, *, exc=None):
    @asynccontextmanager
    async def sf():
        async def execute(stmt):
            calls.append(stmt)
            if exc is not None:
                raise exc
            client = stmt.compile().params["client_1"]
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=rows.get(client))
            return r

        yield SimpleNamespace(execute=execute)

    return sf


async def test_routing_profile_cached_and_last_known(cache):
    calls: list = []
    cache.session_factory = _profile_session({"cowork": _profile_row()}, calls)
    loader = RoutingProfileLoader()
    redis = FakeRedis()
    p = await loader.load(redis, DB, "cowork")
    assert p.default_model == "cowork-opus"
    assert {k for k, _ in redis.setex_calls} == {"routing_profile:cowork",
                                                 "routing_profile:cowork:stale"}
    assert await loader.load(FakeRedis(exc=RedisConnectionError("x")), DB, "cowork") == p
    assert len(calls) == 1
    # absent profile → None, absence marker + stale backup removed
    assert await loader.load(redis, DB, "claude-code") is None
    assert "routing_profile:claude-code:stale" in redis.deleted
    # invalidation by client
    cache.session_factory = _profile_session({"cowork": _profile_row(enabled=False)}, calls)
    cache.invalidate({"routing_clients": ["cowork"]})
    assert await loader.load(FakeRedis(), DB, "cowork") is None


async def test_routing_profile_db_down_uses_redis_stale_backup(cache):
    cache.session_factory = _profile_session({}, [], exc=OSError("down"))
    payload = json.dumps({"client": "cowork", "backend": "mantle", "region": "ap-northeast-1",
                          "default_model": "cowork-opus", "enabled": True})
    redis = FakeRedis({"routing_profile:cowork:stale": payload})
    p = await RoutingProfileLoader().load(redis, DB, "cowork")
    assert p is not None and p.default_model == "cowork-opus"
    hot_path.DB_BREAKER.reset()
    assert await RoutingProfileLoader().load(FakeRedis(), None, "cowork") is None


# ── 503 contract through the auth middleware ──────────────────────────────────


async def test_auth_middleware_turns_catalog_outage_into_503():
    from app.middleware.auth import AuthMiddleware
    from app.services import auth_service as auth_mod

    token = "vk-aaaaaaaa0000"
    caches = auth_mod.AuthLocalCaches(ttl=45, max_stale=300, max_entries=10,
                                      redis_fallback_timeout=0.2, db_timeout=0.2)
    caches.ctx.put(hashlib.sha256(token.encode()).hexdigest(), AuthContext(
        user_id="u1", team_id="t1", dept_id="", roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY, key_id=None, key_prefix="vk-aaaaaaaa",
        allowed_models=None, allowed_clients=None, sso_subject="s"))
    auth_mod.configure_auth_local_caches(caches)

    async def inner(scope, receive, send):
        raise ModelBackendUnavailableError("catalog down")

    sent = []

    async def send(m):
        sent.append(m)

    from app.schemas.domain import DegradationLevel

    scope = {"type": "http", "path": "/model/x/invoke-with-response-stream",
             "headers": [(b"authorization", f"Bearer {token}".encode())],
             "state": {"_degradation_manager":
                       SimpleNamespace(level=DegradationLevel.BOTH_DEGRADED),
                       "_redis": None, "_session_factory": None}}
    await AuthMiddleware(inner)(scope, AsyncMock(), send)
    assert sent[0]["status"] == 503


async def test_legacy_path_when_cache_disabled(fdb):
    assert mc.get() is None
    redis = FakeRedis()
    db = MagicMock()
    await _bedrock(redis, db=db)
    assert f"model:{SONNET}" in redis.gets  # legacy reads Redis on every request
