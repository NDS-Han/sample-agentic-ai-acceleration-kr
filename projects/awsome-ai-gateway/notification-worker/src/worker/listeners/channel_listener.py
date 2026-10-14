# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import time

import structlog
from sqlalchemy.exc import SQLAlchemyError

from worker.handlers.base import EventHandler, NotificationInfraError
from worker.observability.metrics import WorkerMetrics
from worker.redis_client import get_redis_client
from worker.schemas.events import parse_pubsub_message
from worker.services.notification_buffer import get_notification_buffer

logger = structlog.get_logger(__name__)

# get_message 가 이 시간동안 무수신이면 pubsub 연결에 ping 을 보낸다 —
# half-open TCP(페일오버 등으로 서버는 죽었는데 커널은 연결이 살아있는 상태)에서
# listen() 이 무한 블록돼 리스너가 "조용히 귀머거리"가 되는 것을 막는다.
_HEARTBEAT_SEC = 60.0
# shutdown 시 in-flight 발송이 결과 기록까지 마치도록 기다리는 상한.
_INFLIGHT_GRACE_SEC = 15.0


class ChannelListener:
    """Redis Pub/Sub 채널을 구독하고 수신된 이벤트를 EventHandler로 전달한다.

    TaskSupervisor가 ``run()``을 태스크로 실행한다. **메시지 단위** 예외는
    내부에서 삼킨다 — Pub/Sub 은 재생이 없어 재시작으로 실패 메시지를 복구할
    수 없고, 재구독 창의 다른 이벤트만 추가로 잃는다. 구독/연결 자체가 깨지는
    예외만 루프 밖으로 전파되어 Supervisor 재시작 정책(RP-01)이 동작한다.

    이벤트 처리는 bounded 태스크로 디스패치한다 — 핸들러를 루프 안에서 await
    하면 느린 발송(SES 타임아웃+재시도 ≈ 수신자당 ~130s)이 채널 전체 읽기를
    멈추고(head-of-line blocking), 그동안 Redis 측 pubsub 출력 버퍼가 차면
    구독자 연결이 끊겨 버퍼링된 이벤트가 조용히 유실된다.
    """

    def __init__(
        self,
        channel: str,
        handler: EventHandler,
        metrics: WorkerMetrics | None = None,
        max_inflight: int = 8,
    ) -> None:
        self.channel = channel
        self.handler = handler
        self.metrics = metrics
        self._sem = asyncio.Semaphore(max_inflight)
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """메인 구독 루프. 정상 종료 또는 예외 발생 시 finally에서 구독 해제."""
        redis = get_redis_client()
        pubsub = redis.pubsub()
        await pubsub.subscribe(self.channel)
        logger.info("channel_listener_subscribed", channel=self.channel)

        # half-open TCP(페일오버 등으로 서버는 죽었는데 커널은 연결이 살아있는
        # 상태)는 listen() 이 무한 블록돼 리스너가 "조용히 귀머거리"가 된다.
        # 마지막 수신(메시지/pong 불문) 시각을 추적해 무응답이 연속되면
        # 스스로 예외를 던져 Supervisor 재구독을 유도한다.
        last_activity = time.monotonic()
        try:
            while True:
                # async for pubsub.listen() 대신 get_message(timeout) —
                # 주기적으로 타임아웃이 나야 heartbeat ping 을 보낼 수 있다.
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_HEARTBEAT_SEC
                )
                if message is None:
                    # ping() 은 전송만 하고 응답을 기다리지 않는다 — 조용한 피어는
                    # 응답 없이 재전송만 반복하므로, pong 이 다음 get_message 로
                    # 돌아오는지를 last_activity 로 감시한다.
                    await pubsub.ping()
                    if time.monotonic() - last_activity > _HEARTBEAT_SEC * 3:
                        raise ConnectionError(
                            f"pubsub idle for {_HEARTBEAT_SEC * 3:.0f}s on {self.channel}"
                        )
                    continue

                last_activity = time.monotonic()
                # ping 응답(pong)이나 구독 확인 메시지는 이벤트가 아니다 —
                # 디스패치하면 파싱 실패 에러로 잡힌다.
                if message.get("type") != "message":
                    continue

                # 세마포어가 차면 읽기를 잠시 멈춘다 — 디스패치 병렬도 덕에
                # 지속 포화에서만 back-pressure 가 걸리고, 직렬 처리보다
                # 채널 드레인 속도가 max_inflight 배 빠르다.
                await self._sem.acquire()
                task = asyncio.create_task(
                    self._dispatch(message), name=f"dispatch:{self.channel}"
                )
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        finally:
            try:
                await pubsub.unsubscribe(self.channel)
                await pubsub.aclose()
            finally:
                # in-flight 발송이 잘려 'pending' dead-claim 으로 남지 않게
                # 짧게 기다린다 — 못 마친 건 sweeper 가 회수한다.
                if self._inflight:
                    await asyncio.wait(
                        self._inflight, timeout=_INFLIGHT_GRACE_SEC
                    )
            logger.info("channel_listener_unsubscribed", channel=self.channel)

    async def _dispatch(self, message: dict) -> None:
        """수신 메시지 1건을 파싱해 핸들러로 전달한다 (세마포어는 여기서 해제)."""
        try:
            try:
                event = parse_pubsub_message(message["data"])
            except Exception:
                logger.exception(
                    "channel_listener_parse_error", channel=self.channel
                )
                if self.metrics:
                    self.metrics.errors_total.add(1, {"error_type": "parse"})
                return

            if event is None:
                # parse 실패는 parse_pubsub_message가 이미 로깅함
                if self.metrics:
                    self.metrics.errors_total.add(1, {"error_type": "parse"})
                return

            if self.metrics:
                self.metrics.events_received_total.add(
                    1, {"channel": self.channel, "event_type": event.type.value}
                )

            try:
                await self.handler.handle(event)
            except (NotificationInfraError, SQLAlchemyError) as exc:
                # DB 장애 — Pub/Sub 재전송이 없으므로 메모리 버퍼에 넣고
                # HealthCheckTask 의 복구 drain 에 맡긴다. 버퍼도 차면
                # oldest-drop 정책으로 최근 이벤트를 우선 보존한다.
                logger.warning(
                    "event_buffered_infra_error",
                    channel=self.channel,
                    event_type=event.type.value,
                    error=str(exc),
                )
                try:
                    await get_notification_buffer().enqueue(event)
                except Exception:
                    logger.exception("event_buffer_enqueue_failed")
                if self.metrics:
                    self.metrics.errors_total.add(1, {"error_type": "buffered"})
            except Exception as exc:
                # 처리 로직 실패(렌더/발송 등)는 재생 불가 — 삼키고 다음
                # 이벤트로 간다. 재발송 가치가 있는 행은 sweeper 가 회수한다.
                logger.error(
                    "event_processing_failed",
                    channel=self.channel,
                    event_type=event.type.value,
                    error=str(exc),
                )
                if self.metrics:
                    self.metrics.errors_total.add(1, {"error_type": "processing"})
        finally:
            self._sem.release()
