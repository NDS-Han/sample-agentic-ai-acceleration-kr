# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Suspect budget counters — `:lost` sentinel re-delivery queue.

``budget_deduct``/``budget_settle`` failing at finalize leaves the Redis usage
counter thinner than ``budget.budget_usages`` (the enforcement-source ledger).
The counter still exists and is positive, so the admission-time rebuild probe
(``check_budget``) never fires — the under-count persists for the whole period.

The recovery bridge is the same ``{usage_key}:lost`` sentinel the Lua scripts
set on detected loss: the next admission's MGET probe sees it and triggers a
floor-rebuild from the ledger (``budget_rebuild.lua`` — never lowers, also
fixes ``resvsum`` both directions). Enforcement is only wrong until the next
request for that scope — which is exactly when it matters.

Two-tier delivery in ``cost_recorder``:
  1. inline ``SET {key}:lost`` — a single cheap op that survives the pool
     exhaustion bursts that kill the heavier Lua evals;
  2. if even that fails (real Redis outage), the key lands here and each
     healthy health-checker tick retries the sentinel write.

Scope / limits (same honesty contract as CostStreamSpool): in-process memory,
bounded drop-oldest. A pod crash loses queued suspects — this covers
seconds-to-minutes blips, not a restart guarantee. Long outages that flip the
degradation detector already get the full ``budget_reconcile`` window.
"""
from __future__ import annotations

import asyncio
from collections import deque

import structlog

logger = structlog.get_logger(__name__)

# Lua 측 센티넬과 같은 수명 (budget_deduct.lua / budget_settle.lua 의 45일).
SENTINEL_TTL_SECONDS = 3_888_000


class BudgetSuspectSet:
    """Bounded FIFO of usage keys whose `:lost` sentinel write is pending."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._buf: deque[str] = deque()
        # drain 중 팝된 키는 잠시 _membership 밖이라, 그 사이의 재실패 enqueue 는
        # 중복이 생길 수 있다 — 센티넬 SET 은 멱등이라 최악은 중복 SET 한 번.
        self._membership: set[str] = set()
        self._maxlen = maxlen
        self._lock = asyncio.Lock()
        self._dropped = 0
        self._drop_counter = None

    def set_metrics(self, dropped_counter) -> None:
        self._drop_counter = dropped_counter

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def dropped(self) -> int:
        return self._dropped

    def enqueue(self, usage_key: str) -> None:
        """Queue a usage key for sentinel delivery. Sync, never blocks a request.

        Plain deque + explicit bound (same reason as CostStreamSpool): eviction
        is deliberate and counted, and can never strand a drain-held head.
        Already-queued keys are skipped — the sentinel SET is idempotent, a
        second failure adds nothing.
        """
        if usage_key in self._membership:
            return
        while len(self._buf) >= self._maxlen and self._buf:
            evicted = self._buf.popleft()
            self._membership.discard(evicted)
            self._dropped += 1
            if self._drop_counter is not None:
                self._drop_counter.add(1)
            logger.warning("budget_suspect_dropped_oldest", suspect_size=len(self._buf))
        self._buf.append(usage_key)
        self._membership.add(usage_key)

    async def drain(self, redis) -> int:
        """Write `{key}:lost` for queued keys. Returns sentinels delivered.

        Pop-and-hold like CostStreamSpool.drain: a failed/cancelled SET puts the
        key back (``finally`` covers CancelledError too) — never strands it.
        Stops at first failure: Redis is still bad, the rest wait for next tick.
        """
        if redis is None or not self._buf:
            return 0
        delivered = 0
        async with self._lock:
            while self._buf:
                key = self._buf.popleft()
                self._membership.discard(key)
                delivered_ok = False
                try:
                    await redis.set(
                        f"{key}:lost", "1", ex=SENTINEL_TTL_SECONDS
                    )
                    delivered_ok = True
                except Exception:
                    logger.warning(
                        "budget_suspect_drain_interrupted",
                        delivered=delivered,
                        remaining=len(self._buf) + 1,
                    )
                finally:
                    if not delivered_ok:
                        self._buf.appendleft(key)
                        self._membership.add(key)
                if not delivered_ok:
                    break
                delivered += 1
        if delivered:
            logger.info(
                "budget_suspects_marked_lost", count=delivered, remaining=len(self._buf)
            )
        return delivered
