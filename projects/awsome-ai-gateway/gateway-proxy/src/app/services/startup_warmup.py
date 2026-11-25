# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Startup warm-up — fill pools and caches before this process takes traffic (wave 1e).

Why: a new gateway process used to start with empty pools and caches. Its first requests opened
the first DB connections (over RDS Proxy: TLS + proxy auth, often > 1 s) inside the 0.3 s hot-path
deadline, timed out, opened the process DB breaker and answered 503 for 2–4 s; first-seen VKs had
no local auth fallback either.

What runs (per uvicorn worker, inside the lifespan, before ``app.state.started``). uvicorn does not
accept connections on a worker until its lifespan startup returns, so a worker that is still
warming gets no traffic and readiness only ever sees warmed workers:

1. ``redis``    — ``startup_warmup_redis_conns`` concurrent PINGs (opens pool connections).
2. ``db_pool``  — opens the whole hot-path (auth) pool and ``startup_warmup_db_conns`` main-pool
                  connections at the same time, ``SELECT 1`` on each, returns them to the pool.
3. ``auth``     — recently used VK hashes (``recent_vk``) → ``key:vk:{hash}`` mapping in Redis →
                  users + entitlements in batches → ``AuthLocalCaches`` (``active`` and ``ctx``),
                  exactly the values the request path would build.
4. ``budget``   — those users' and teams' budget configs in batches → 1c policy cache.
5. ``models``   — every ACTIVE alias / provider model id through the same ``RouterService`` calls
                  the routers make, the model list, and every routing profile → 1d model cache.
6. ``jwt``      — active JWT keyset → the process-local copy.

Bounded: the lifespan waits at most ``startup_warmup_timeout_sec``. Failed steps are retried
(back-off 1 s doubling to ``startup_warmup_retry_max_sec``); steps whose inputs failed wait. If
the window ends first (DB/Redis down or slow) the process becomes ready anyway — readiness stays
process-local (wave 1), ``/health/ready`` reports ``warmup.state = "degraded"`` — and the same
task keeps warming in the background until every step succeeded. A DB outage therefore never
keeps pods out of the load balancer; it only means they start cold, as before.

Warm-up reads use the main pool with their own step deadline, never the request-path helpers
that open the hot-path DB breaker — except ``models``, which deliberately goes through the real
router code (with ``redis=None`` so values come from the DB) to fill exactly the request keys.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

OK = "ok"
FAILED = "failed"
PENDING = "pending"
WAITING = "waiting"  # an input step has not succeeded yet


@dataclass
class Step:
    name: str
    fn: Callable[[], Awaitable[dict | None]]
    deps: tuple[str, ...] = ()
    status: str = PENDING
    error: str | None = None
    detail: dict | None = None
    sec: float | None = None
    attempts: int = 0


@dataclass
class _Shared:
    """Values one step hands to the next (auth → budget)."""

    users: dict[str, str | None] = field(default_factory=dict)


class StartupWarmup:
    def __init__(self, steps: list[Step], *, timeout_sec: float = 20.0,
                 step_timeout_sec: float = 10.0, retry_max_sec: float = 30.0,
                 enabled: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.steps = steps
        self._by = {s.name: s for s in steps}
        self.timeout_sec = timeout_sec
        self.step_timeout_sec = step_timeout_sec
        self.retry_max_sec = retry_max_sec
        self.enabled = enabled
        self._clock = clock
        self._task: asyncio.Task | None = None
        self.started_at: float | None = None
        self.ready_at: float | None = None       # when the lifespan stopped waiting
        self.completed_at: float | None = None   # when every step had succeeded
        self.degraded = False
        self.passes = 0

    @property
    def complete(self) -> bool:
        return all(s.status == OK for s in self.steps)

    # ── execution ─────────────────────────────────────────────────────────────
    async def _run_step(self, st: Step) -> None:
        st.attempts += 1
        t0 = self._clock()
        try:
            async with asyncio.timeout(self.step_timeout_sec):
                st.detail = await st.fn()
            st.status, st.error = OK, None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st.status, st.error = FAILED, type(e).__name__
            logger.warning("startup_warmup_step_failed", step=st.name, attempt=st.attempts,
                           error=f"{type(e).__name__}: {e}"[:300])
        st.sec = round(self._clock() - t0, 3)

    async def _pass(self) -> None:
        self.passes += 1
        for st in self.steps:
            if st.status == OK:
                continue
            if any(self._by[d].status != OK for d in st.deps if d in self._by):
                st.status = WAITING
                continue
            await self._run_step(st)

    async def _loop(self) -> None:
        delay = 1.0
        while True:
            await self._pass()
            if self.complete:
                self.completed_at = self._clock()
                elapsed = round(self.completed_at - (self.started_at or self.completed_at), 2)
                if self.degraded:
                    logger.info("startup_warmup_completed_in_background", elapsed_sec=elapsed,
                                steps=self._summary())
                else:
                    logger.info("startup_warmup_complete", elapsed_sec=elapsed,
                                steps=self._summary())
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry_max_sec)

    async def run(self) -> bool:
        """Warm up; return True when complete within the window, else False (degraded, the
        background task keeps going)."""
        self.started_at = self._clock()
        if not self.enabled or not self.steps:
            self.ready_at = self.completed_at = self._clock()
            return True
        self._task = asyncio.create_task(self._loop(), name="startup-warmup")
        done, _ = await asyncio.wait({self._task}, timeout=self.timeout_sec)
        self.ready_at = self._clock()
        if self._task in done and self._task.exception() is None and self.complete:
            return True
        if self._task in done and self._task.exception() is not None:
            logger.error("startup_warmup_crashed", error=repr(self._task.exception()))
        self.degraded = True
        logger.warning("startup_warmup_degraded_ready",
                       waited_sec=round(self.ready_at - self.started_at, 2),
                       timeout_sec=self.timeout_sec, steps=self._summary())
        return False

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ── reporting (readiness body) ────────────────────────────────────────────
    def _summary(self) -> dict:
        return {s.name: s.status for s in self.steps}

    def snapshot(self) -> dict:
        if not self.enabled:
            return {"state": "disabled"}
        if self.complete:
            state = "warm"
        elif self.ready_at is None:
            state = "warming"
        else:
            state = "degraded"
        out: dict[str, Any] = {
            "state": state,
            "background": self.degraded,
            "steps": {
                s.name: {k: v for k, v in (("status", s.status), ("error", s.error),
                                           ("sec", s.sec), ("attempts", s.attempts),
                                           ("detail", s.detail)) if v is not None}
                for s in self.steps
            },
        }
        if self.started_at is not None and self.ready_at is not None:
            out["ready_after_sec"] = round(self.ready_at - self.started_at, 2)
        if self.started_at is not None and self.completed_at is not None:
            out["complete_after_sec"] = round(self.completed_at - self.started_at, 2)
        return out


# ── steps ─────────────────────────────────────────────────────────────────────


async def warm_redis(redis, n: int) -> dict:
    await asyncio.gather(*(redis.ping() for _ in range(max(1, n))))
    return {"conns": max(1, n)}


async def open_pool(engine, n: int) -> int:
    """Check out ``n`` connections at once (so they are distinct), SELECT 1, return them."""
    from sqlalchemy import text

    if engine is None or n <= 0:
        return 0
    conns: list = []

    async def one():
        c = await engine.connect()
        conns.append(c)
        await c.execute(text("SELECT 1"))

    try:
        results = await asyncio.gather(*(one() for _ in range(n)), return_exceptions=True)
    finally:
        for c in conns:
            try:
                await c.close()
            except Exception:
                pass
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise errors[0]
    return n


async def warm_db_pools(hot_engine, hot_n: int, main_engine, main_n: int) -> dict:
    hot, main = await asyncio.gather(open_pool(hot_engine, hot_n), open_pool(main_engine, main_n))
    return {"hot": hot, "main": main}


async def warm_auth(redis, session_factory, *, recent_key: str, window_sec: float,
                    max_keys: int, shared: _Shared) -> dict:
    from app.services import auth_service, recent_vk

    caches = auth_service.AUTH_LOCAL_CACHES
    if caches is None:
        return {"skipped": "auth local caches off"}
    hashes = await recent_vk.recent_hashes(redis, recent_key, window_sec=window_sec,
                                           limit=max_keys)
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        pipe = redis.pipeline(transaction=False)
        for h in part:
            pipe.get(f"key:vk:{h}")
        for h, v in zip(part, await pipe.execute()):
            if not v:  # no mapping = revoked / expired key
                continue
            try:  # one malformed mapping must not fail the whole batch query
                pairs.append((h, str(uuid.UUID(v.decode() if isinstance(v, bytes) else str(v)))))
            except ValueError:
                continue
    gen = caches.generation
    async with session_factory() as s:
        contexts, active = await auth_service.preload_vk_contexts(s, pairs)
    if caches.generation != gen:
        # an invalidation arrived while we read — our values may predate it; retry the step.
        raise RuntimeError("auth invalidated during preload")
    for uid, ok in active.items():
        caches.active.put(uid, ok)
    for h, _uid in reversed(pairs):  # oldest first → newest end up most recently used
        ctx = contexts.get(h)
        if ctx is not None:
            caches.ctx.put(h, ctx)
    shared.users = {c.user_id: (c.team_id or None) for c in contexts.values()}
    return {"recent": len(hashes), "mapped": len(pairs), "users": len(active),
            "contexts": len(contexts)}


async def warm_budget(session_factory, shared: _Shared) -> dict:
    from app.services.budget_service import BudgetService

    if not shared.users:
        return {"policies": 0}
    async with session_factory() as s:
        n = await BudgetService.preload_policies(
            s, list(shared.users), [t for t in shared.users.values() if t]
        )
    return {"policies": n}


def _resolvers(rs, provider) -> list[Callable[..., Awaitable[Any]]]:
    from app.schemas.domain import ProviderType
    from app.services.router_service import OPENAI_CHAT_PROVIDERS, OPENAI_RESPONSES_PROVIDERS

    try:
        p = ProviderType(provider)
    except ValueError:
        return []
    out: list = []
    if p == ProviderType.BEDROCK:
        out.append(rs.resolve_bedrock_model)
    if p in (ProviderType.BEDROCK_MANTLE, ProviderType.BEDROCK_MANTLE_OPENAI):
        out.append(lambda r, d, ref, _p=p: rs.resolve_mantle_model(r, d, ref, expected_provider=_p))
    if p in OPENAI_RESPONSES_PROVIDERS:
        out.append(rs.resolve_codex_model)
    if p in OPENAI_CHAT_PROVIDERS:
        out.append(rs.resolve_openai_model)
    return out


async def warm_models(session_factory, routing_loader, *, max_aliases: int,
                      concurrency: int = 4) -> dict:
    from sqlalchemy import select

    from app.models.model import ModelAlias
    from app.models.routing import RoutingProfile
    from app.services import model_cache
    from app.services.model_cache import ModelBackendUnavailableError
    from app.services.router_service import RouterService

    cache = model_cache.get()
    if cache is None:
        return {"skipped": "model cache disabled"}
    async with session_factory() as s:
        rows = (await s.execute(
            select(ModelAlias.alias, ModelAlias.provider_model_id, ModelAlias.provider)
            .where(ModelAlias.status == "ACTIVE").order_by(ModelAlias.alias).limit(max_aliases)
        )).all()
        clients = list((await s.execute(
            select(RoutingProfile.client).order_by(RoutingProfile.client).limit(200)
        )).scalars().all())

    rs = RouterService()
    calls: list[Callable[[Any], Awaitable[Any]]] = []
    for alias, pmid, provider in rows:
        for ref in dict.fromkeys(r for r in (alias, pmid) if r):
            calls.append(lambda d, ref=ref: rs.alias_provider(None, d, ref))
            for fn in _resolvers(rs, provider):
                calls.append(lambda d, ref=ref, fn=fn: fn(None, d, ref))
    calls.append(lambda d: rs.list_active_models(None, d))
    if routing_loader is not None:
        for c in clients:
            calls.append(lambda d, c=c: routing_loader.load(None, d, c))

    # The cache reads through its own session factory (short sessions); the session passed here
    # only signals "DB allowed". Without a factory the shared session forces sequential calls.
    sem = asyncio.Semaphore(concurrency if cache.session_factory is not None else 1)
    failed = 0

    async with session_factory() as s:
        async def one(call):
            nonlocal failed
            async with sem:
                try:
                    await call(s)
                except LookupError:
                    pass  # unknown / inactive / other plane — a valid cached answer
                except ModelBackendUnavailableError:
                    failed += 1

        await asyncio.gather(*(one(c) for c in calls))
    if failed:
        raise ModelBackendUnavailableError(f"{failed}/{len(calls)} model lookups failed")
    return {"aliases": len(rows), "lookups": len(calls), "profiles": len(clients),
            "entries": len(cache.cache)}


async def warm_jwt(session_factory) -> dict:
    from app.services import auth_service

    async with session_factory() as s:
        keys = await auth_service._JWT_STRATEGY._get_active_keyset(None, s)
    if keys is None:
        raise ConnectionError("jwt keyset unavailable")
    return {"keys": len(keys)}


def build(settings, *, redis, db_engine, hot_db_engine, session_factory,
          routing_loader) -> StartupWarmup:
    shared = _Shared()
    main_n = settings.db_pool_size if settings.startup_warmup_db_conns < 0 \
        else settings.startup_warmup_db_conns
    steps = [
        Step("redis", lambda: warm_redis(redis, settings.startup_warmup_redis_conns)),
        Step("db_pool", lambda: warm_db_pools(hot_db_engine, settings.hot_db_pool_size,
                                              db_engine, main_n)),
        Step("auth", lambda: warm_auth(
            redis, session_factory, recent_key=settings.auth_recent_vk_key,
            window_sec=settings.startup_warmup_auth_window_sec,
            max_keys=settings.startup_warmup_auth_max_keys, shared=shared,
        ), deps=("redis", "db_pool")),
        Step("budget", lambda: warm_budget(session_factory, shared), deps=("auth",)),
        Step("models", lambda: warm_models(
            session_factory, routing_loader, max_aliases=settings.startup_warmup_model_max_aliases,
        ), deps=("db_pool",)),
        Step("jwt", lambda: warm_jwt(session_factory), deps=("db_pool",)),
    ]
    return StartupWarmup(
        steps,
        timeout_sec=settings.startup_warmup_timeout_sec,
        step_timeout_sec=settings.startup_warmup_step_timeout_sec,
        retry_max_sec=settings.startup_warmup_retry_max_sec,
        enabled=settings.startup_warmup_enabled,
    )
