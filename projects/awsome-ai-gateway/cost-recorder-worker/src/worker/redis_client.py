# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import re

import structlog
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster

from worker.config import Settings

logger = structlog.get_logger(__name__)


def _redact_credentials(url: str) -> str:
    """`scheme://:pass@host/db` → `scheme://<redacted>@host/db`.

    ⚠️ REDIS_URL 은 런타임에 실제 ElastiCache auth token 을 **평문으로** 담고 있다.
       helm 이 `rediss://:$(REDIS_PASSWORD)@host:port/db` 로 렌더하고
       (charts/llm-gateway/templates/_helpers.tpl:149) K8s 가 컨테이너 시작 시
       Secret 값으로 $(REDIS_PASSWORD) 를 치환하기 때문이다. 예전엔 이 URL 을 그대로
       info 로그로 남겨, pod 이 뜰 때마다 Redis 비밀번호가 CloudWatch 로그에 적혔다
       (dev 에서 실제로 확인). 로그 접근 권한은 Redis 접근 권한보다 훨씬 넓다.

    host/port/db 는 진단에 필요하므로 남기고 userinfo 구간만 지운다. `[^/]*` 는
    authority 구간을 벗어나지 못하므로(path 는 `/` 로 시작) 탐욕적으로 잡아도 안전하고,
    비밀번호에 `@` 가 인코딩 없이 들어와도 마지막 `@` 까지 지운다. 자격증명이 없는
    URL 은 매치되지 않아 그대로 남는다. (db/env.py:49, gateway-proxy 와 같은 규칙.)
    """
    return re.sub(r"://[^/]*@", "://<redacted>@", url)

# XREADGROUP 전용 연결 + 보조 명령(XACK/XADD 재시도/PUBLISH 등)용 여유분
_REDIS_MAX_CONNECTIONS = 20


async def create_redis_client(settings: Settings) -> Redis | RedisCluster:
    """standalone/cluster 자동 감지 Redis 클라이언트."""
    kwargs: dict = {"max_connections": _REDIS_MAX_CONNECTIONS}
    if settings.redis_tls_enabled:
        kwargs["ssl"] = True

    probe = Redis.from_url(settings.redis_url)
    try:
        info = await probe.info("server")
        mode = info.get("redis_mode", "standalone")
    except Exception:
        mode = "standalone"
    finally:
        await probe.aclose()

    logger.info("redis_mode_detected", mode=mode, url=_redact_credentials(settings.redis_url))

    if mode == "cluster":
        return RedisCluster.from_url(settings.redis_url, **kwargs)
    return Redis.from_url(settings.redis_url, **kwargs)
