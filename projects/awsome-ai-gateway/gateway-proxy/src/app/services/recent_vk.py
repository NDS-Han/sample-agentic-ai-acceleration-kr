# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Recently used virtual keys — the list a new gateway process pre-loads at startup (wave 1e).

The VK → user mapping lives only in Redis (``key:vk:{sha256}``); the raw key is never stored and
``usage_logs`` has only the 11-char prefix. So the gateway itself remembers which keys were used:
on a successful VK authentication the key's **sha256 hash** (the same value that already names
``key:vk:{hash}``, so nothing new is exposed) goes into one Redis sorted set,
``auth_recent_vk_key`` (score = unix time of last use).

Cost: per process, each key is noted at most once per ``record_interval_sec``; noted hashes are
written in one ``ZADD … GT`` per ``flush_interval_sec`` from a background task (never on the
request path). The set is trimmed to ``window_sec`` and ``max_keys`` and expires when no gateway
writes to it. Redis errors only delay the next flush.

At startup ``startup_warmup`` reads the newest ``startup_warmup_auth_max_keys`` hashes used within
``startup_warmup_auth_window_sec`` and pre-loads their users into the in-process auth caches.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)


class RecentVkRecorder:
    def __init__(self, redis, *, key: str, record_interval_sec: float = 300.0,
                 flush_interval_sec: float = 10.0, window_sec: float = 86400.0,
                 max_keys: int = 20000, trim_interval_sec: float = 60.0,
                 redis_timeout_sec: float = 1.0, max_pending: int = 50000,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.redis = redis
        self.key = key
        self.record_interval_sec = record_interval_sec
        self.flush_interval_sec = flush_interval_sec
        self.window_sec = window_sec
        self.max_keys = max(1, max_keys)
        self.trim_interval_sec = trim_interval_sec
        self.redis_timeout_sec = redis_timeout_sec
        self.max_pending = max_pending
        self._clock = clock
        self._wall = wall
        self._last_noted: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._last_trim = -1e18
        self._task: asyncio.Task | None = None
        self.dropped = 0

    # ── request path (sync, O(1)) ─────────────────────────────────────────────
    def note(self, key_hash: str) -> None:
        now = self._clock()
        last = self._last_noted.get(key_hash)
        if last is not None and now - last < self.record_interval_sec:
            return
        if len(self._last_noted) >= 4 * self.max_pending:
            self._last_noted.clear()
        self._last_noted[key_hash] = now
        if key_hash not in self._pending and len(self._pending) >= self.max_pending:
            self.dropped += 1
            return
        self._pending[key_hash] = self._wall()

    # ── background ────────────────────────────────────────────────────────────
    async def flush(self) -> int:
        """Write pending hashes (one ZADD per 1000) and trim now and then. Returns written count."""
        if self.redis is None:
            return 0
        batch, self._pending = self._pending, {}
        written = 0
        try:
            async with asyncio.timeout(self.redis_timeout_sec):
                items = list(batch.items())
                for i in range(0, len(items), 1000):
                    await self.redis.zadd(self.key, dict(items[i:i + 1000]), gt=True)
                    written += len(items[i:i + 1000])
                if items:
                    await self.redis.expire(self.key, int(self.window_sec) + 3600)
                now = self._clock()
                if now - self._last_trim >= self.trim_interval_sec:
                    self._last_trim = now
                    await self.redis.zremrangebyscore(self.key, "-inf",
                                                      self._wall() - self.window_sec)
                    await self.redis.zremrangebyrank(self.key, 0, -(self.max_keys + 1))
        except asyncio.CancelledError:
            self._restore(batch, written)
            raise
        except Exception as e:  # RedisError, TimeoutError, OSError — retry next tick
            self._restore(batch, written)
            logger.info("recent_vk_flush_failed", error=type(e).__name__,
                        pending=len(self._pending))
        return written

    def _restore(self, batch: dict[str, float], written: int) -> None:
        rest = list(batch.items())[written:]
        for h, ts in rest:
            if len(self._pending) >= self.max_pending:
                self.dropped += 1
                continue
            self._pending.setdefault(h, ts)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_sec)
            await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="recent-vk-recorder")

    async def stop(self, *, final_flush: bool = True) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if final_flush and self._pending:
            await self.flush()


#: Set by the app lifespan; None = not recording (tests, disabled).
RECORDER: RecentVkRecorder | None = None


def configure(recorder: RecentVkRecorder | None) -> None:
    global RECORDER
    RECORDER = recorder


def note(key_hash: str) -> None:
    """Called after a successful VK authentication. Never raises."""
    r = RECORDER
    if r is None:
        return
    try:
        r.note(key_hash)
    except Exception:
        pass


async def recent_hashes(redis, key: str, *, window_sec: float, limit: int,
                        wall: Callable[[], float] = time.time) -> list[str]:
    """Newest first, used within ``window_sec``, at most ``limit``."""
    if limit <= 0:
        return []
    raw = await redis.zrevrangebyscore(key, "+inf", wall() - window_sec, start=0, num=limit)
    return [h.decode() if isinstance(h, bytes) else str(h) for h in raw or ()]
