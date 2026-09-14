# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Redis Stream `cost:stream` XREADGROUP 소비자.

Flow:
  1. Startup: consumer group MKSTREAM 생성 (존재하면 skip)
  2. Loop:
     - Unacked backlog 먼저 소비 (id='0')
     - 완료되면 신규 메시지 (id='>') XREADGROUP BLOCK 5000ms COUNT batch_max_size
     - 배치 누적 → flush → XACK
  3. 타임아웃 시 현재 누적분만 flush (count < batch_max_size 이어도 OK).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog
from redis.exceptions import ResponseError

from worker.batch_flusher import BatchFlusher
from worker.config import Settings
from worker.schemas.cost_stream import CostStreamEntry

logger = structlog.get_logger(__name__)


def _parse_xautoclaim(res: Any) -> tuple[str, list]:
    """``XAUTOCLAIM`` 응답을 ``(next_cursor, messages)`` 로 정규화한다.

    ⚠️ redis-py 는 버전에 따라 2-tuple ``(cursor, msgs)`` 또는 3-tuple
       ``(cursor, msgs, deleted_ids)`` 를 돌려준다. 한쪽만 가정하면 다른 쪽에서
       ValueError 로 회수가 통째로 죽고, 그 실패는 "고아가 없다" 와 구별되지 않는다.
    """
    if not res:
        return "0-0", []
    if isinstance(res, (list, tuple)):
        cursor = res[0] if len(res) >= 1 else "0-0"
        msgs = res[1] if len(res) >= 2 else []
        if isinstance(cursor, bytes):
            cursor = cursor.decode()
        return str(cursor), list(msgs or [])
    return "0-0", []


class StreamConsumer:
    """cost:stream → BatchFlusher pipeline."""

    def __init__(
        self,
        redis: Any,
        flusher: BatchFlusher,
        settings: Settings,
        metrics: Any = None,
    ) -> None:
        self._redis = redis
        self._flusher = flusher
        self._stream = settings.cost_stream_key
        self._group = settings.cost_stream_group
        self._consumer = settings.cost_stream_consumer
        self._batch_max = settings.batch_max_size
        self._batch_interval = settings.batch_max_interval_sec
        self._block_ms = settings.xread_block_ms
        self._claim_interval = settings.xautoclaim_interval_sec
        self._claim_min_idle_ms = settings.xautoclaim_min_idle_ms
        self._claim_task: asyncio.Task | None = None
        self._metrics = metrics

    async def ensure_group(self) -> None:
        """Consumer group MKSTREAM create. BUSYGROUP은 정상 상황."""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info(
                "consumer_group_created", stream=self._stream, group=self._group
            )
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.info(
                    "consumer_group_exists", stream=self._stream, group=self._group
                )
            else:
                raise

    async def run(self) -> None:
        """메인 소비 루프. TaskSupervisor가 예외 시 재시작."""
        await self.ensure_group()
        logger.info(
            "stream_consumer_started",
            stream=self._stream,
            group=self._group,
            consumer=self._consumer,
        )

        # 기동 시 unacked backlog 먼저 처리 (**이 consumer 이름의** 크래시 복구)
        await self._drain_backlog()

        # ⚠️ 죽은 **다른** consumer 이름의 PEL 은 위 backlog 로 절대 보이지 않는다
        #    (XREADGROUP id='0' 은 자기 PEL 만 돌려준다). 파드 이름이 consumer 이름이고
        #    롤아웃마다 바뀌므로, 회수하지 않으면 그 메시지들은 영구히 유실된다.
        #    기동 시 한 번 + 주기적으로 돈다.
        await self._reclaim_orphans()
        self._claim_task = asyncio.create_task(self._reclaim_loop())

        try:
            # 정상 소비 루프
            await self._consume_live()
        finally:
            if self._claim_task is not None:
                self._claim_task.cancel()

    async def _drain_backlog(self) -> None:
        """이 consumer 이름으로 남아있는 unacked 메시지 재처리 (at-least-once)."""
        while True:
            msgs = await self._redis.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: "0"},
                count=self._batch_max,
                block=0,  # no block for backlog
            )
            if not msgs:
                break

            entries, ids = self._decode(msgs)
            if not entries:
                # 파싱 실패 메시지들도 ACK하여 무한 루프 방지.
                if ids:
                    await self._redis.xack(self._stream, self._group, *ids)
                break

            await self._flusher.flush(entries)
            await self._redis.xack(self._stream, self._group, *ids)
            logger.info("backlog_batch_processed", count=len(ids))

    async def _reclaim_loop(self) -> None:
        """주기적으로 고아 PEL 을 회수한다. 예외로 소비 루프를 죽이지 않는다."""
        while True:
            try:
                await asyncio.sleep(self._claim_interval)
                await self._reclaim_orphans()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("xautoclaim_loop_failed")

    async def _reclaim_orphans(self) -> None:
        """``XAUTOCLAIM`` 으로 다른 consumer 이름의 idle 메시지를 이 이름으로 가져와 처리.

        ⚠️ 이것이 없으면 무슨 일이 나나. 워커는 RollingUpdate Deployment 라 파드 이름에
           랜덤 접미사가 붙고, 그 이름이 consumer 이름이다. 배치를 읽은 뒤 flush 가
           실패해(DB 페일오버 등) XACK 을 못 한 상태에서 파드가 롤되면, 새 파드는 **다른**
           이름이라 그 PEL 을 볼 수 없다. 그 배치는 영구히 unacked 로 남고, cost:stream 은
           MAXLEN~100_000 으로 트림되므로 결국 원본 entry 까지 사라진다 — 그 요청들의
           usage_logs 행, budget_usages 차감, 일별 카운터가 모두 없다(즉 **과소청구**이며
           복구 불가).

        ⚠️ 살아 있는 형제 replica 의 진행 중 배치를 빼앗을 수 있다. 그래서 min_idle 을
           크게 둔다(기본 5분). 그럼에도 겹치면, 배치 flusher 의 재처리 필터가 이중청구를
           막는다 — 두 방어가 짝이다.
        """
        cursor = "0-0"
        reclaimed = 0
        while True:
            try:
                res = await self._redis.xautoclaim(
                    name=self._stream,
                    groupname=self._group,
                    consumername=self._consumer,
                    min_idle_time=self._claim_min_idle_ms,
                    start_id=cursor,
                    count=self._batch_max,
                )
            except ResponseError as e:
                # XAUTOCLAIM 은 Redis/Valkey 6.2+ 다. 구버전이면 조용히 포기한다 —
                # 여기서 죽으면 소비 자체가 멈춘다.
                logger.warning("xautoclaim_unsupported", error=str(e)[:160])
                return

            cursor, msgs = _parse_xautoclaim(res)
            if not msgs:
                break

            entries, ids = self._decode([(self._stream, msgs)])
            if entries:
                await self._flusher.flush(entries)
            if ids:
                await self._redis.xack(self._stream, self._group, *ids)
                reclaimed += len(ids)
            if cursor in ("0-0", "0", None):
                break

        if reclaimed:
            logger.warning(
                "orphan_pel_reclaimed",
                count=reclaimed,
                consumer=self._consumer,
                min_idle_ms=self._claim_min_idle_ms,
            )

    async def _consume_live(self) -> None:
        """신규 메시지 XREADGROUP(>) + batch 누적 + time/count 기준 flush."""
        while True:
            batch_entries: list[CostStreamEntry] = []
            batch_ids: list[str] = []
            deadline = time.monotonic() + self._batch_interval

            while len(batch_entries) < self._batch_max:
                remaining_ms = max(
                    100, int((deadline - time.monotonic()) * 1000)
                )
                if remaining_ms <= 0:
                    break

                msgs = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={self._stream: ">"},
                    count=self._batch_max - len(batch_entries),
                    block=min(remaining_ms, self._block_ms),
                )
                if not msgs:
                    # timeout → 현재 배치 flush 이후 새 배치로.
                    break

                entries, ids = self._decode(msgs)
                batch_entries.extend(entries)
                batch_ids.extend(ids)

            if not batch_entries:
                # yield back to event loop — xreadgroup 호출이 이미 blocking이지만
                # 예외 없이 빈 결과 온 경우에도 tight loop 방지.
                await asyncio.sleep(0)
                continue

            await self._flusher.flush(batch_entries)
            await self._redis.xack(self._stream, self._group, *batch_ids)

    def _decode(
        self, raw_msgs: list[Any]
    ) -> tuple[list[CostStreamEntry], list[str]]:
        """XREADGROUP 응답 → (entries, ids). 파싱 실패 메시지는 ID만 반환하여 ACK."""
        entries: list[CostStreamEntry] = []
        ids: list[str] = []
        for _stream_name, records in raw_msgs:
            for msg_id, fields in records:
                raw_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                ids.append(raw_id)
                payload = fields.get(b"payload") or fields.get("payload")
                if payload is None:
                    logger.warning("cost_stream_entry_missing_payload", msg_id=raw_id)
                    continue
                try:
                    raw = (
                        payload.decode("utf-8") if isinstance(payload, bytes) else payload
                    )
                    data = json.loads(raw)
                    entries.append(CostStreamEntry(**data))
                except Exception as exc:
                    logger.warning(
                        "cost_stream_entry_parse_failed",
                        msg_id=raw_id,
                        error=str(exc),
                    )
        return entries, ids
