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

# 큐 아이템: (request_id, partition_key, 직렬화 bytes). 레코드 객체를 큐에
# 들고 있으면 파싱된 request/response dict 가 살아있어 실제 메모리가 직렬화
# 바이트의 ~3배가 된다 — 상한 회계가 실메모리를 반영하려면 bytes 만 보관한다.
_QueueItem = tuple[str, str, bytes]


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
        max_queue_bytes: int = 134_217_728,
    ) -> None:
        self._firehose = firehose_client
        self._s3 = s3_client
        self._stream_name = stream_name
        self._bucket = bucket
        self._enabled = enabled
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_record_bytes = max_record_bytes
        self._max_queue_bytes = max_queue_bytes
        # 레코드 수가 아니라 **바이트**로도 상한을 둔다 — 과거엔 개수(10k)만
        # 제한이라 대형 요청(Claude Code 프롬프트 수백 KB)이 쌓이면 flusher
        # 처리량(~20 rec/s)을 추월해 2GiB 파드가 OOM 으로 죽을 수 있었다(R16).
        # enqueue 시점에 한 번만 직렬화해 bytes 만 저장한다 — 크기 회계가
        # 실메모리와 거의 일치하고 flush 시 재직렬화 비용도 사라진다.
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=max_queue)
        self._queued_bytes = 0
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

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    async def enqueue(self, record: BodyLogRecord) -> bool:
        if not self.enabled_effective:
            return True  # no-op: silently skip when Firehose unconfigured
        return await self._enqueue_data(
            record.request_id, record.partition_key(), record.to_firehose_bytes()
        )

    async def _enqueue_data(
        self, request_id: str, partition_key: str, data: bytes
    ) -> bool:
        """직렬화 바이트를 직접 넣는 재큐 경로 — flush 실패 재큐가 재직렬화
        없이 큐를 거치게 하고, 큐에는 바이트만 보관해 상한 회계를 정확하게 한다."""
        size = len(data)
        if size > self._max_queue_bytes:
            self._dropped += 1
            logger.warning(
                "body_log_record_too_large_dropped",
                request_id=request_id,
                size=size,
            )
            return False
        # 바이트 상한을 위해 가장 오래된 레코드부터 버린다 — 최신 감사 기록이
        # 오래된 것보다 가치 있다는 기존 drop-oldest 정책의 연장.
        while self._queued_bytes + size > self._max_queue_bytes:
            try:
                _, _, evicted = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_bytes -= len(evicted)
            self._dropped += 1
            logger.warning(
                "body_log_queue_evicted",
                reason="byte_cap",
                dropped=self._dropped,
                queued_bytes=self._queued_bytes,
            )
        try:
            self._queue.put_nowait((request_id, partition_key, data))
        except asyncio.QueueFull:
            try:
                _, _, evicted = self._queue.get_nowait()
                self._queued_bytes -= len(evicted)
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait((request_id, partition_key, data))
            self._dropped += 1
            self._queued_bytes += size
            logger.warning("body_log_record_dropped", queue_size=self._queue.qsize())
            return False
        self._queued_bytes += size
        return True

    async def start(self) -> None:
        if not self.enabled_effective:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                # 틱당 1배치(100건)는 20 req/s 지속 부하에서 큐가 불어나 drop-
                # oldest 로 감사 레코드를 잃는다 — 비울 때까지 반복하되 틱을
                # 독점하지 않게 배치 수 상한을 둔다(R22 r1 S3).
                for _ in range(10):
                    if await self._flush_once() == 0:
                        break
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

    def _drain_batch(self) -> list[_QueueItem]:
        batch: list[_QueueItem] = []
        for _ in range(self._batch_size):
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_bytes -= len(item[2])
            batch.append(item)
        return batch

    async def _flush_once(self) -> int:
        batch = self._drain_batch()
        if not batch:
            return 0

        # 처리가 끝난(전송 확정·재인큐·폐기 결정) 레코드를 추적한다 — 취소 시
        # 아직 결론이 안 난 항목만 큐로 되돌려, 이미 배송된 레코드의 중복 재전송을
        # 최소화한다(R16 V3-S4). 전송 중 취소된 청크는 부분 배송 여부를 알 수
        # 없어 되돌린다 — Firehose 자체가 at-least-once 라 request_id 기준의
        # 희소한 중복 감사행은 수용한다.
        done: set[int] = set()
        try:
            return await self._flush_batch(batch, done)
        except asyncio.CancelledError:
            for i, item in enumerate(batch):
                if i in done:
                    continue
                # 개수·바이트 상한을 어기고 밀어넣으면 회계가 깨진다 — 넘으면
                # drop 카운터를 올려 가시화한다(N3).
                if self._queue.full() or (
                    self._queued_bytes + len(item[2]) > self._max_queue_bytes
                ):
                    self._dropped += 1
                    logger.warning(
                        "body_log_requeue_full", request_id=item[0]
                    )
                    continue
                self._queue.put_nowait(item)
                self._queued_bytes += len(item[2])
            raise

    async def _flush_batch(
        self, batch: list[_QueueItem], done: set[int]
    ) -> int:
        # enqueue 시점에 직렬화된 바이트를 재사용한다 — oversized 레코드는 S3 로.
        # ``done`` 에는 "취소돼도 재인큐하지 않아도 되는" batch 인덱스를 기록한다:
        # 전송 확정, _send_firehose_chunk 내부 재인큐, oversized 폐기/실패 전부
        # 해당 — 이미 결론이 난 항목을 되돌리면 중복 감사행이 된다.
        eligible: list[tuple[int, _QueueItem]] = []
        oversized: list[tuple[int, _QueueItem]] = []
        for idx, item in enumerate(batch):
            if len(item[2]) <= self._max_record_bytes:
                eligible.append((idx, item))
            else:
                oversized.append((idx, item))

        flushed = 0

        # Sub-chunk eligible records by BOTH the 4 MB byte limit and the 500-record limit.
        chunk: list[_QueueItem] = []
        chunk_idx: list[int] = []
        chunk_bytes = 0
        for idx, item in eligible:
            if chunk and (
                chunk_bytes + len(item[2]) > _MAX_FIREHOSE_BATCH_BYTES
                or len(chunk) >= _MAX_FIREHOSE_BATCH_RECORDS
            ):
                flushed += await self._send_firehose_chunk(chunk)
                done.update(chunk_idx)
                chunk = []
                chunk_idx = []
                chunk_bytes = 0
            chunk.append(item)
            chunk_idx.append(idx)
            chunk_bytes += len(item[2])
        if chunk:
            flushed += await self._send_firehose_chunk(chunk)
            done.update(chunk_idx)

        for idx, (request_id, partition_key, data) in oversized:
            if self._s3 is None or not self._bucket:
                logger.warning("body_log_oversized_no_s3", request_id=request_id)
                self._dropped += 1
                done.add(idx)
                continue
            try:
                await asyncio.to_thread(
                    self._s3.put_object,
                    Bucket=self._bucket,
                    Key=f"{partition_key}/{request_id}.json",
                    Body=data,
                )
                flushed += 1
            except Exception:
                logger.exception("body_log_s3_put_failed", request_id=request_id)
                self._dropped += 1
            done.add(idx)

        return flushed

    async def _send_firehose_chunk(self, chunk: list[_QueueItem]) -> int:
        """Send one sub-chunk to Firehose. Re-enqueue records that fail (call-level
        exception OR per-record ErrorCode). Return the count confirmed delivered."""
        records = [{"Data": data} for _, _, data in chunk]
        try:
            resp = await asyncio.to_thread(
                self._firehose.put_record_batch,
                DeliveryStreamName=self._stream_name,
                Records=records,
            )
        except Exception:
            logger.exception("body_log_firehose_put_failed", count=len(records))
            for request_id, partition_key, data in chunk:
                await self._enqueue_data(request_id, partition_key, data)
            return 0

        failed = resp.get("FailedPutCount", 0) if isinstance(resp, dict) else 0
        if not failed:
            return len(chunk)

        responses = resp.get("RequestResponses", []) if isinstance(resp, dict) else []
        delivered = 0
        for i, (request_id, partition_key, data) in enumerate(chunk):
            res = responses[i] if i < len(responses) else {}
            if isinstance(res, dict) and res.get("ErrorCode"):
                await self._enqueue_data(request_id, partition_key, data)
            else:
                delivered += 1
        logger.warning(
            "body_log_firehose_partial_failure",
            failed=failed,
            requeued=len(chunk) - delivered,
        )
        return delivered
