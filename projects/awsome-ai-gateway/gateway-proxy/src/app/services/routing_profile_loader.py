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


def _row_to_schema(row: RoutingProfile) -> RoutingProfileSchema:
    return RoutingProfileSchema(
        client=row.client,
        backend=row.backend,
        account_role_arn=row.account_role_arn,
        region=row.region,
        default_model=row.default_model,
        external_id=row.external_id,
        enabled=row.enabled,
        web_search_enabled=row.web_search_enabled,
    )


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

        from app.services import model_cache as _mc

        if _mc.MODEL_CACHE is not None:
            return await self._load_cached(_mc.MODEL_CACHE, redis, db, client)

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

        schema = _row_to_schema(row)
        if redis is not None:
            try:
                _payload = json.dumps(schema.model_dump(mode="json"))
                await redis.setex(cache_key, ROUTING_CACHE_TTL, _payload)
                # 장기 백업 — 위 주석 참조 (DB 열화 시에만 읽힌다).
                await redis.setex(f"{cache_key}:stale", STALE_CACHE_TTL, _payload)
            except Exception:
                logger.warning("routing_profile_cache_write_failed", client=client)

        return schema if schema.enabled else None

    async def _load_cached(self, cache, redis, db, client: str) -> RoutingProfileSchema | None:
        """wave 1d — 프로세스 로컬 캐시 경로(services/model_cache.py). 키·TTL·의미는 위와 같다.

        차이: 로컬 히트면 Redis/DB 를 보지 않는다. Redis/DB 읽기에 데드라인이 있다. DB 가 답하지
        못하면(열화·타임아웃) 마지막으로 알던 프로파일 → Redis ``:stale`` 백업 순으로 쓴다.
        """
        from app.services.model_cache import NX, OK, Entry

        cache_key = f"routing_profile:{client}"

        def _entry(schema: RoutingProfileSchema | None) -> Entry:
            if schema is None:
                return Entry(NX, None, client=client)
            return Entry(OK, schema, client=client)

        async def _redis_load() -> Entry | None:
            cached = await redis.get(cache_key)
            if not cached:
                return None
            if cached == "__absent__" or cached == b"__absent__":
                return _entry(None)
            try:
                return _entry(RoutingProfileSchema(**json.loads(cached)))
            except Exception:
                logger.warning("routing_profile_cache_parse_failed", client=client)
                return None

        async def _db_load(s):
            result = await s.execute(select(RoutingProfile).where(RoutingProfile.client == client))
            row = result.scalar_one_or_none()
            if row is None:
                # 부재 캐시 30 s + 스테일 백업 삭제(삭제된 프로파일 부활 방지, R22 r2 N3).
                return _entry(None), [
                    ("setex", cache_key, 30, "__absent__"),
                    ("delete", f"{cache_key}:stale", 0, None),
                ]
            schema = _row_to_schema(row)
            payload = json.dumps(schema.model_dump(mode="json"))
            return _entry(schema), [
                ("setex", cache_key, ROUTING_CACHE_TTL, payload),
                ("setex", f"{cache_key}:stale", STALE_CACHE_TTL, payload),
            ]

        async def _fallback(redis_failed: bool, db_failed: bool) -> Entry:
            # DB 열화/실패 — Redis 의 24h 스냅샷(E2E S3-M7). 프로파일이 꼭 필요한 client 를
            # 조용한 in-account 전환에서 지킨다. Redis 도 못 읽으면 None(레거시와 같다).
            if redis is None:
                return _entry(None)
            stale, _failed = await cache.redis_call(
                lambda: redis.get(f"{cache_key}:stale"), what="routing_profile.stale"
            )
            if stale:
                try:
                    schema = RoutingProfileSchema(**json.loads(stale))
                    logger.warning("routing_profile_serving_stale_db_degraded", client=client)
                    return _entry(schema)
                except Exception:
                    logger.warning("routing_profile_stale_parse_failed", client=client)
            return _entry(None)

        entry = await cache.get(
            ("profile", client),
            db=db,
            db_load=_db_load,
            redis=redis,
            redis_load=_redis_load if redis is not None else None,
            fallback=_fallback,
        )
        schema = entry.value if entry.kind == OK else None
        return schema if schema is not None and schema.enabled else None
