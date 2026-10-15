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


#: 이 워커가 이해하는 cost:stream 스키마의 최신 버전. 게이트웨이는 3.
#: additive(필드 추가) 업그레이드는 Pydantic extra-ignore 로 안전하게 넘어가고,
#: 이를 넘는 entry 는 경고+메트릭으로 배포 검증에 신호를 남긴다. 의미 변경이
#: 필요한 업그레이드는 워커 선배포가 전제다 — ARCHITECTURE.md 의 스트림 계약 참고.
_SUPPORTED_SCHEMA_VERSION = 3


def _parse_xautoclaim(res: Any) -> tuple[str, list, list]:
    """``XAUTOCLAIM`` 응답을 ``(next_cursor, messages, deleted_ids)`` 로 정규화한다.

    ⚠️ redis-py 는 버전에 따라 2-tuple ``(cursor, msgs)`` 또는 3-tuple
       ``(cursor, msgs, deleted_ids)`` 를 돌려준다. 한쪽만 가정하면 다른 쪽에서
       ValueError 로 회수가 통째로 죽고, 그 실패는 "고아가 없다" 와 구별되지 않는다.

    Redis 7+ 의 3번째 원소는 **본문이 이미 MAXLEN 트림된** PEL 항목 id 들 —
    이 항목들은 PEL 에서도 제거되며 되돌릴 수 없다. 호출자가 반드시
    계측해야 한다: 유실이 아예 로그 없이 일어나는 경로다.
    """
    if not res:
        return "0-0", [], []
    if isinstance(res, (list, tuple)):
        cursor = res[0] if len(res) >= 1 else "0-0"
        msgs = res[1] if len(res) >= 2 else []
        deleted = res[2] if len(res) >= 3 else []
        if isinstance(cursor, bytes):
            cursor = cursor.decode()
        return str(cursor), list(msgs or []), list(deleted or [])
    return "0-0", [], []


def _stream_id_key(sid: str) -> tuple[int, int]:
    """``"<ms>-<seq>"`` 스트림 id 를 비교 가능한 튜플로. ``"0"`` 같은 단축형도 받는다."""
    ms, _, seq = sid.partition("-")
    return int(ms or 0), int(seq or 0)


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
        self.last_lag = 0
        # dead-letter 추적 — 같은 배치 머리(첫 entry id)가 grace 기간을 넘어
        # 계속 flush 실패하면 결정적 오류로 보고 행 단위 소화로 넘긴다.
        # 그냥 재시도하면 스트림 전체 봉쇄 → MAXLEN 트림 전량 유실로 번진다.
        # ⚠️ 소비 경로별로 분리 — reclaim(백그라운드 태스크)과 live/drain 이
        #    같은 streak 를 공유하면, live 배치 성공이 reclaim 실패 추적을
        #    지워 회수 대상 poison 배치가 grace 에 영원히 도달하지 못한다.
        self._fail_head: str | None = None
        self._first_fail_at: float | None = None
        self._reclaim_fail_head: str | None = None
        self._reclaim_first_fail_at: float | None = None
        self._dead_letter_grace = settings.stream_dead_letter_grace_sec

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
            # ⚠️ crash-loop(DB 장애 등)에서 _claim_task 는 다시 만들어지기 전에
            #    여기서 죽으므로 lag 샘플링이 얼어붙는다 — 소비가 멈춘 바로 그
            #    상황에서 알람이 사라진다. drain 루프가 재시작마다 도는 곳이니
            #    여기서도 샘플링한다.
            await self._sample_lag()
            msgs = await self._redis.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: "0"},
                count=self._batch_max,
                block=None,  # PEL 읽기는 어차피 비블로킹 — BLOCK 을 생략해 의도를 명시
            )
            if not msgs:
                break

            entries, ids = self._decode(msgs)
            if not entries:
                # 파싱 실패 메시지들도 ACK하여 무한 루프 방지.
                if ids:
                    await self._redis.xack(self._stream, self._group, *ids)
                break

            await self._flush_or_dead_letter(entries, ids)
            await self._redis.xack(self._stream, self._group, *ids)
            await self._trim_consumed()
            logger.info("backlog_batch_processed", count=len(ids))

    async def _trim_consumed(self) -> None:
        """커밋된 entry 본문을 정리한다 — 스트림이 무한히 자라는 것을 막는다.

        ⚠️ 게이트웨이 쪽의 ``XADD MAXLEN ~`` 근사 트림은 **미소비 entry 도**
           잘라낸다 — 워커가 몇 분만 멈춰도 청구 기록이 영구 유실된다. 대신
           여기서 커밋 경계를 경계로 둔다. 레이스 방지를 위해 읽는 순서가
           중요하다: ``last-delivered-id`` 를 **먼저** 읽고 그 뒤에 pending
           최솟값을 읽는다. pending 이 비어 보이는 사이 다른 소비자가 읽은
           진행 중 entry 를 last-delivered-id 로 지우면 그 배치의 재처리가
           ``(id, None)`` 으로 영구 유실되므로, 경계는 두 값의 **작은 쪽**
           이다 — L 읽기 이후 배달된 것만 커질 수 있어 안전하다.
           실패해도 소비에 영향 없다(다음 배치가 다시 시도).
        """
        try:
            last_delivered: Any = None
            for g in await self._redis.xinfo_groups(self._stream) or []:
                name = g.get("name")
                if isinstance(name, bytes):
                    name = name.decode()
                if name == self._group:
                    last_delivered = g.get("last-delivered-id")
                    break
            pend = await self._redis.xpending_range(
                self._stream, self._group, "-", "+", count=1
            )
            floor: Any = pend[0].get("message_id") if pend else last_delivered
            # redis-py 는 decode_responses=False 로 bytes 를 돌려준다 — 비교와
            # XTRIM 전에 반드시 디코드한다(str(b"1-0") → "b'1-0'" 으로 오염).
            if isinstance(floor, bytes):
                floor = floor.decode()
            if isinstance(last_delivered, bytes):
                last_delivered = last_delivered.decode()
            if floor and last_delivered:
                # pending 최솟값과 last-delivered 중 작은 쪽 — 둘 다 읽은 시점
                # 이전의 커밋 경계를 넘지 않도록 보수적으로 잡는다.
                if _stream_id_key(str(floor)) > _stream_id_key(str(last_delivered)):
                    floor = last_delivered
            if not floor:
                return
            await self._redis.xtrim(self._stream, minid=floor)
        except Exception as e:
            # 트림이 조용히 멈추면 스트림이 MAXLEN 백스톱(500K, 수백 MB)까지
            # 자란다 — 소비 장애와 별개로 알 수 있어야 한다.
            logger.warning("cost_stream_trim_failed", error=str(e)[:160])

    async def _sample_lag(self) -> None:
        """consumer group 의 미처리 entry 수를 `last_lag` 에 반영한다 (메트릭용).

        XINFO GROUPS 의 `lag`(Redis 7+)는 미배달+pending 합산이라 실제 backlog 에
        가장 가깝다. 구버전 Valkey/Redis 에 lag 이 없으면 `pending`(PEL 크기)으로
        폴백한다. 실패해도 소비 루프에 영향을 주지 않는다.
        """
        try:
            groups = await self._redis.xinfo_groups(self._stream)
            for g in groups or []:
                name = g.get("name")
                if isinstance(name, bytes):
                    name = name.decode()
                if name == self._group:
                    lag = g.get("lag")
                    if lag is None:
                        lag = g.get("pending", 0)
                    self.last_lag = int(lag)
                    return
            # 그룹 행이 없다 = 아직 메시지를 읽은 적 없음 → backlog 0
            self.last_lag = 0
        except Exception as e:
            logger.debug("stream_lag_sample_failed", error=str(e)[:160])

    async def _reclaim_loop(self) -> None:
        """주기적으로 고아 PEL 을 회수한다. 예외로 소비 루프를 죽이지 않는다."""
        while True:
            try:
                await asyncio.sleep(self._claim_interval)
                await self._sample_lag()
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
           MAXLEN~500_000 으로 트림되므로 결국 원본 entry 까지 사라진다 — 그 요청들의
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

            cursor, msgs, deleted_ids = _parse_xautoclaim(res)
            if deleted_ids:
                # Redis 7+: 본문이 MAXLEN 으로 잘려 PEL 에서도 제거된 항목들.
                # (id, None) 경로와 달리 본문이 없어 기록 자체가 불가하다 —
                # 유실된 청구 행 수를 반드시 계측한다.
                logger.error(
                    "cost_stream_pel_trimmed",
                    count=len(deleted_ids),
                    consumer=self._consumer,
                )
            if not msgs:
                # cursor 가 0-0 이 아니면 스캔 페이지에 idle 미달만 있던 것 —
                # 뒤쪽에 고아가 남았을 수 있으니 cursor 까지 간다.
                if cursor in ("0-0", "0", None):
                    break
                continue

            entries, ids = self._decode([(self._stream, msgs)])
            if entries:
                await self._flush_or_dead_letter(entries, ids, reclaim=True)
            if ids:
                await self._redis.xack(self._stream, self._group, *ids)
                await self._trim_consumed()
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
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break  # 배치 간격 상한 — 지연보다 flush 를 우선.

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
                # R3-11: poison-only 배치 — 디코딩 불가 메시지도 읽은 ID 는 ACK 해
                # PEL 에 남기지 않는다. 안 하면 XAUTOCLAIM 이 계속 회수해 무한
                # 재실패 루프가 된다. 디코딩 불가 메시지는 재처리해도 영원히
                # 실패하므로 drop 이 맞다.
                if batch_ids:
                    await self._redis.xack(self._stream, self._group, *batch_ids)
                # yield back to event loop — xreadgroup 호출이 이미 blocking이지만
                # 예외 없이 빈 결과 온 경우에도 tight loop 방지.
                await asyncio.sleep(0)
                continue

            await self._flush_or_dead_letter(batch_entries, batch_ids)
            await self._redis.xack(self._stream, self._group, *batch_ids)
            await self._trim_consumed()

    async def _flush_or_dead_letter(
        self, entries: list[CostStreamEntry], ids: list[str], *, reclaim: bool = False
    ) -> None:
        """배치 flush + 결정적 실패 dead-letter.

        같은 배치 머리(첫 entry id)가 ``stream_dead_letter_grace_sec`` 를 넘어
        계속 실패하면 그 오류는 재시도로 낫지 않는 결정적 오류다(예: 워커가
        마이그레이션보다 먼저 배포된 UndefinedColumn). 그냥 재전파하면 스트림
        전체가 멈추고 MAXLEN 트림으로 배치뿐 아니라 **뒤의 전부**가 유실된다.
        grace 를 넘기면 행 단위 소화(flush_dead_letter)로 전환해 살리는 행만
        기록하고 스트림을 진행시킨다. 단 일시 계열 오류(연결·타임아웃)는
        dead-letter 경로에서도 재전파된다 — grace 는 유실이 아니라 진행
        선택지만 준다.

        배치 머리 기준으로 추적한다 — 같은 배치가 재전달되면 머리 id 가 같고,
        배치가 바뀌면 카운터가 리셋된다(supervisor 재시작 시 head 부터 다시 읽음).
        ``reclaim=True`` 는 XAUTOCLAIM 회수 경로 — 별도 streak 를 써서 live
        소비의 성공이 회수 대상 poison 의 추적을 지우지 않게 한다.
        """
        if reclaim:
            fail_head_attr = "_reclaim_fail_head"
            fail_at_attr = "_reclaim_first_fail_at"
        else:
            fail_head_attr = "_fail_head"
            fail_at_attr = "_first_fail_at"
        try:
            await self._flusher.flush(entries)
            setattr(self, fail_head_attr, None)
            setattr(self, fail_at_attr, None)
            return
        except Exception:
            head = ids[0] if ids else None
            now = time.monotonic()
            if head != getattr(self, fail_head_attr) or getattr(self, fail_at_attr) is None:
                setattr(self, fail_head_attr, head)
                setattr(self, fail_at_attr, now)
                raise
            if now - getattr(self, fail_at_attr) < self._dead_letter_grace:
                raise
            logger.error(
                "batch_dead_letter",
                head=head,
                batch_size=len(entries),
                reclaim=reclaim,
                grace_sec=round(now - getattr(self, fail_at_attr), 1),
            )
            written, dropped = await self._flusher.flush_dead_letter(entries)
            # replay 로 걸러진 행(다른 소비자가 이미 커밋)은 dropped 가 아니다 —
            # len(entries)-written 으로 세면 경합 스킵이 유실로 오집계된다.
            logger.warning(
                "batch_dead_letter_done",
                written=written,
                dropped=dropped,
                replay_skipped=len(entries) - written - dropped,
            )
            if dropped and self._metrics:
                self._metrics.dead_letter_dropped.add(dropped)
            setattr(self, fail_head_attr, None)
            setattr(self, fail_at_attr, None)

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
                # XAUTOCLAIM 은 PEL 만 남고 본문이 MAXLEN 으로 잘린 entry 를
                # (id, None) 으로 돌려준다 — fields.get 이 AttributeError 로
                # 소비 루프를 죽이기 전에 여기서 ACK 대상으로 분류한다.
                if fields is None:
                    logger.warning("cost_stream_entry_trimmed", msg_id=raw_id)
                    continue
                payload = fields.get(b"payload") or fields.get("payload")
                if payload is None:
                    logger.warning("cost_stream_entry_missing_payload", msg_id=raw_id)
                    continue
                try:
                    raw = (
                        payload.decode("utf-8") if isinstance(payload, bytes) else payload
                    )
                    data = json.loads(raw)
                    entry = CostStreamEntry(**data)
                    entries.append(entry)
                except Exception as exc:
                    logger.warning(
                        "cost_stream_entry_parse_failed",
                        msg_id=raw_id,
                        error=str(exc),
                    )
                else:
                    if entry.schema_version > _SUPPORTED_SCHEMA_VERSION:
                        # 게이트웨이가 워커보다 새 스키마 — additive(필드 추가)면
                        # 파싱은 안전하지만, 의미 변경이면 조용히 오염된다.
                        # loud signal 을 남겨 배포 검증이 잡을 수 있게 한다.
                        logger.warning(
                            "cost_stream_future_schema",
                            msg_id=raw_id,
                            schema_version=entry.schema_version,
                            supported=_SUPPORTED_SCHEMA_VERSION,
                        )
                        if self._metrics:
                            self._metrics.future_schema_entries.add(1)
        return entries, ids
