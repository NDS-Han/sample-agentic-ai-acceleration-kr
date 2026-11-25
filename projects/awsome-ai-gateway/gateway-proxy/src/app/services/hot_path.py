# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Hot-path isolation helpers — keep slow DB/Redis from slowing or failing proxy requests.

Pieces (all per process, no shared infra):

- ``DbBreaker``: after a hot-path DB call times out or errors, skip the DB for a short
  cooldown so a slow database costs one bounded wait per cooldown, not one per request.
  It also honours the health checker's degradation level (DB_DEGRADED / BOTH_DEGRADED).
- ``run_db``: run ``fn(session)`` on the dedicated hot-path pool under ``asyncio.timeout``.
  Any timeout / DB error raises ``HotDbUnavailable`` (and opens the breaker) — callers
  fall back to their last-known-good cache instead of waiting.
- ``LocalCache``: small LRU + TTL cache that can also serve stale values (``get_stale``)
  for fail-to-cache.
- ``AuthInvalidationListener``: subscribes to ``auth_invalidation_channel`` and evicts
  local auth entries when admin-api changes a user/key. If pub/sub is down the TTL is the
  fallback (entries are re-validated after ``auth_local_cache_ttl_sec``).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

import structlog
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import MaxConnectionsError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = structlog.get_logger(__name__)

T = TypeVar("T")
V = TypeVar("V")


class HotDbUnavailable(Exception):
    """The hot-path DB call was skipped (breaker/degraded) or did not finish in time."""


class DbBreaker:
    """Per-process 'backend is slow' window + degradation-level gate.

    Used for the DB (``DB_BREAKER``) and for Redis (``REDIS_BREAKER``). One infrastructure
    failure opens it for ``cooldown_sec``; after that the next call tries the backend again.
    """

    def __init__(self, cooldown_sec: float = 2.0,
                 clock: Callable[[], float] = time.monotonic, *, name: str = "db") -> None:
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._open_until = 0.0
        self._dm = None
        self.opened_total = 0
        self.name = name

    def attach(self, degradation_manager) -> None:
        self._dm = degradation_manager

    def available(self) -> bool:
        if self._dm is not None:
            from app.schemas.domain import DegradationLevel

            if self._dm.level in (DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED):
                return False
        return self._clock() >= self._open_until

    def trip(self, reason: str) -> None:
        now = self._clock()
        if now >= self._open_until:
            self.opened_total += 1
            logger.warning(f"hot_path_{self.name}_breaker_open", reason=reason,
                           cooldown_sec=self.cooldown_sec)
        self._open_until = now + self.cooldown_sec

    def reset(self) -> None:
        self._open_until = 0.0

    def record_success(self) -> None:
        """The backend answered. No-op here (the DB breaker closes by time alone)."""

    def is_open(self) -> bool:
        """Pure check (never grants a probe)."""
        return not DbBreaker.available(self)


class RedisBreaker(DbBreaker):
    """``DbBreaker`` + single-probe half-open state (p1-hotpath-redis).

    With the plain cooldown every request that starts in the instant after the cooldown tries
    Redis again; under a Redis *hang* each of them then waits its full deadline (the budget
    reservation path alone has 3 s + 4 s + refund deadlines). Here, after the cooldown exactly one
    caller of ``available()`` gets through (the probe); everyone else still sees the breaker
    open until the probe reports ``record_success()`` (→ closed) or fails (``trip`` → open again).
    A probe that never reports frees the slot after another ``cooldown_sec``.
    ``is_open()`` is the side-effect-free check for "should I use the fallback?".
    """

    def __init__(self, cooldown_sec: float = 2.0,
                 clock: Callable[[], float] = time.monotonic, *, name: str = "redis") -> None:
        super().__init__(cooldown_sec, clock, name=name)
        self._tripped = False
        self._probe_until = 0.0

    def available(self) -> bool:
        now = self._clock()
        if now < self._open_until:
            return False
        if not self._tripped:
            return True
        if now < self._probe_until:
            return False  # a probe is in flight
        self._probe_until = now + self.cooldown_sec
        return True  # this caller is the probe

    def is_open(self) -> bool:
        # open, or half-open (probe pending or in flight) — only a reported success closes it.
        return self._tripped or self._clock() < self._open_until

    def trip(self, reason: str) -> None:
        super().trip(reason)
        self._tripped = True
        self._probe_until = 0.0

    def record_success(self) -> None:
        # Only after the cooldown: a late success of a call that started before the trip must
        # not close the breaker while other calls are still timing out.
        if self._tripped and self._clock() >= self._open_until:
            self._tripped = False
            self._probe_until = 0.0
            logger.info(f"hot_path_{self.name}_breaker_closed")

    def reset(self) -> None:
        super().reset()
        self._tripped = False
        self._probe_until = 0.0


#: Process-wide breaker shared by auth, downgrade and web-search lookups.
DB_BREAKER = DbBreaker()

#: Process-wide Redis breaker (p1-hotpath-redis) shared by the request-path Redis users:
#: budget check (``budget_degraded.DEGRADED.redis_breaker``), model cache, rate limits
#: (config reads + Lua), VK auth (when a local fallback exists) and cost lifecycle events.
#: Not gated on the health checker's level — callers that care check the level themselves.
REDIS_BREAKER = RedisBreaker(name="redis")


class HotRedisUnavailable(RedisConnectionError):
    """A request-path Redis call was skipped because ``REDIS_BREAKER`` is open.

    Subclass of redis ``ConnectionError`` so existing ``except RedisError`` fallbacks apply.
    """


def is_redis_infra_error(exc: BaseException) -> bool:
    """Timeout / connection-level failure that should open ``REDIS_BREAKER``.

    Pool exhaustion (``MaxConnectionsError``) is local back-pressure, not a Redis outage;
    a skipped call (``HotRedisUnavailable``) must not extend the window by itself.
    """
    if isinstance(exc, (HotRedisUnavailable, MaxConnectionsError)):
        return False
    return isinstance(exc, (RedisTimeoutError, RedisConnectionError, TimeoutError, OSError))


async def run_redis(call: Callable[[], Awaitable[T]], *, timeout: float | None,
                    what: str = "redis", breaker: DbBreaker | None = None) -> T:
    """Run one request-path Redis call under ``REDIS_BREAKER`` and a deadline.

    Breaker open → ``HotRedisUnavailable`` at once. Timeout / connection error → trip and re-raise.
    Other errors (``ResponseError`` from a script, bad data) re-raise without tripping.
    """
    br = breaker or REDIS_BREAKER
    if not br.available():
        raise HotRedisUnavailable(f"{what}: redis breaker open")
    try:
        if timeout is not None and timeout > 0:
            async with asyncio.timeout(timeout):
                out = await call()
        else:
            out = await call()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if is_redis_infra_error(e):
            br.trip(f"{what}:{type(e).__name__}")
        elif isinstance(e, ResponseError):
            br.record_success()  # Redis answered (a script/data error is not an outage)
        raise
    br.record_success()
    return out

#: Dedicated hot-path session factory (set at startup). Falls back to the caller's factory.
_HOT_SESSION_FACTORY = None


def configure(*, session_factory=None, degradation_manager=None,
              cooldown_sec: float | None = None,
              redis_cooldown_sec: float | None = None) -> None:
    global _HOT_SESSION_FACTORY
    if session_factory is not None:
        _HOT_SESSION_FACTORY = session_factory
    if degradation_manager is not None:
        DB_BREAKER.attach(degradation_manager)
    if cooldown_sec is not None:
        DB_BREAKER.cooldown_sec = cooldown_sec
    if redis_cooldown_sec is not None:
        REDIS_BREAKER.cooldown_sec = redis_cooldown_sec


def hot_session_factory(fallback=None):
    return _HOT_SESSION_FACTORY or fallback


async def run_db(
    fn: Callable[[Any], Awaitable[T]],
    *,
    timeout: float,
    session_factory=None,
    what: str = "db",
) -> T:
    """Run ``fn(session)`` with a hard deadline. Raises ``HotDbUnavailable`` instead of waiting."""
    sf = hot_session_factory(session_factory)
    if sf is None or not DB_BREAKER.available():
        raise HotDbUnavailable(f"{what}: db unavailable (degraded or breaker open)")
    try:
        async with asyncio.timeout(timeout):
            async with sf() as session:
                return await fn(session)
    except HotDbUnavailable:
        raise
    except TimeoutError as e:
        DB_BREAKER.trip(f"{what}:timeout")
        raise HotDbUnavailable(f"{what}: timed out after {timeout}s") from e
    except asyncio.CancelledError:
        raise
    except Exception as e:  # SQLAlchemyError, OSError, asyncpg errors ...
        # Permission-style errors raised by fn itself are not infrastructure failures.
        if isinstance(e, PermissionError):
            raise
        DB_BREAKER.trip(f"{what}:{type(e).__name__}")
        raise HotDbUnavailable(f"{what}: {type(e).__name__}") from e


class LocalCache(Generic[V]):
    """LRU + TTL. ``get_fresh`` within ttl; ``get_stale`` within max_stale (fail-to-cache)."""

    def __init__(self, ttl: float, max_stale: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_stale = max(max_stale, ttl)
        self.max_entries = max(1, max_entries)
        self._clock = clock
        self._d: OrderedDict[Any, tuple[float, V]] = OrderedDict()

    def put(self, key, value: V) -> None:
        self._d[key] = (self._clock(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)

    def _get(self, key, limit: float) -> V | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        if self._clock() - hit[0] > limit:
            if self._clock() - hit[0] > self.max_stale:
                self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return hit[1]

    def get_fresh(self, key) -> V | None:
        return self._get(key, self.ttl)

    def get_stale(self, key) -> V | None:
        return self._get(key, self.max_stale)

    def age(self, key) -> float | None:
        hit = self._d.get(key)
        return None if hit is None else self._clock() - hit[0]

    def evict(self, key) -> None:
        self._d.pop(key, None)

    def evict_where(self, pred: Callable[[Any, V], bool]) -> int:
        doomed = [k for k, (_, v) in self._d.items() if pred(k, v)]
        for k in doomed:
            self._d.pop(k, None)
        return len(doomed)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


# ── auth invalidation (admin-api → gateway) ───────────────────────────────────


class AuthInvalidationListener:
    """Redis pub/sub subscriber that evicts local auth entries.

    Message (JSON): ``{"user_ids": [...], "key_hashes": [...], "all": false}``.
    Uses a plain (non-cluster) connection to ``redis_url``: classic PUBLISH is broadcast to
    every node of a Redis/Valkey cluster, so one node subscription sees all messages.
    """

    def __init__(self, redis_url: str, channel: str, on_message: Callable[[dict], None], *,
                 socket_connect_timeout: float = 1.0) -> None:
        self._url = redis_url
        self._channel = channel
        self._on_message = on_message
        self._connect_timeout = socket_connect_timeout
        self._task: asyncio.Task | None = None
        self.connected = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auth-invalidation-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        from redis.asyncio import Redis

        backoff = 1.0
        while True:
            client = None
            try:
                client = Redis.from_url(self._url, socket_connect_timeout=self._connect_timeout,
                                        socket_keepalive=True, health_check_interval=30)
                pubsub = client.pubsub()
                await pubsub.subscribe(self._channel)
                self.connected = True
                backoff = 1.0
                logger.info("auth_invalidation_subscribed", channel=self._channel)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = msg.get("data")
                        payload = json.loads(data.decode() if isinstance(data, bytes) else data)
                        if isinstance(payload, dict):
                            self._on_message(payload)
                    except Exception:
                        logger.warning("auth_invalidation_bad_message")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("auth_invalidation_listener_down", error=type(e).__name__,
                               retry_in=backoff)
            finally:
                self.connected = False
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


_INFLIGHT: dict[str, asyncio.Task] = {}


def single_flight(key: str, factory: Callable[[], Awaitable[Any]]) -> bool:
    """Start ``factory()`` in the background unless a task for ``key`` is already running.

    Returns True if a new task was started.
    """
    t = _INFLIGHT.get(key)
    if t is not None and not t.done():
        return False

    async def _wrapped():
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("single_flight_task_failed", key=key, exc_info=True)
        finally:
            _INFLIGHT.pop(key, None)

    from app.services.streaming import _spawn

    task = _spawn(_wrapped())
    if task is not None:
        _INFLIGHT[key] = task
    return True
