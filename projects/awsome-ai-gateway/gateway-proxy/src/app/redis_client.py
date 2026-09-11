# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import re

import structlog
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import Settings

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
    URL 은 매치되지 않아 그대로 남는다. (db/env.py:49 와 같은 규칙.)
    """
    return re.sub(r"://[^/]*@", "://<redacted>@", url)


def _resilience_kwargs(settings: Settings) -> dict:
    """연결 복원력 kwargs (deepdive Q50 Phase 2).

    socket_timeout 이 없으면(과거) 느린 노드가 awaited 호출을 무한 블로킹해 풀을
    전 pod 에서 고갈시킨다. 명령/연결 타임아웃 + blip 재시도 + idle health-check 로
    "느린 Redis → 빠른 실패 → 상위 fallback" 으로 바꾼다. 값 0/음수면 해당 항목 생략
    (과거 동작 복귀 — 안전밸브). hot-path 라 값 조정 시 load A/B 권장.
    """
    kwargs: dict = {"max_connections": settings.redis_pool_size}

    if settings.redis_socket_timeout and settings.redis_socket_timeout > 0:
        kwargs["socket_timeout"] = settings.redis_socket_timeout
    if settings.redis_connect_timeout and settings.redis_connect_timeout > 0:
        kwargs["socket_connect_timeout"] = settings.redis_connect_timeout
    if settings.redis_health_check_interval and settings.redis_health_check_interval > 0:
        kwargs["health_check_interval"] = settings.redis_health_check_interval

    if settings.redis_retries and settings.redis_retries > 0:
        # 단발 timeout/connection blip 만 백오프 재시도(50ms→cap 500ms). 느린 DB
        # 강등 전 흡수. 다른 예외(논리 오류 등)는 재시도 안 함.
        kwargs["retry"] = Retry(ExponentialBackoff(cap=0.5, base=0.05), settings.redis_retries)
        kwargs["retry_on_error"] = [RedisTimeoutError, RedisConnectionError]

    return kwargs


def _cluster_kwargs(settings: Settings) -> dict:
    """cluster 전용 추가 kwargs(deepdive Q50 Phase4-f). read_from_replicas 는
    RedisCluster 에만 유효(standalone 엔 전달 금지)."""
    kw = _resilience_kwargs(settings)
    if settings.redis_read_from_replicas:
        kw["read_from_replicas"] = True
    return kw


async def create_redis_client(settings: Settings) -> Redis | RedisCluster:
    """Redis 클라이언트 생성. REDIS_CLUSTER_MODE 환경변수로 모드 결정.
    미설정 시 INFO 명령으로 자동 감지 (Startup Probe).
    """
    kwargs = _resilience_kwargs(settings)

    if settings.redis_cluster_mode is True:
        logger.info("redis_mode", mode="cluster", url=_redact_credentials(settings.redis_url))
        return RedisCluster.from_url(settings.redis_url, **_cluster_kwargs(settings))

    if settings.redis_cluster_mode is False:
        logger.info("redis_mode", mode="standalone", url=_redact_credentials(settings.redis_url))
        return Redis.from_url(settings.redis_url, **kwargs)

    # Auto-detect via Startup Probe — 프로브에도 connect/socket 타임아웃을 줘야
    # 느린 노드/TLS 핸드셰이크에서 pod 부팅이 무한 대기하지 않는다(과거엔 무타임아웃).
    # 끄기(0)로 설정해도 프로브만은 2s/1s 하한을 둬 부팅 무한대기를 막는다.
    probe = Redis.from_url(
        settings.redis_url,
        socket_timeout=(settings.redis_socket_timeout or 2.0),
        socket_connect_timeout=(settings.redis_connect_timeout or 1.0),
    )
    try:
        info = await probe.info("server")
        mode = info.get("redis_mode", "standalone")
    except Exception:
        mode = "standalone"
    finally:
        await probe.aclose()

    logger.info("redis_mode_autodetect", mode=mode, url=_redact_credentials(settings.redis_url))

    if mode == "cluster":
        return RedisCluster.from_url(settings.redis_url, **_cluster_kwargs(settings))

    return Redis.from_url(settings.redis_url, **kwargs)
