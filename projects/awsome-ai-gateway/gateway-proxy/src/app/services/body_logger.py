# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import asyncio

import structlog

from app.schemas.body_log import BodyLogRecord

logger = structlog.get_logger(__name__)

# Firehose PutRecordBatch hard limits: 500 records AND 4 MB per call. Use 4_000_000
# (not 4_194_304) to leave headroom for request envelope overhead.
_MAX_FIREHOSE_BATCH_BYTES = 4_000_000
_MAX_FIREHOSE_BATCH_RECORDS = 500


class BodyLogger:
    """In-process raw-body logger: bounded queue -> background batch flush to
    Firehose (S3 fallback for >1MB records). No-op when Firehose is unconfigured.

    Hot path only calls ``enqueue`` (microseconds, no network I/O). The flusher
    task (start/stop in Task 3) ships batches. Bodies never touch Redis.
    """

    def __init__(
        self,
        *,
        firehose_client=None,
        s3_client=None,
        stream_name: str | None = None,
        bucket: str | None = None,
        enabled: bool = True,
        max_queue: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        max_record_bytes: int = 900_000,
    ) -> None:
        self._firehose = firehose_client
        self._s3 = s3_client
        self._stream_name = stream_name
        self._bucket = bucket
        self._enabled = enabled
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_record_bytes = max_record_bytes
        self._queue: asyncio.Queue[BodyLogRecord] = asyncio.Queue(maxsize=max_queue)
        self._dropped = 0
        self._task: asyncio.Task | None = None

    @property
    def enabled_effective(self) -> bool:
        return bool(self._enabled and self._stream_name and self._firehose is not None)

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, record: BodyLogRecord) -> bool:
        if not self.enabled_effective:
            return True  # no-op: silently skip when Firehose unconfigured
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(record)
            self._dropped += 1
            logger.warning("body_log_record_dropped", queue_size=self._queue.qsize())
            return False

    async def start(self) -> None:
        if not self.enabled_effective:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush_once()
            except Exception:
                logger.exception("body_log_flush_loop_error")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # graceful drain: flush everything still queued
        while self._queue.qsize() > 0:
            flushed = await self._flush_once()
            if flushed == 0:
                break

    def _drain_batch(self) -> list[BodyLogRecord]:
        batch: list[BodyLogRecord] = []
        for _ in range(self._batch_size):
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _flush_once(self) -> int:
        batch = self._drain_batch()
        if not batch:
            return 0

        # Pair each eligible record with its serialized bytes so failed records can be
        # re-enqueued; oversized records go to S3 individually.
        eligible: list[tuple[BodyLogRecord, bytes]] = []
        oversized: list[tuple[BodyLogRecord, bytes]] = []
        for rec in batch:
            data = rec.to_firehose_bytes()
            if len(data) <= self._max_record_bytes:
                eligible.append((rec, data))
            else:
                oversized.append((rec, data))

        flushed = 0

        # Sub-chunk eligible records by BOTH the 4 MB byte limit and the 500-record limit.
        chunk: list[tuple[BodyLogRecord, bytes]] = []
        chunk_bytes = 0
        for rec, data in eligible:
            if chunk and (
                chunk_bytes + len(data) > _MAX_FIREHOSE_BATCH_BYTES
                or len(chunk) >= _MAX_FIREHOSE_BATCH_RECORDS
            ):
                flushed += await self._send_firehose_chunk(chunk)
                chunk = []
                chunk_bytes = 0
            chunk.append((rec, data))
            chunk_bytes += len(data)
        if chunk:
            flushed += await self._send_firehose_chunk(chunk)

        for rec, data in oversized:
            if self._s3 is None or not self._bucket:
                logger.warning("body_log_oversized_no_s3", request_id=rec.request_id)
                continue
            try:
                await asyncio.to_thread(
                    self._s3.put_object,
                    Bucket=self._bucket,
                    Key=f"{rec.partition_key()}/{rec.request_id}.json",
                    Body=data,
                )
                flushed += 1
            except Exception:
                logger.exception("body_log_s3_put_failed", request_id=rec.request_id)

        return flushed

    async def _send_firehose_chunk(
        self, chunk: list[tuple[BodyLogRecord, bytes]]
    ) -> int:
        """Send one sub-chunk to Firehose. Re-enqueue records that fail (call-level
        exception OR per-record ErrorCode). Return the count confirmed delivered."""
        records = [{"Data": data} for _, data in chunk]
        try:
            resp = await asyncio.to_thread(
                self._firehose.put_record_batch,
                DeliveryStreamName=self._stream_name,
                Records=records,
            )
        except Exception:
            logger.exception("body_log_firehose_put_failed", count=len(records))
            for rec, _ in chunk:
                await self.enqueue(rec)
            return 0

        failed = resp.get("FailedPutCount", 0) if isinstance(resp, dict) else 0
        if not failed:
            return len(chunk)

        responses = resp.get("RequestResponses", []) if isinstance(resp, dict) else []
        delivered = 0
        for i, (rec, _) in enumerate(chunk):
            res = responses[i] if i < len(responses) else {}
            if isinstance(res, dict) and res.get("ErrorCode"):
                await self.enqueue(rec)
            else:
                delivered += 1
        logger.warning(
            "body_log_firehose_partial_failure",
            failed=failed,
            requeued=len(chunk) - delivered,
        )
        return delivered
