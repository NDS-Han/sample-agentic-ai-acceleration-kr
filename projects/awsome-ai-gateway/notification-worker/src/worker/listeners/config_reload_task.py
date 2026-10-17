# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.redis_client import get_redis_client
from worker.services.config_cache import get_config_cache

logger = structlog.get_logger(__name__)


class ConfigReloadTask:
    """notifications:config_reload Pub/Sub 이벤트 수신 시 ConfigCache를 즉시 갱신한다 (PP-02)."""

    CHANNEL = "notifications:config_reload"
    # ChannelListener 와 같은 half-open TCP 감지 — pubsub.listen() 은 무응답
    # 연결에서 무한 블록돼 페일오버 후 조용히 귀머거리가 된다(5분 폴링이
    # 피해를 완화하긴 하지만). get_message(timeout) + ping 으로 자가 감지.
    _HEARTBEAT_SEC = 60.0

    async def run(self) -> None:
        """Pub/Sub 채널을 구독하여 config reload 메시지를 수신하고 캐시를 갱신한다."""
        import time

        redis = get_redis_client()
        pubsub = redis.pubsub()
        await pubsub.subscribe(self.CHANNEL)
        logger.info("config_reload_task_subscribed", channel=self.CHANNEL)
        last_activity = time.monotonic()
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=self._HEARTBEAT_SEC
                )
                if message is None:
                    await pubsub.ping()
                    if time.monotonic() - last_activity > self._HEARTBEAT_SEC * 3:
                        raise ConnectionError(
                            f"pubsub idle for {self._HEARTBEAT_SEC * 3:.0f}s on {self.CHANNEL}"
                        )
                    continue
                last_activity = time.monotonic()
                if message["type"] != "message":
                    continue
                try:
                    cache = get_config_cache()
                    await cache.reload()
                    logger.info("config_reload_triggered")
                except Exception as exc:
                    logger.error("config_reload_failed", error=str(exc))
                    # 재시작 불필요 — 다음 메시지에서 재시도
        finally:
            await pubsub.unsubscribe(self.CHANNEL)
            await pubsub.aclose()
            logger.info("config_reload_task_unsubscribed")
