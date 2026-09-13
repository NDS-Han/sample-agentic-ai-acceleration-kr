# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""전역 런타임 설정 읽기/쓰기.

DB(``public.system_settings``)가 진실의 원천이고 Redis 는 그 앞의 읽기 캐시다.
행이 없으면 "미설정" 이고, 소비자가 자기 기본값을 적용한다(body logging: OFF).
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_invalidation import CacheInvalidationManager
from app.repositories.system_settings_repository import SystemSettingsRepository

logger = structlog.get_logger()

#: DB 키(``public.system_settings.key``).
BODY_LOGGING_KEY = "body_logging_enabled"
#: gateway-proxy 가 읽는 Redis 키. 그쪽 ``services/body_log_flag.py`` 와 같아야 한다.
BODY_LOG_REDIS_KEY = "bodylog:enabled"


class SystemSettingsService:
    """DB 가 진실의 원천, Redis 는 읽기 캐시(write-through)."""

    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def get_body_logging_enabled(self, session: AsyncSession) -> bool:
        repo = SystemSettingsRepository(session)
        row = await repo.get(BODY_LOGGING_KEY)
        if row is None:
            # 미설정 = OFF. 본문 수집 기능이 "설정을 못 읽었다" 는 이유로 켜지면 안 된다.
            return False
        return bool(row.value)

    async def set_body_logging_enabled(
        self,
        session: AsyncSession,
        *,
        enabled: bool,
        actor_id: uuid.UUID | None,
    ) -> bool:
        """DB 에 저장한다. **Redis 미러링은 하지 않는다** — 아래 이유.

        ⚠️ 여기서 Redis 에 쓰면 안 된다. 이 메서드는 라우터의 ``session.commit()``
           **전에** 끝난다. 그 창에 gateway-proxy 가 플래그를 읽으면 아직 커밋되지 않은
           값이 Redis 에만 있는 상태가 되고, 만약 그 트랜잭션이 롤백되면
           **DB 는 OFF 인데 Redis 는 ON** 으로 갈린다 — 즉 아무도 켜지 않은 본문 수집이
           돌아간다. 프라이버시에 영향을 주는 스위치에서 그 방향의 불일치는 특히 나쁘다.

           같은 부류의 결함을 이 레포에서 두 번 고쳤다(per-user allowed-clients,
           per-app web-search 토글). 미러링은 라우터가 커밋 뒤에
           :meth:`mirror_body_logging_to_cache` 로 한다.
        """
        repo = SystemSettingsRepository(session)
        await repo.upsert(BODY_LOGGING_KEY, enabled, updated_by=actor_id)
        logger.info(
            "system_settings.body_logging_set",
            enabled=enabled,
            actor_id=str(actor_id) if actor_id else None,
        )
        return enabled

    async def mirror_body_logging_to_cache(self, enabled: bool) -> None:
        """커밋된 값을 Redis 로 미러링한다. **``session.commit()`` 뒤에** 호출한다.

        실패해도 예외를 올리지 않는다. gateway-proxy 는 Redis 미스 시 DB 에서
        재수화하므로(``body_log_flag``), 이 쓰기는 전파를 빠르게 하는 최적화이지
        정확성의 근거가 아니다. 캐시 쓰기 실패로 관리자의 토글을 500 으로 만들면
        "DB 는 바뀌었는데 API 는 실패했다" 는 더 나쁜 상태가 된다.
        """
        redis = getattr(self._cache_mgr, "_redis", None)
        if redis is None:
            return
        try:
            await redis.set(BODY_LOG_REDIS_KEY, "1" if enabled else "0")
        except Exception:
            # ⚠️ 조용히 넘기지 않고 남긴다 — 전파가 늦어지는 창(gateway 의 in-process
            #    TTL + DB 재수화)이 생기므로 운영자가 알아야 한다.
            logger.warning("system_settings.body_logging_cache_mirror_failed", enabled=enabled)
