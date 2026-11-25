# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Wave 1e: startup warm-up before readiness + recently used VK pre-load.

Covers the recorder (hashes only, throttled, background ZADD), the bounded warm-up orchestrator
(degraded ready + background completion), the steps (pool open, auth / budget / model / jwt
pre-load), the readiness body and the lifespan ordering. Real-Postgres equivalence tests
(batch pre-load == request-path values) run when PROOF_DSN is set.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import health as health_router
from app.schemas.domain import AuthContext, AuthType, DegradationLevel, ProviderType, Role
from app.services import auth_service as auth_mod
from app.services import recent_vk
from app.services import startup_warmup as sw
from app.services.model_cache import ModelBackendUnavailableError

KEY = "gateway:auth:recent_vk"
U1, U2 = "00000000-0000-4000-a000-0000000000a1", "00000000-0000-4000-a000-0000000000a2"


@pytest.fixture(autouse=True)
def _reset():
    recent_vk.configure(None)
    yield
    recent_vk.configure(None)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── recorder ──────────────────────────────────────────────────────────────────


async def test_recorder_notes_each_key_once_per_interval_and_flushes_hashes_only():
    r = fakeredis.aioredis.FakeRedis()
    clk, wall = _Clock(), _Clock(1_700_000_000.0)
    rec = recent_vk.RecentVkRecorder(r, key=KEY, record_interval_sec=300, clock=clk, wall=wall)
    h1, h2 = "a" * 64, "b" * 64
    rec.note(h1)
    rec.note(h1)
    rec.note(h2)
    assert len(rec._pending) == 2
    assert await rec.flush() == 2
    assert rec._pending == {}
    members = await r.zrange(KEY, 0, -1, withscores=True)
    assert {m.decode() for m, _ in members} == {h1, h2}
    assert await r.ttl(KEY) > 86400  # expires if no gateway writes any more
    # within the interval: not noted again
    clk.t += 100
    rec.note(h1)
    assert rec._pending == {}
    # after the interval: noted again with the newer time (ZADD GT keeps the max)
    clk.t += 300
    wall.t += 400
    rec.note(h1)
    await rec.flush()
    assert await r.zscore(KEY, h1) == pytest.approx(1_700_000_400.0)


async def test_recorder_trims_by_window_and_size():
    r = fakeredis.aioredis.FakeRedis()
    wall = _Clock(1_700_000_000.0)
    await r.zadd(KEY, {"old": wall.t - 90000, **{f"k{i}": wall.t - i for i in range(10)}})
    rec = recent_vk.RecentVkRecorder(r, key=KEY, window_sec=86400, max_keys=5, wall=wall)
    rec.note("new")
    await rec.flush()
    kept = [m.decode() for m in await r.zrevrange(KEY, 0, -1)]
    assert kept == ["new", "k0", "k1", "k2", "k3"]


async def test_recorder_keeps_pending_when_redis_fails():
    r = AsyncMock()
    r.zadd = AsyncMock(side_effect=ConnectionError("down"))
    rec = recent_vk.RecentVkRecorder(r, key=KEY)
    rec.note("h1")
    assert await rec.flush() == 0
    assert "h1" in rec._pending  # retried on the next tick


async def test_recent_hashes_newest_first_within_window_capped():
    r = fakeredis.aioredis.FakeRedis()
    now = 1_700_000_000.0
    await r.zadd(KEY, {"stale": now - 90000, "a": now - 10, "b": now - 5, "c": now - 1})
    got = await recent_vk.recent_hashes(r, KEY, window_sec=86400, limit=2, wall=lambda: now)
    assert got == ["c", "b"]


def _ctx(uid="u1"):
    return AuthContext(user_id=uid, team_id="t1", dept_id="", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY, key_prefix="vk-aaaaaaaa")


async def test_vk_auth_success_notes_the_key_hash(mock_redis):
    seen: list[str] = []
    recent_vk.configure(SimpleNamespace(note=seen.append))
    token = "vk-aaaaaaaa0000"
    h = hashlib.sha256(token.encode()).hexdigest()
    store = {f"key:cache:vk:{h}": _ctx().model_dump_json(), f"key:vk:{h}": "u1"}
    mock_redis.get = AsyncMock(side_effect=lambda k: store.get(k))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: True))
    await auth_mod.VKAuthStrategy().authenticate(f"Bearer {token}", mock_redis, db)
    assert seen == [h]  # the hash, never the raw key
    # failed auth (no mapping) is not noted
    seen.clear()
    mock_redis.get = AsyncMock(return_value=None)
    with pytest.raises(PermissionError):
        await auth_mod.VKAuthStrategy().authenticate("Bearer vk-bbbbbbbb0000", mock_redis, db)
    assert seen == []


# ── orchestrator ──────────────────────────────────────────────────────────────


def _step(name, results, deps=()):
    """Step whose successive calls raise / return from ``results``."""
    calls = {"n": 0}

    async def fn():
        i = calls["n"]
        calls["n"] += 1
        v = results[min(i, len(results) - 1)]
        if isinstance(v, BaseException):
            raise v
        if isinstance(v, float):
            await asyncio.sleep(v)
            return {"slept": v}
        return v

    return sw.Step(name, fn, deps=deps), calls


async def test_warmup_completes_within_window():
    a, _ = _step("a", [{"x": 1}])
    b, _ = _step("b", [{"y": 2}], deps=("a",))
    w = sw.StartupWarmup([a, b], timeout_sec=5)
    assert await w.run() is True
    snap = w.snapshot()
    assert snap["state"] == "warm" and snap["background"] is False
    assert snap["steps"]["b"]["detail"] == {"y": 2}


async def test_failed_step_is_retried_inside_the_window():
    a, calls = _step("a", [ConnectionError("x"), {"ok": True}])
    w = sw.StartupWarmup([a], timeout_sec=5)
    assert await w.run() is True
    assert calls["n"] == 2 and w.snapshot()["steps"]["a"]["attempts"] == 2


async def test_window_ends_degraded_ready_then_background_completes(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(sw.asyncio, "sleep", lambda d: real_sleep(min(d, 0.05)))
    db, _ = _step("db", [ConnectionError("down")] * 6 + [{"ok": True}])
    dep, dep_calls = _step("dep", [{"ok": True}], deps=("db",))
    other, _ = _step("other", [{"ok": True}])
    w = sw.StartupWarmup([db, dep, other], timeout_sec=0.1, retry_max_sec=0.05)
    assert await w.run() is False
    snap = w.snapshot()
    assert snap["state"] == "degraded" and snap["background"] is True
    assert snap["steps"]["db"]["status"] == sw.FAILED
    assert snap["steps"]["db"]["error"] == "ConnectionError"
    assert snap["steps"]["dep"]["status"] == sw.WAITING and dep_calls["n"] == 0
    assert snap["steps"]["other"]["status"] == sw.OK
    await asyncio.wait_for(w._task, 5)
    snap = w.snapshot()
    assert snap["state"] == "warm" and snap["background"] is True
    assert "complete_after_sec" in snap


async def test_hung_step_is_bounded_by_the_step_deadline():
    hung, _ = _step("hung", [30.0, {"ok": True}])
    w = sw.StartupWarmup([hung], timeout_sec=0.2, step_timeout_sec=0.05)
    assert await w.run() is False
    await w.stop()
    assert w.snapshot()["steps"]["hung"]["error"] == "TimeoutError"


async def test_disabled_warmup_is_ready_immediately():
    a, calls = _step("a", [{"x": 1}])
    w = sw.StartupWarmup([a], enabled=False)
    assert await w.run() is True and calls["n"] == 0
    assert w.snapshot() == {"state": "disabled"}


# ── steps ─────────────────────────────────────────────────────────────────────


class _FakeEngine:
    def __init__(self, fail_at: int | None = None) -> None:
        self.open = 0
        self.peak = 0
        self.closed = 0
        self.fail_at = fail_at
        self.n = 0

    async def connect(self):
        self.n += 1
        if self.fail_at is not None and self.n == self.fail_at:
            raise OSError("refused")
        self.open += 1
        self.peak = max(self.peak, self.open)
        eng = self

        class _C:
            async def execute(self, _stmt):
                await asyncio.sleep(0.01)

            async def close(self):
                eng.open -= 1
                eng.closed += 1

        return _C()


async def test_open_pool_holds_n_connections_at_once_then_returns_them():
    eng = _FakeEngine()
    assert await sw.open_pool(eng, 5) == 5
    assert eng.peak == 5 and eng.open == 0


async def test_open_pool_failure_still_returns_opened_connections():
    eng = _FakeEngine(fail_at=3)
    with pytest.raises(OSError):
        await sw.open_pool(eng, 5)
    assert eng.open == 0 and eng.closed == 4


@asynccontextmanager
async def _null_session():
    yield SimpleNamespace()


def _sf():
    return _null_session()


async def test_warm_auth_fills_local_caches_for_mapped_recent_keys(monkeypatch):
    r = fakeredis.aioredis.FakeRedis()
    import time as _t

    now = _t.time()
    await r.zadd(KEY, {"h1": now - 5, "h2": now - 3, "revoked": now - 1, "bad": now - 2})
    await r.set("key:vk:h1", U1)
    await r.set("key:vk:h2", U2.upper())  # normalised
    await r.set("key:vk:bad", "not-a-uuid")  # skipped, does not fail the batch
    caches = auth_mod.AuthLocalCaches(ttl=45, max_stale=300, max_entries=100,
                                      redis_fallback_timeout=0.5, db_timeout=0.3)
    auth_mod.configure_auth_local_caches(caches)
    seen = {}

    async def fake_preload(_s, pairs):
        seen["pairs"] = pairs
        return {"h1": _ctx(U1)}, {U1: True, U2: False}

    monkeypatch.setattr(auth_mod, "preload_vk_contexts", fake_preload)
    shared = sw._Shared()
    out = await sw.warm_auth(r, _sf, recent_key=KEY, window_sec=86400, max_keys=10,
                             shared=shared)
    assert sorted(seen["pairs"]) == [("h1", U1), ("h2", U2)]  # revoked + malformed skipped
    assert out == {"recent": 4, "mapped": 2, "users": 2, "contexts": 1}
    assert caches.ctx.get_stale("h1").user_id == U1
    assert caches.ctx.get_stale("h2") is None
    assert caches.active.get_fresh(U1) is True and caches.active.get_fresh(U2) is False
    assert shared.users == {U1: "t1"}


async def test_warm_auth_discards_values_read_before_an_invalidation(monkeypatch):
    r = fakeredis.aioredis.FakeRedis()
    caches = auth_mod.AuthLocalCaches(ttl=45, max_stale=300, max_entries=100,
                                      redis_fallback_timeout=0.5, db_timeout=0.3)
    auth_mod.configure_auth_local_caches(caches)

    async def fake_preload(_s, _pairs):
        caches.invalidate({"user_ids": ["u1"]})
        return {"h1": _ctx("u1")}, {"u1": True}

    monkeypatch.setattr(auth_mod, "preload_vk_contexts", fake_preload)
    with pytest.raises(RuntimeError):
        await sw.warm_auth(r, _sf, recent_key=KEY, window_sec=86400, max_keys=10,
                           shared=sw._Shared())
    assert caches.ctx.get_stale("h1") is None and len(caches.active) == 0


async def test_preloaded_context_serves_a_first_seen_key_when_the_db_times_out(mock_redis):
    """The point of the auth pre-load: Redis AuthContext expired + DB slow → served, not 503."""
    from app.services import hot_path

    token = "vk-aaaaaaaa0000"
    h = hashlib.sha256(token.encode()).hexdigest()
    caches = auth_mod.AuthLocalCaches(ttl=45, max_stale=300, max_entries=100,
                                      redis_fallback_timeout=0.5, db_timeout=0.05)
    auth_mod.configure_auth_local_caches(caches)
    mock_redis.get = AsyncMock(side_effect=lambda k: "u1" if k == f"key:vk:{h}" else None)

    @asynccontextmanager
    async def slow_sf():
        async def execute(*_a, **_k):
            await asyncio.sleep(1)

        yield SimpleNamespace(execute=execute)

    hot_path.configure(session_factory=slow_sf)
    with pytest.raises(auth_mod.AuthBackendUnavailableError):
        await auth_mod.VKAuthStrategy().authenticate(f"Bearer {token}", mock_redis, None)
    hot_path.DB_BREAKER.reset()
    caches.ctx.put(h, _ctx("u1").model_copy(update={"key_prefix": None}))
    ctx = await auth_mod.VKAuthStrategy().authenticate(f"Bearer {token}", mock_redis, None)
    assert ctx.user_id == "u1" and ctx.key_prefix == token[:11]


async def test_warm_budget_uses_users_from_the_auth_step(monkeypatch):
    from app.services.budget_service import BudgetService

    got = {}

    async def fake(_s, uids, tids):
        got["u"], got["t"] = uids, tids
        return 3

    monkeypatch.setattr(BudgetService, "preload_policies", staticmethod(fake))
    shared = sw._Shared(users={"u1": "t1", "u2": None})
    assert await sw.warm_budget(_sf, shared) == {"policies": 3}
    assert got == {"u": ["u1", "u2"], "t": ["t1"]}


def test_resolvers_cover_every_request_entry_point():
    from app.services.router_service import RouterService

    rs = RouterService()
    names = lambda p: sorted(getattr(f, "__name__", "mantle") for f in sw._resolvers(rs, p))  # noqa: E731
    assert names("BEDROCK") == ["resolve_bedrock_model"]
    assert names("BEDROCK_MANTLE") == ["<lambda>"]
    assert names("BEDROCK_MANTLE_OPENAI") == ["<lambda>", "resolve_codex_model"]
    assert names("BEDROCK_RUNTIME_OPENAI") == ["resolve_codex_model", "resolve_openai_model"]
    assert names("OPENMODEL") == ["resolve_openai_model"]
    assert sw._resolvers(rs, "NOPE") == []


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


async def test_warm_models_calls_router_paths_for_alias_and_model_id(monkeypatch):
    from app.services import model_cache
    from app.services.router_service import RouterService

    results = [
        _Rows([("claude-x", "global.anthropic.x", "BEDROCK"),
               ("gpt", "openai.gpt", "BEDROCK_MANTLE_OPENAI")]),
        _Rows(["cowork"]),
    ]

    @asynccontextmanager
    async def sf():
        async def execute(_stmt):
            return results.pop(0)

        yield SimpleNamespace(execute=execute)

    cache = model_cache.ModelCatalogCache(session_factory=sf)
    model_cache.configure(cache)
    calls = []

    def rec(name):
        async def f(self, redis, db, ref=None, **kw):
            assert redis is None and db is not None
            calls.append((name, ref, kw.get("expected_provider")))
            if ref == "openai.gpt" and name == "codex":
                raise LookupError("other plane")
        return f

    monkeypatch.setattr(RouterService, "alias_provider", rec("prov"))
    monkeypatch.setattr(RouterService, "resolve_bedrock_model", rec("bedrock"))
    monkeypatch.setattr(RouterService, "resolve_mantle_model", rec("mantle"))
    monkeypatch.setattr(RouterService, "resolve_codex_model", rec("codex"))
    monkeypatch.setattr(RouterService, "list_active_models", rec("list"))
    loader = SimpleNamespace(load=AsyncMock())
    out = await sw.warm_models(sf, loader, max_aliases=10)
    assert sorted(calls, key=str) == sorted([
        ("prov", "claude-x", None), ("bedrock", "claude-x", None),
        ("prov", "global.anthropic.x", None), ("bedrock", "global.anthropic.x", None),
        ("prov", "gpt", None), ("mantle", "gpt", ProviderType.BEDROCK_MANTLE_OPENAI),
        ("codex", "gpt", None),
        ("prov", "openai.gpt", None), ("mantle", "openai.gpt", ProviderType.BEDROCK_MANTLE_OPENAI),
        ("codex", "openai.gpt", None), ("list", None, None),
    ], key=str)
    loader.load.assert_awaited_once()
    assert loader.load.await_args.args[0] is None and loader.load.await_args.args[2] == "cowork"
    assert out["aliases"] == 2 and out["profiles"] == 1


async def test_warm_models_fails_the_step_when_the_backend_is_unavailable(monkeypatch):
    from app.services import model_cache
    from app.services.router_service import RouterService

    results = [_Rows([("a", "a-id", "BEDROCK")]), _Rows([])]

    @asynccontextmanager
    async def sf():
        async def execute(_stmt):
            return results.pop(0)

        yield SimpleNamespace(execute=execute)

    model_cache.configure(model_cache.ModelCatalogCache(session_factory=sf))

    async def boom(*_a, **_k):
        raise ModelBackendUnavailableError("db down")

    for m in ("alias_provider", "resolve_bedrock_model", "list_active_models"):
        monkeypatch.setattr(RouterService, m, boom)
    with pytest.raises(ModelBackendUnavailableError):
        await sw.warm_models(sf, None, max_aliases=10)


async def test_warm_models_skipped_when_model_cache_disabled():
    assert await sw.warm_models(_sf, None, max_aliases=10) == {"skipped": "model cache disabled"}


async def test_warm_jwt_fills_local_keyset_and_fails_without_db(monkeypatch):
    strat = auth_mod.JWTAuthStrategy()
    monkeypatch.setattr(auth_mod, "_JWT_STRATEGY", strat)

    @asynccontextmanager
    async def sf():
        async def execute(_stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        yield SimpleNamespace(execute=execute)

    assert await sw.warm_jwt(sf) == {"keys": 0}
    assert strat._local_keyset is not None

    @asynccontextmanager
    async def bad_sf():
        async def execute(_stmt):
            raise OSError("down")

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(auth_mod, "_JWT_STRATEGY", auth_mod.JWTAuthStrategy())
    with pytest.raises(ConnectionError):
        await sw.warm_jwt(bad_sf)


def test_build_wires_settings():
    from app.config import Settings

    s = Settings(startup_warmup_timeout_sec=7, startup_warmup_db_conns=-1, db_pool_size=9)
    w = sw.build(s, redis=None, db_engine=None, hot_db_engine=None, session_factory=None,
                 routing_loader=None)
    assert [x.name for x in w.steps] == ["redis", "db_pool", "auth", "budget", "models", "jwt"]
    assert w.timeout_sec == 7 and w.enabled is True
    deps = {x.name: x.deps for x in w.steps}
    assert deps["auth"] == ("redis", "db_pool") and deps["budget"] == ("auth",)


# ── readiness + lifespan ──────────────────────────────────────────────────────


async def _ready(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/health/ready")


async def test_readiness_reports_degraded_warmup_but_stays_ready():
    a, _ = _step("db", [ConnectionError("down")])
    w = sw.StartupWarmup([a], timeout_sec=0.05)
    await w.run()
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=DegradationLevel.DB_DEGRADED)
    app.state.started = True
    app.state.shutting_down = False
    app.state.startup_warmup = w
    resp = await _ready(app)
    await w.stop()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready" and body["warmup"]["state"] == "degraded"
    assert body["warmup"]["steps"]["db"]["error"] == "ConnectionError"


def test_lifespan_warms_up_before_marking_started():
    src = (Path(__file__).resolve().parents[2] / "src" / "app" / "main.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    body = ast.get_source_segment(src, fn)
    y = body.index("yield")
    run = body.index("await startup_warmup.run()")
    started = body.index("app.state.started = True")
    # after every cache/pool it warms is configured, before readiness can say ready
    for dep in ("configure_auth_local_caches(_auth_caches)", "_model_cache.configure(_mcache)",
                "_budget_degraded.configure(settings)", "hot_db_engine = create_hot_db_engine"):
        assert body.index(dep) < run, dep
    assert run < started < y
    # shutdown stops the background warm-up and the recorder before Redis closes
    assert y < body.index("await startup_warmup.stop()") < body.index("await redis.aclose()")
    assert body.index("recent_vk_recorder.stop()") < body.index("await redis.aclose()")


# ── real Postgres: batch pre-load == request-path values ──────────────────────

_PROOF_DSN = os.environ.get("PROOF_DSN")


def _real_sf(conn):
    from sqlalchemy.ext.asyncio import AsyncSession

    class _SF:
        def __call__(self):
            return self

        async def __aenter__(self):
            self.s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            return self.s

        async def __aexit__(self, *a):
            await self.s.close()
            return False

    return _SF()


_U = {i: f"00000000-0000-4e1e-a000-0000000000{i:02d}" for i in range(1, 9)}
_ORG, _DEPT = "00000000-0000-4e1e-b000-000000000001", "00000000-0000-4e1e-b000-000000000002"
_T1, _T2 = "00000000-0000-4e1e-c000-000000000001", "00000000-0000-4e1e-c000-000000000002"


async def _seed(s):
    from sqlalchemy import text

    def ex(q, **p):
        return s.execute(text(q.replace("$", "CAST(:").replace("@", " AS uuid)")), p)

    await ex("INSERT INTO auth.organizations (id, name) VALUES ($o@, 'w1e')", o=_ORG)
    await ex("INSERT INTO auth.departments (id, org_id, name) VALUES ($d@, $o@, 'w1e')",
             d=_DEPT, o=_ORG)
    for t in (_T1, _T2):
        await ex("INSERT INTO auth.teams (id, dept_id, name) VALUES ($t@, $d@, :t)", t=t, d=_DEPT)
    users = {1: (_T1, "DEVELOPER", True), 2: (_T1, "DEVELOPER", True),
             3: (_T2, "TEAM_LEADER", True), 4: (None, "ADMIN", True),
             5: (_T1, "DEVELOPER", False), 7: (_T2, "DEVELOPER", True)}
    for i, (t, role, act) in users.items():
        await ex("INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject,"
                 " is_active) VALUES ($u@, $t@, :e, 'x', CAST(:r AS auth.user_role), :e, :a)",
                 u=_U[i], t=t, e=f"w1e-{i}@x", r=role, a=act)
    for a in ("w1e-a", "w1e-b", "w1e-c"):
        await ex("INSERT INTO model.model_aliases (alias, provider, provider_model_id, api_format,"
                 " created_by) VALUES (:a, 'BEDROCK', :a, 'BEDROCK_NATIVE', $u@)", a=a, u=_U[1])
    await ex("INSERT INTO model.user_allowed_models VALUES ($u@, 'w1e-a', $u@)", u=_U[1])
    await ex("INSERT INTO model.team_allowed_models VALUES ($t@, 'w1e-b', $u@),"
             " ($t@, 'w1e-c', $u@)", t=_T1, u=_U[1])
    await ex("INSERT INTO auth.user_allowed_clients VALUES ($u@, 'codex', $u@)", u=_U[1])
    await ex("INSERT INTO auth.team_allowed_clients VALUES ($t@, 'cowork', $u@)", t=_T1, u=_U[1])
    await ex("INSERT INTO auth.org_allowed_clients VALUES ($o@, 'claude-code', $u@)",
             o=_ORG, u=_U[1])
    # budgets: u1 total + per-app, u2 per-app only, team T1 total, team T2 none
    for scope, sid, client, amt in (("USER", _U[1], None, 50), ("USER", _U[1], "cowork", 10),
                                    ("USER", _U[2], "codex", 5), ("TEAM", _T1, None, 900)):
        await ex("INSERT INTO budget.budget_configs (scope, scope_id, client, max_budget_usd,"
                 " allocated_by, effective_from) VALUES (CAST(:sc AS budget.budget_scope), $s@,"
                 " :c, :m, $u@, date '2026-01-01')", sc=scope, s=sid, c=client, m=amt, u=_U[1])
    await s.commit()  # releases the savepoint into the outer (rolled back) transaction


@pytest.mark.skipif(not _PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")
async def test_real_pg_auth_preload_matches_request_path():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.auth import User

    engine = create_async_engine(_PROOF_DSN)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            sf = _real_sf(conn)
            async with sf() as s:
                await _seed(s)
            pairs = [(f"h{i}", _U[i]) for i in (1, 2, 3, 4, 5, 6, 7)] + [("h1b", _U[1])]
            async with sf() as s:
                ctxs, active = await auth_mod.preload_vk_contexts(s, pairs, chunk=3)
            assert active == {_U[1]: True, _U[2]: True, _U[3]: True, _U[4]: True,
                              _U[5]: False, _U[6]: False, _U[7]: True}
            assert set(ctxs) == {"h1", "h1b", "h2", "h3", "h4", "h7"}
            for h, uid in pairs:
                if h not in ctxs:
                    continue
                async with sf() as s:
                    user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
                    am, ac = await auth_mod._resolve_entitlements(
                        s, uid, str(user.team_id) if user.team_id else None)
                    want = auth_mod._vk_auth_context(user, am, ac, None)
                got = ctxs[h]
                assert got.model_dump(exclude={"allowed_models", "allowed_clients"}) == \
                    want.model_dump(exclude={"allowed_models", "allowed_clients"}), h
                norm = lambda v: None if v is None else sorted(v)  # noqa: E731
                assert norm(got.allowed_models) == norm(want.allowed_models), h
                assert norm(got.allowed_clients) == norm(want.allowed_clients), h
            # the cases the rules distinguish
            assert ctxs["h1"].allowed_models == ["w1e-a"]
            assert ctxs["h1"].allowed_clients == ["codex"]
            assert sorted(ctxs["h2"].allowed_models) == ["w1e-b", "w1e-c"]
            assert ctxs["h2"].allowed_clients == ["cowork"]
            assert ctxs["h3"].allowed_models is None
            assert ctxs["h3"].allowed_clients == ["claude-code"]
            assert ctxs["h3"].roles == [Role.TEAM_LEADER] and ctxs["h4"].roles == [Role.USER]
            assert ctxs["h4"].team_id == "" and ctxs["h4"].allowed_clients is None
            await trans.rollback()
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")
async def test_real_pg_budget_preload_matches_background_refresh():
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.services import budget_degraded as bd
    from app.services.budget_service import BudgetService

    engine = create_async_engine(_PROOF_DSN)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            sf = _real_sf(conn)
            async with sf() as s:
                await _seed(s)
            uids, tids = [_U[1], _U[2], _U[3]], [_T1, _T2]
            svc = BudgetService()
            async with sf() as s:
                for u in uids:
                    await svc._load_user_policy(s, u)
                for t in tids:
                    await svc._load_team_policy(s, t)
            want = dict((k, v[1]) for k, v in bd.DEGRADED.policy.cache._d.items())
            bd.DEGRADED.reset()
            async with sf() as s:
                n = await BudgetService.preload_policies(s, uids, tids, chunk=2)
            got = dict((k, v[1]) for k, v in bd.DEGRADED.policy.cache._d.items())
            assert n == 5
            assert got == want
            assert got[("team", _T2)] is bd.ABSENT and got[("user", _U[2])] is bd.ABSENT
            assert got[("client", _U[2], "codex")]["limit_usd"] in (5, "5", "5.0000", 5.0)
            await trans.rollback()
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")
async def test_real_pg_open_pool_leaves_n_idle_connections():
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_PROOF_DSN, pool_size=4, max_overflow=0)
    try:
        assert await sw.open_pool(engine, 4) == 4
        assert engine.pool.checkedin() == 4 and engine.pool.checkedout() == 0
    finally:
        await engine.dispose()
