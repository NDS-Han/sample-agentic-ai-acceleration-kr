# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.models.notification import NotificationConfig

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

_POLL_INTERVAL = 300  # 5분 (PP-02)


class ConfigCache:
    """NotificationConfig 인메모리 캐시 (PP-02).

    이중 갱신 전략:
    1. Pub/Sub 즉시 갱신 (notifications:config_reload 수신 시 reload() 호출)
    2. 5분 주기 DB 폴링 (HealthCheckTask의 60초 루프에서 needs_poll() 확인)

    둘 다 실패 시 메모리의 기존 데이터로 계속 동작 (graceful degradation).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._configs: dict[str, NotificationConfig] = {}
        # ⚠️ "아직 로드 안 함" 을 0.0 으로 쓰면 안 된다. needs_poll() 이
        #    `time.monotonic() - self._last_loaded` 를 보는데, monotonic 의 기준점은
        #    임의(리눅스에서는 **부팅 시각**)다. 그래서 호스트 uptime 이 5분 미만이면
        #    `monotonic() - 0.0 < 300` 이 되어, **한 번도 로드하지 않은 캐시가
        #    "폴링 불필요" 라고 답한다.** 노드가 방금 뜬 직후 재시작한 파드에서는
        #    Pub/Sub 갱신이 오기 전까지 빈 설정으로 도는 창이 생긴다.
        #    (실측: 로컬 uptime 94,643s 라 통과 → CI 러너에서 실패. 부팅 후
        #     5분이라는 조건 때문에 개발 환경에서는 거의 재현되지 않는다.)
        #    None = 로드 이력 없음. 시계와 무관하게 폴링이 필요하다.
        self._last_loaded: float | None = None

    async def load(self) -> None:
        """DB에서 전체 NotificationConfig를 로드하여 캐시를 갱신한다."""
        async with self._session_factory() as session:
            result = await session.execute(select(NotificationConfig))
            configs = result.scalars().all()

        self._configs = {c.event_type: c for c in configs}
        self._last_loaded = time.monotonic()
        logger.info("config_cache_loaded", count=len(self._configs))

    async def reload(self) -> None:
        """Pub/Sub config_reload 이벤트 수신 시 호출된다."""
        await self.load()
        logger.info("config_cache_reloaded")

    def get(self, event_type: str) -> NotificationConfig | None:
        """이벤트 유형에 해당하는 NotificationConfig를 반환한다."""
        return self._configs.get(event_type)

    def needs_poll(self) -> bool:
        """마지막 로드로부터 POLL_INTERVAL(5분)이 경과했는지 확인한다.

        로드 이력이 없으면(``_last_loaded is None``) 무조건 True — 빈 캐시로 도는 창을
        만들지 않는다. 예전엔 sentinel 이 ``0.0`` 이라 호스트 uptime 이 5분 미만일 때
        False 를 돌려줬다(``__init__`` 주석 참조).
        """
        if self._last_loaded is None:
            return True
        return (time.monotonic() - self._last_loaded) > _POLL_INTERVAL


# Module singleton
_cache: ConfigCache | None = None


async def init_config_cache(session_factory: async_sessionmaker[AsyncSession]) -> None:
    global _cache
    _cache = ConfigCache(session_factory)
    await _cache.load()


def get_config_cache() -> ConfigCache:
    assert _cache is not None, "ConfigCache not initialised. Call init_config_cache() first."
    return _cache
