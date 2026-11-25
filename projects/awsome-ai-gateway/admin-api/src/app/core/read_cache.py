# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""분석 읽기 캐시 — Redis 결과 캐시 + 질의 키 단위 single-flight.

## 왜
관리자 화면 하나가 위젯 여러 개를 동시에 부르고, 여러 관리자가 같은 화면을 연다. 같은 질의가
동시에 N 번 DB 로 가면 분석 풀(작다)이 그 N 개로 찬다. 그래서 같은 키는
  1. Redis 에 결과가 있으면 그것을 쓰고,
  2. 같은 프로세스의 동시 요청은 하나의 계산(asyncio Future)을 함께 기다리고,
  3. 다른 pod 의 동시 요청은 Redis `SET NX` 잠금으로 한 pod 만 계산하고 나머지는 결과를
     기다린다(최대 wait 초 — 넘으면 스스로 계산한다. 잠금은 정합성 장치가 아니라 부하 장치다).

## 실패 시
Redis 오류는 전부 삼킨다(fail-open) — 캐시는 성능 장치이지 정합성의 근거가 아니다.

## 큰 값
사용자 × 모델 스코프 결과는 3 MB 규모다. 직렬화·역직렬화는 이벤트 루프 밖(스레드)에서 한다 —
루프에서 json.loads 하면 그동안 같은 프로세스의 모든 요청(로그인 포함)이 멈춘다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.encoders import jsonable_encoder

logger = logging.getLogger(__name__)

#: 이 크기(바이트)를 넘는 JSON 은 스레드에서 (역)직렬화한다.
_THREAD_JSON_BYTES = 64 * 1024
#: 다른 pod 가 계산 중일 때 결과를 기다리는 간격.
_POLL_SEC = 0.1

_inflight: dict[str, asyncio.Future] = {}


async def _loads(raw: bytes | str) -> Any:
    if len(raw) > _THREAD_JSON_BYTES:
        return await asyncio.to_thread(json.loads, raw)
    return json.loads(raw)


def _dump(value: Any) -> tuple[Any, str]:
    plain = jsonable_encoder(value)
    return plain, json.dumps(plain, separators=(",", ":"))


async def _dumps(value: Any) -> tuple[Any, str]:
    # 크기를 미리 알 수 없으므로 dict 의 대략적 크기로 판단하지 않고 항상 스레드로 보낸다 —
    # 작은 값에서는 스레드 전환 비용(수십 µs)이 무시할 만하다.
    return await asyncio.to_thread(_dump, value)


class _LocalLRU:
    """역직렬화된 큰 값의 프로세스 로컬 캐시(TTL) — 페이지/정렬 요청마다 3 MB 를 다시 파싱하지 않게."""

    def __init__(self, size: int = 8) -> None:
        self._size = size
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return hit[1]

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, value)
        self._d.move_to_end(key)
        while len(self._d) > self._size:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()


local_lru = _LocalLRU()


async def _redis_get(redis, key: str) -> Any | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — 캐시 실패는 요청 실패가 아니다
        logger.debug("read cache get failed key=%s err=%s", key, exc)
        return None
    if not raw:
        return None
    try:
        value = await _loads(raw)
    except (ValueError, TypeError):
        return None
    # dict 가 아니면 깨진 엔트리다(예: str() repr 로 저장된 값) — miss 로 재계산.
    return value if isinstance(value, dict) else None


async def _redis_set(redis, key: str, text_value: str, ttl: int) -> None:
    if redis is None:
        return
    try:
        await redis.setex(key, ttl, text_value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("read cache set failed key=%s err=%s", key, exc)


async def _try_lock(redis, key: str, token: str, ttl_ms: int) -> bool:
    if redis is None:
        return True
    try:
        return bool(await redis.set(f"{key}:lock", token, nx=True, px=ttl_ms))
    except Exception:  # noqa: BLE001 — Redis 가 없으면 각자 계산
        return True


async def _unlock(redis, key: str, token: str) -> None:
    if redis is None:
        return
    try:
        # 내 토큰일 때만 지운다 — 만료 후 다른 pod 가 잡은 잠금을 지우지 않게.
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
            "else return 0 end", 1, f"{key}:lock", token)
    except Exception:  # noqa: BLE001
        pass


async def cached(
    redis,
    key: str,
    compute: Callable[[], Awaitable[Any]],
    *,
    ttl: int,
    wait: float = 25.0,
    keep_local: bool = False,
) -> Any:
    """``key`` 의 결과를 돌려준다 — Redis 적중 / 같은 프로세스 동시 요청 공유 / pod 간 잠금.

    compute() 의 결과는 JSON 직렬화 가능한 값(pydantic 모델 포함)이어야 한다. 반환값은
    적중·계산 어느 쪽이든 같은 형태(jsonable_encoder 결과, 즉 dict/list/원시값)다.
    keep_local=True 면 역직렬화된 값을 프로세스 로컬 LRU 에도 TTL 동안 둔다(큰 값용).
    """
    if keep_local and (hit := local_lru.get(key)) is not None:
        return hit
    if (hit := await _redis_get(redis, key)) is not None:
        if keep_local:
            local_lru.put(key, hit, ttl)
        return hit

    fut = _inflight.get(key)
    if fut is not None:
        return await asyncio.shield(fut)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _inflight[key] = fut
    try:
        token = uuid.uuid4().hex
        locked = await _try_lock(redis, key, token, int((wait + 5) * 1000))
        if not locked:
            # 다른 pod 가 계산 중 — 결과가 Redis 에 오를 때까지 기다린다(최대 wait 초).
            deadline = loop.time() + wait
            while loop.time() < deadline:
                await asyncio.sleep(_POLL_SEC)
                if (hit := await _redis_get(redis, key)) is not None:
                    fut.set_result(hit)
                    if keep_local:
                        local_lru.put(key, hit, ttl)
                    return hit
            logger.info("read cache wait timed out — computing locally key=%s", key)
        try:
            value = await compute()
            plain, text_value = await _dumps(value)
            await _redis_set(redis, key, text_value, ttl)
            if keep_local:
                local_lru.put(key, plain, ttl)
            fut.set_result(plain)
            return plain
        finally:
            if locked:
                await _unlock(redis, key, token)
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
            # 기다리는 쪽이 없으면 "Future exception was never retrieved" 경고가 난다.
            fut.exception()
        raise
    finally:
        _inflight.pop(key, None)


def ttl_for(range_end, today, settings=None) -> int:
    """오늘이 포함된 범위는 짧게, 다 지난 범위는 길게."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    return (settings.ANALYTICS_CACHE_TTL_SEC if range_end >= today
            else settings.ANALYTICS_CACHE_TTL_PAST_SEC)
