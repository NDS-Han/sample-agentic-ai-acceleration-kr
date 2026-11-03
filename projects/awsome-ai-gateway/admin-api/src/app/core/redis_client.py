# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster

from app.core.config import get_settings


async def create_redis_client() -> aioredis.Redis | RedisCluster:
    """Redis 클라이언트 생성.

    REDIS_CLUSTER_MODE 설정으로 모드 결정:
      - True: ElastiCache cluster mode (prod). RedisCluster 사용 — shard 간 MOVED redirect 자동 처리.
      - False: standalone (dev, 로컬).
      - None: 기동 시 INFO 명령으로 자동 감지.
    gateway-proxy 의 create_redis_client 와 동일 로직 (FR-4.1 rate limit Lua 경로에서
    이미 입증된 패턴).
    """
    settings = get_settings()
    kwargs: dict = {
        "decode_responses": True,
        "encoding": "utf-8",
        "max_connections": settings.REDIS_POOL_SIZE,
        # Redis 무응답 시 요청/잡이 무한 대기하지 않게 상한을 둔다 — repair 는
        # 행 FOR UPDATE 를 잡은 채 Redis I/O 를 하므로 무한 대기는 같은 키의
        # revoke 까지 멈추게 한다.
        "socket_timeout": 5.0,
        "socket_connect_timeout": 3.0,
    }

    if settings.REDIS_CLUSTER_MODE is True:
        return RedisCluster.from_url(settings.REDIS_URL, **kwargs)

    if settings.REDIS_CLUSTER_MODE is False:
        return aioredis.from_url(settings.REDIS_URL, **kwargs)

    # Auto-detect via startup INFO
    probe = aioredis.from_url(settings.REDIS_URL)
    try:
        info = await probe.info("server")
        mode = info.get("redis_mode", "standalone")
    except Exception:
        mode = "standalone"
    finally:
        await probe.aclose()

    if mode == "cluster":
        return RedisCluster.from_url(settings.REDIS_URL, **kwargs)
    return aioredis.from_url(settings.REDIS_URL, **kwargs)
