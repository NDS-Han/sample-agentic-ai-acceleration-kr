# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Dead-letter spool for cost:stream XADD failures (P0-②).

The ONLY durable path that carries a finalized request's usage to the DB is the
``cost:stream`` Redis Stream. If Redis is down at finalize time, the XADD fails
and — previously — the record was lost forever (the failure was swallowed with a
warning). This spool catches those failed payloads in a bounded in-memory buffer
and re-publishes them when Redis recovers (health checker) or on shutdown.

Scope / limits (honest):
  - In-process memory. Since wave 3b a background writer (``run_store_writer``) moves spooled
    payloads into ``usage.cost_pending_events`` within milliseconds (``CostEventStore``); the
    worker drains that table. Only what is still in memory when the pod dies is lost (and the
    request's admission 'start' event lets the worker sweeper write an estimated row for it).
  - Bounded (drop-oldest) to avoid unbounded growth when Redis AND the DB are down; drops
    are counted + logged so the loss is visible rather than silent.
  - Re-publish is plain re-XADD; the cost-recorder-worker's usage_logs INSERT is
    idempotent (request_id UNIQUE) and budget_usages is now replay-safe (P0-①),
    so a re-published entry cannot double-count even if it raced a partial XADD.

This is distinct from ``UsageBufferQueue`` (which is DB/ORM-shaped, for a separate
DB-outage concern); this spool re-publishes to the SAME stream the worker reads.
"""
from __future__ import annotations

import asyncio
from collections import deque

import structlog

logger = structlog.get_logger(__name__)


class CostStreamSpool:
    """Bounded in-memory buffer of cost:stream XADD payloads pending re-publish."""

    def __init__(self, stream_key: str, maxlen: int = 10_000,
                 maxlen_field: int | None = None) -> None:
        self._stream_key = stream_key
        self._maxlen = maxlen
        # Re-publish XADD MAXLEN — None(기본) = 트림 없음(wave 3 B4). MAXLEN 은 미소비 entry 도
        # 잘라 청구 기록을 잃는다. 크기 제어는 워커의 XTRIM MINID(소비된 것만)가 한다.
        self._maxlen_field = maxlen_field
        # PLAIN deque (NOT deque(maxlen=...)). The bound is enforced explicitly in
        # enqueue() so that overflow eviction is a deliberate, counted action — and
        # crucially so that a concurrent append can NEVER auto-evict the in-flight
        # head that drain() has popped into a local variable (see drain()).
        self._buf: deque[str] = deque()
        self._lock = asyncio.Lock()
        self._dropped = 0
        self._drop_counter = None  # GatewayMetrics.usage_records_dropped_total
        #: run_store_writer 가 돌 때만 생긴다 — enqueue 가 깨운다.
        self._wake: asyncio.Event | None = None

    def set_metrics(self, dropped_counter) -> None:
        self._drop_counter = dropped_counter

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def dropped(self) -> int:
        return self._dropped

    def enqueue(self, payload_json: str) -> None:
        """Buffer a serialized stream payload (the value passed to XADD).

        Synchronous single-coroutine-step body (no await) — request handlers never
        block on a slow drain. asyncio is single-threaded, so this body runs to
        completion without interleaving with drain()'s synchronous sections.
        When at capacity we evict the OLDEST entry and COUNT it (visible loss, not
        silent). A plain deque is used (no maxlen) so eviction only ever happens
        here, deliberately — never as an automatic side effect of append() racing
        a drain that is holding the in-flight item in a local variable.
        """
        while len(self._buf) >= self._maxlen and self._buf:
            self._buf.popleft()
            self._dropped += 1
            if self._drop_counter is not None:
                self._drop_counter.add(1)
            logger.warning("cost_stream_spool_dropped_oldest", spool_size=len(self._buf))
        self._buf.append(payload_json)
        if self._wake is not None:
            # DB 폴백 writer 를 깨운다(run_store_writer) — 다음 틱을 기다리지 않는다.
            self._wake.set()

    async def flush_to_store(self, store, max_items: int = 500) -> int:
        """버퍼 앞쪽 최대 ``max_items`` 개를 DB 폴백 표(``CostEventStore``)에 쓴다(wave 3b, B3).

        drain() 과 같은 pop-and-hold 규칙: 꺼낸 묶음은 로컬에만 있고, 쓰기가 성공하지 않으면
        (예외·취소 모두) 원래 순서대로 앞에 되돌린다. 같은 lock 을 잡으므로 drain() 과 동시에
        같은 항목을 두 번 내보내지 않는다. 반환 = 쓴 개수.
        """
        if store is None or not self._buf:
            return 0
        async with self._lock:
            batch: list[str] = []
            while self._buf and len(batch) < max_items:
                batch.append(self._buf.popleft())
            if not batch:
                return 0
            written = False
            try:
                await store.write(batch)
                written = True
            except Exception:
                logger.warning("cost_stream_spool_db_write_failed",
                               batch=len(batch), remaining=len(self._buf) + len(batch))
            finally:
                if not written:
                    self._buf.extendleft(reversed(batch))
            return len(batch) if written else 0

    async def run_store_writer(self, store, interval: float = 1.0, max_items: int = 500) -> None:
        """spool → DB 폴백 표를 계속 비우는 백그라운드 루프(lifespan 이 띄우고 cancel 한다).

        enqueue 가 깨우므로 XADD 실패 후 보통 수 ms 안에 DB 에 들어간다 — 파드가 SIGKILL 되면
        잃는 것은 그 사이의 항목뿐이다(예전에는 Redis 복구 전까지 전부 메모리에만 있었다).
        DB 도 실패하면 항목은 버퍼에 남고(헬스체커가 Redis 복구 시 재발행) ``interval`` 뒤 다시
        시도한다.
        """
        self._wake = asyncio.Event()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._wake.clear()
            while self._buf:
                n = await self.flush_to_store(store, max_items=max_items)
                if n == 0:
                    break  # DB 실패 — 다음 틱에 다시

    async def drain(self, redis) -> int:
        """Re-publish buffered payloads to cost:stream. Returns count drained.

        Pop-and-hold: popleft() the head into a LOCAL variable, then await XADD.
        Because the item now lives only in the local (not in the deque), a
        concurrent enqueue() cannot evict it. If publish does not succeed for ANY
        reason the payload is appendleft()'d back; since the deque is unbounded-by-
        construction (bound enforced only in enqueue), that always succeeds.
        The re-buffer is in a `finally` so it ALSO covers asyncio.CancelledError
        (a BaseException — NOT caught by `except Exception`), e.g. the health-check
        task being cancelled on shutdown mid-await: the in-flight payload is put
        back rather than stranded. FIFO preserved; no double-publish (popped
        before XADD, only re-added when not published).
        """
        if redis is None or not self._buf:
            return 0
        drained = 0
        async with self._lock:
            while self._buf:
                payload = self._buf.popleft()  # remove into local; safe from eviction
                published = False
                try:
                    trim = ({"maxlen": self._maxlen_field, "approximate": True}
                            if self._maxlen_field else {})
                    await redis.xadd(self._stream_key, {"payload": payload}, **trim)
                    published = True
                except Exception:
                    # Redis still unhealthy — finally re-buffers, then we stop.
                    logger.warning(
                        "cost_stream_spool_drain_interrupted",
                        drained=drained,
                        remaining=len(self._buf) + 1,
                    )
                finally:
                    if not published:
                        # Normal failure OR cancellation (CancelledError propagates
                        # after this): never strand the in-flight payload.
                        self._buf.appendleft(payload)
                if not published:
                    break  # normal Exception path; CancelledError already propagated
                drained += 1
        if drained:
            logger.info("cost_stream_spool_drained", count=drained, remaining=len(self._buf))
        return drained
