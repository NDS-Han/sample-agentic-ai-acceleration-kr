# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Transient-error retry for Redis accounting ops.

클라이언트 레벨 ``Retry(retries=1, ~50ms)`` 는 blip 용이라, 관측된 풀고갈
버스트(~수 초, ``MaxConnectionsError`` — 요청 종결이 몰려 finalize/admission
명령이 pool 을 한꺼번에 요구하는 형태)를 못 견딘다. 회계 경로의 명령을
호출부에서 좁게 재시도한다 — 전역 ``redis_retries`` 상향은 핫패스(auth·
rate-limit 읽기) 지연 상한까지 올리므로 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

_BUDGET_OP_ATTEMPTS = 3
_BUDGET_OP_BACKOFF_S = (0.05, 0.15)  # 재시도 1·2차 지연(초) — 지터 곱해 사용


async def call_with_transient_retry(
    call: Callable[..., Awaitable[Any]], *args, idempotent: bool
) -> Any:
    """Redis awaitable(exists/get/eval …)에 일시 오류 한정 재시도를 얹는다.

    idempotent=False(비멱등 변경 — INCRBYFLOAT 계열: budget_deduct·
    budget_reserve): ``ConnectionError`` 계열만 재시도한다.
    ``MaxConnectionsError`` 는 풀이 커넥션을 못 내줘 커맨드가 소켓에
    한 번도 안 나간 상태라 재실행이 안전하다. ``TimeoutError`` 는 서버
    실행 후 응답만 유실됐을 수 있어 재시도하면 이중 적용 — 제외한다.
    idempotent=True(마커 멱등 eval·읽기 전용 명령): ``TimeoutError`` 도
    재시도한다 — 멱등성/무상태성이 이중 적용을 차단한다.
    """
    retryable = (
        (RedisConnectionError, RedisTimeoutError)
        if idempotent
        else (RedisConnectionError,)
    )
    last: Exception | None = None
    for attempt in range(_BUDGET_OP_ATTEMPTS):
        try:
            return await call(*args)
        except retryable as e:
            last = e
            if attempt < _BUDGET_OP_ATTEMPTS - 1:
                delay = _BUDGET_OP_BACKOFF_S[
                    min(attempt, len(_BUDGET_OP_BACKOFF_S) - 1)
                ]
                await asyncio.sleep(delay * (0.5 + random.random()))
    assert last is not None  # for loop 가 빈손으로 끝날 수 없다
    raise last
