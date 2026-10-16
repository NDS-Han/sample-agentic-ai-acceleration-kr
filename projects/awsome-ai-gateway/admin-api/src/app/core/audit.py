# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog

logger = structlog.get_logger()


class AuditLogger:
    """Audit logger with in-process queue + long-lived batch consumer.

    Behavior:
      - log(session, ...): session 이 주어지면 ``session.add`` 로 비즈니스
        트랜잭션에 바인딩한다(R21 S5) — 커밋과 동거하므로 롤백된 변경에
        SUCCESS 행이 남지 않는다. 실패를 기록하는 경로(4xx 응답과 함께 세션이
        롤백되는 경우)는 ``queue_only=True`` 로 큐를 쓴다.
      - session=None 또는 queue_only → non-blocking put_nowait into
        asyncio.Queue. If queue is full, drop entry and emit a warning
        (응답 지연보다 우선).
      - Background consumer drains the queue every `flush_interval` seconds
        or when `batch_size` items accumulate, then bulk-INSERTs.
      - shutdown() sets stop_event and waits for the consumer to drain remaining
        items (graceful). Pod kill -9 may lose in-flight queue items.

    NFR-3.8 implication: audit log is now eventually-consistent with < 500ms
    typical lag. See spec.
    """

    def __init__(
        self,
        *,
        batch_size: int = 100,
        flush_interval: float = 0.5,
        max_queue_size: int = 10_000,
    ) -> None:
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._consumer_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._dropped = 0

    # audit.audit_logs VARCHAR 컬럼 폭 — 클라이언트 조작 값(x-request-id,
    # path 파라미터 기반 resource_id)이 넘치면 배치 INSERT 전체가 실패해
    # 무관한 행까지 유실된다. enqueue 시점에 컬럼 폭으로 클립한다.
    _FIELD_LIMITS = {
        "actor_role": 32,
        "action": 128,
        "resource_type": 64,
        "resource_id": 256,
        "result": 32,
        "ip_address": 45,
        "request_id": 128,
    }

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[:limit]

    async def log(
        self,
        session: AsyncSession | None,  # 있으면 행을 이 트랜잭션에 바인딩(R21 S5)
        *,
        actor_user_id: uuid.UUID,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: str,
        changes: dict | None = None,
        result: str = "SUCCESS",
        ip_address: str = "0.0.0.0",
        request_id: str = "",
        service_token_id: uuid.UUID | None = None,
        queue_only: bool = False,
    ) -> None:
        changes = dict(changes) if changes else {}
        # changes 는 JSONB 로 들어간다 — Decimal/datetime/UUID 같은 비직렬화
        # 값이 있으면 커밋 시점에 터지고(S5 이후 비즈니스 트랜잭션과 동거라
        # 감사 직렬화 실패가 500 을 만든다 — R21 M-B), default=str 로 정규화한다.
        # allow_nan=False — 기본 json.dumps 는 NaN/Infinity 를 bare token 으로
        # 출력하는데 JSONB 는 그것을 거부한다. 세션-바운드 감사(S5)는 비즈니스
        # 트랜잭션과 동거하므로 커밋 시점 거부가 곧 500 이다 — ValueError 를
        # 유도해 아래 폴백으로 보낸다(R21 r3 N1).
        try:
            changes = json.loads(json.dumps(changes, default=str, allow_nan=False))
        except (TypeError, ValueError):
            changes = {"_unserializable": True}
        if service_token_id is not None:
            # svc- 토큰 호출자는 모두 같은 합성 user_id(...0011)를 공유한다 —
            # 어떤 토큰이 행동했는지 기록하지 않으면 revoke 후에도 행위자를
            # 특정할 수 없다.
            changes["_service_token_id"] = str(service_token_id)
        entry = {
            "id": uuid.uuid4(),
            "timestamp": datetime.now(timezone.utc),
            "actor_user_id": actor_user_id,
            "actor_role": self._clip(actor_role, self._FIELD_LIMITS["actor_role"]),
            "action": self._clip(action, self._FIELD_LIMITS["action"]),
            "resource_type": self._clip(resource_type, self._FIELD_LIMITS["resource_type"]),
            "resource_id": self._clip(resource_id, self._FIELD_LIMITS["resource_id"]),
            "changes": changes,
            "result": self._clip(result, self._FIELD_LIMITS["result"]),
            "ip_address": self._clip(ip_address, self._FIELD_LIMITS["ip_address"]),
            "request_id": self._clip(request_id, self._FIELD_LIMITS["request_id"]),
        }
        # 요청 세션이 있으면 비즈니스 트랜잭션에 묶는다 — CommittingRoute 의
        # commit 에 같이 들어가므로 롤백된 변경에 SUCCESS 감사 행이 남거나,
        # 큐 유실/배치 실패로 커밋된 변경의 감사가 사라지는 일이 없다.
        # ``queue_only=True`` 는 예외 응답 이후에도 남겨야 하는 행(열람 시도
        # 감사처럼 실패 자체를 기록하는 경로)용 — 세션에 묶으면 404/4xx 롤백에
        # 같이 버려진다.
        if session is not None and not queue_only:
            session.add(AuditLog(**entry))
            return
        # NOTE: log() does not check stop_event. If called during/after shutdown,
        # the entry can be enqueued but never drained (consumer already exited).
        # Trade-off accepted: we don't want callers to block or fail at request time.
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "audit.queue_full_drop",
                action=action,
                resource_type=resource_type,
                dropped_total=self._dropped,
            )

    async def start(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        if self._consumer_task is not None:
            return
        self._session_factory = session_factory
        self._stop_event.clear()
        self._consumer_task = asyncio.create_task(self._consume(), name="audit_consumer")

    async def shutdown(self, timeout: float = 10.0) -> None:
        if self._consumer_task is None:
            return
        # NOTE: consumer wakes up at most after `flush_interval` seconds (default 0.5s)
        # because _drain_batch awaits queue.get() with that timeout. Acceptable at
        # current defaults; raise concern if flush_interval is ever raised significantly.
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._consumer_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("audit.shutdown_timeout", remaining=self._queue.qsize())
            self._consumer_task.cancel()
        self._consumer_task = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        return self._dropped

    async def _consume(self) -> None:
        assert self._session_factory is not None
        while True:
            batch = await self._drain_batch()
            if batch:
                await self._flush_batch(batch)
            if self._stop_event.is_set() and self._queue.empty():
                return

    async def _drain_batch(self) -> list[dict[str, Any]]:
        """Wait for first item (or flush_interval timeout), then drain up to batch_size."""
        batch: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
            batch.append(first)
        except asyncio.TimeoutError:
            return batch
        # 큐에 더 있으면 batch_size 까지 즉시 drain (block 안 함)
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        assert self._session_factory is not None
        try:
            async with self._session_factory() as session:
                session.add_all([AuditLog(**entry) for entry in batch])
                await session.commit()
            return
        except Exception as exc:
            logger.error("audit.flush_failed", count=len(batch), error=str(exc), exc_info=True)
            # 행 단위 오류(DataError/IntegrityError)만 단건 폴백으로 진입한다 —
            # 연결 오류/풀 타임아웃은 배치 전체가 같은 이유로 실패할 테고, 그때
            # 100회 재시도는 각 호출이 pool_timeout 을 기다려 컨슈머를 수십 분
            # 멈추게 한다(R21 r2 S-1).
            from sqlalchemy.exc import DataError, IntegrityError

            if not isinstance(exc, (DataError, IntegrityError)):
                return
        if len(batch) <= 1:
            return
        # 한 독 행이 배치 전체를 쓰러뜨리지 않게 단건 폴백 — 실패한 행만 로그.
        for entry in batch:
            try:
                async with self._session_factory() as session:
                    session.add(AuditLog(**entry))
                    await session.commit()
            except Exception as exc:
                logger.error(
                    "audit.row_flush_failed",
                    action=entry.get("action"),
                    resource_type=entry.get("resource_type"),
                    error=str(exc),
                )


# Module-level singleton (호환). main.py lifespan 에서 start()/shutdown() 호출.
audit_logger = AuditLogger()
