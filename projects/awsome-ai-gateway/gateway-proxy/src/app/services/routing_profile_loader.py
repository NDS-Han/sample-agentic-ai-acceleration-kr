# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.routing import RoutingProfile
from app.schemas.routing import RoutingProfileSchema

logger = structlog.get_logger(__name__)

ROUTING_CACHE_TTL = 300  # 5 min, mirrors MODEL_CACHE_TTL
# 마지막으로 확인된 프로파일의 장기 백업 — DB 열화 + 5분 캐시 만료가 겹치면
# profile=None → cowork 가 Mantle 대신 in-account Bedrock 으로, cross-account 가
# in-account 로 조용히 바뀐다(계정·리전·모델·단가 전부). 그 조용한 전환이 장애보다
# 나쁘므로, DB 열화 시에만 이 키를 읽는다(E2E S3-M7). admin-api 무효화는 두 키를
# 함께 지운다.
STALE_CACHE_TTL = 86400  # 24h


class RoutingProfileLoader:
    """Loads model.routing_profiles rows by client, Redis-cached.

    Returns None when there is no enabled profile for the client -> callers
    fall back to the default (Claude Code / Bedrock InvokeModel) path.
    """

    async def load(
        self, redis, db: Optional[AsyncSession], client: Optional[str]
    ) -> Optional[RoutingProfileSchema]:
        if not client:
            return None

        cache_key = f"routing_profile:{client}"
        if redis is not None:
            try:
                cached = await redis.get(cache_key)
            except Exception:
                cached = None
            if cached:
                # 음성 캐시 — 프로파일 없는 client(other 등)는 매 요청 DB 를
                # 두드렸다. 짧은 TTL 로 부재를 캐시한다(R22 r1 N3).
                if cached == "__absent__" or cached == b"__absent__":
                    return None
                try:
                    schema = RoutingProfileSchema(**json.loads(cached))
                    return schema if schema.enabled else None
                except Exception:
                    logger.warning("routing_profile_cache_parse_failed", client=client)

        if db is None:
            # DB 열화 — 마지막으로 확인된 스냅샷을 서빙한다(24h TTL). 프로파일이 꼭
            # 필요한 client(cowork, cross-account)를 조용한 in-account 전환에서 지킨다.
            if redis is not None:
                try:
                    stale = await redis.get(f"{cache_key}:stale")
                except Exception:
                    stale = None
                if stale:
                    try:
                        schema = RoutingProfileSchema(**json.loads(stale))
                        if schema.enabled:
                            logger.warning(
                                "routing_profile_serving_stale_db_degraded", client=client
                            )
                            return schema
                    except Exception:
                        logger.warning(
                            "routing_profile_stale_parse_failed", client=client
                        )
            return None

        result = await db.execute(
            select(RoutingProfile).where(RoutingProfile.client == client)
        )
        row = result.scalar_one_or_none()
        if row is None:
            if redis is not None:
                try:
                    # 짧은 부재 캐시 — 신규 프로파일 등록이 최대 30s 늦게 반영된다.
                    await redis.setex(cache_key, 30, "__absent__")
                    # DB 정상 + 행 부재 = 프로파일이 삭제된 것 — 스테일 스냅샷을
                    # 같이 지워 이후 DB 열화 창에서 삭제된 프로파일이 부활하지
                    # 않게 한다(R22 r2 N3).
                    await redis.delete(f"{cache_key}:stale")
                except Exception:
                    pass
            return None

        schema = RoutingProfileSchema(
            client=row.client,
            backend=row.backend,
            account_role_arn=row.account_role_arn,
            region=row.region,
            default_model=row.default_model,
            external_id=row.external_id,
            enabled=row.enabled,
            web_search_enabled=row.web_search_enabled,
        )
        if redis is not None:
            try:
                _payload = json.dumps(schema.model_dump(mode="json"))
                await redis.setex(cache_key, ROUTING_CACHE_TTL, _payload)
                # 장기 백업 — 위 주석 참조 (DB 열화 시에만 읽힌다).
                await redis.setex(f"{cache_key}:stale", STALE_CACHE_TTL, _payload)
            except Exception:
                logger.warning("routing_profile_cache_write_failed", client=client)

        return schema if schema.enabled else None
