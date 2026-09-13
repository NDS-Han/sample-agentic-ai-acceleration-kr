# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import structlog
from sqlalchemy import text

logger = structlog.get_logger(__name__)

# Redis key written by admin-api (write-through) and read here. String "1"/"0".
BODY_LOG_REDIS_KEY = "bodylog:enabled"

# Raw read of the admin-mutable flag. gateway-proxy has no ORM model for
# public.system_settings (admin-api owns it); a scalar read is enough.
_DB_QUERY = text(
    "SELECT value FROM public.system_settings WHERE key = 'body_logging_enabled'"
)


class BodyLogFlag:
    """Runtime on/off flag for body-logging, cached in-process with a short TTL.

    Source of truth is Redis key ``bodylog:enabled`` (written by admin-api).
    On a Redis miss the flag is rehydrated from ``public.system_settings`` (DB is
    the durable source) and Redis is re-populated. The in-process cache means the
    hot path reads a boolean from memory and only touches Redis once per TTL
    window (per worker), so a flipped toggle takes effect within ``ttl`` seconds
    without a per-request Redis call.

    Fails safe: any Redis/DB error, or a fully degraded call (redis and
    session_factory both None), returns ``default`` (OFF).
    """

    def __init__(
        self,
        *,
        ttl: float = 5.0,
        default: bool = False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl
        self._default = default
        self._now = now
        self._value = default
        self._expires_at = 0.0  # epoch seconds; 0 forces a first refresh
        self._lock = asyncio.Lock()

    async def is_enabled(self, redis, session_factory) -> bool:
        now = self._now()
        # Fast path: cached value still fresh.
        if now < self._expires_at:
            return self._value

        async with self._lock:
            # Re-check under lock (another coroutine may have refreshed).
            now = self._now()
            if now < self._expires_at:
                return self._value

            value = await self._refresh(redis, session_factory)
            self._value = value
            self._expires_at = now + self._ttl
            return value

    async def _refresh(self, redis, session_factory) -> bool:
        # 1) Redis (source of truth in the hot path).
        if redis is not None:
            try:
                raw = await redis.get(BODY_LOG_REDIS_KEY)
            except Exception:
                logger.warning("body_log_flag_redis_read_failed")
                return self._default
            if raw is not None:
                return _truthy(raw)
            # 2) Redis miss → rehydrate from DB, re-populate Redis.
            return await self._hydrate_from_db(redis, session_factory)

        # No Redis: try DB directly, else default.
        if session_factory is not None:
            return await self._hydrate_from_db(redis, session_factory)
        return self._default

    async def _hydrate_from_db(self, redis, session_factory) -> bool:
        if session_factory is None:
            return self._default
        try:
            async with session_factory() as session:
                result = await session.execute(_DB_QUERY)
                row = result.scalar_one_or_none()
        except Exception:
            logger.warning("body_log_flag_db_read_failed")
            return self._default

        enabled = bool(row) if row is not None else self._default
        if redis is not None:
            try:
                await redis.set(BODY_LOG_REDIS_KEY, "1" if enabled else "0")
            except Exception:
                logger.warning("body_log_flag_redis_write_failed")
        return enabled


def _truthy(raw) -> bool:
    """Interpret a Redis value as the flag. Accepts bytes/str; '1'/'true' = on."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
