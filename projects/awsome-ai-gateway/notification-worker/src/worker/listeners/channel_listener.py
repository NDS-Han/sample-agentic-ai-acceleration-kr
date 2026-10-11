# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.handlers.base import EventHandler
from worker.observability.metrics import WorkerMetrics
from worker.redis_client import get_redis_client
from worker.schemas.events import parse_pubsub_message

logger = structlog.get_logger(__name__)


class ChannelListener:
    """Redis Pub/Sub 채널을 구독하고 수신된 이벤트를 EventHandler로 전달한다.

    TaskSupervisor가 ``run()``을 태스크로 실행한다. **메시지 단위** 예외는
    내부에서 삼킨다 — Pub/Sub 은 재생이 없어 재시작으로 실패 메시지를 복구할
    수 없고, 재구독 창의 다른 이벤트만 추가로 잃는다. 구독/연결 자체가 깨지는
    예외만 ``async for`` 밖으로 전파되어 Supervisor 재시작 정책(RP-01)이 동작한다.
    """

    def __init__(
        self,
        channel: str,
        handler: EventHandler,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self.channel = channel
        self.handler = handler
        self.metrics = metrics

    async def run(self) -> None:
        """메인 구독 루프. 정상 종료 또는 예외 발생 시 finally에서 구독 해제."""
        redis = get_redis_client()
        pubsub = redis.pubsub()
        await pubsub.subscribe(self.channel)
        logger.info("channel_listener_subscribed", channel=self.channel)

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                try:
                    # 1. 메시지 파싱
                    event = parse_pubsub_message(message["data"])
                    if event is None:
                        # parse 실패는 parse_pubsub_message가 이미 로깅함
                        if self.metrics:
                            self.metrics.errors_total.add(1, {"error_type": "parse"})
                        continue

                    # 2. 메트릭: 수신 카운트
                    if self.metrics:
                        self.metrics.events_received_total.add(
                            1, {"channel": self.channel, "event_type": event.type.value}
                        )

                    # 3. 핸들러 호출 — 메시지 단위 실패는 여기서 삼킨다.
                    # Pub/Sub 은 ACK/재생이 없어 이미 소비된 메시지는 재시작으로도
                    # 복구되지 않는다. re-raise → Supervisor 재시작 경로는
                    # 재구독(backoff) 창 동안 **다른** 이벤트까지 유실시키므로,
                    # parse 는 됐지만 처리에 반복 실패하는 poison 이벤트가 채널
                    # 전체를 마비시킬 수 있다. 구독 자체가 깨지는 오류는 아래
                    # async for 가 raise 하여 Supervisor 재시작(RP-01)으로 간다.
                    try:
                        await self.handler.handle(event)
                    except Exception as exc:
                        logger.error(
                            "event_processing_failed",
                            channel=self.channel,
                            event_type=event.type.value,
                            error=str(exc),
                        )
                        if self.metrics:
                            self.metrics.errors_total.add(1, {"error_type": "processing"})
                        continue

                except Exception:
                    # 파싱/메트릭 등 메시지 범위 내 예상 밖 예외도 동일 — 재시작으로는
                    # 이 메시지를 복구할 수 없고 다른 이벤트만 잃는다. 구독/연결
                    # 오류는 async for 가 raise 하여 Supervisor 재시작으로 간다.
                    logger.exception("channel_listener_loop_error", channel=self.channel)
                    if self.metrics:
                        self.metrics.errors_total.add(1, {"error_type": "unexpected"})
                    continue

        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()
            logger.info("channel_listener_unsubscribed", channel=self.channel)
