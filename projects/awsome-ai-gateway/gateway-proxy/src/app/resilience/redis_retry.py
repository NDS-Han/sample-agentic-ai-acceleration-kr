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
from redis.exceptions import MaxConnectionsError
from redis.exceptions import TimeoutError as RedisTimeoutError

_BUDGET_OP_ATTEMPTS = 3
_BUDGET_OP_BACKOFF_S = (0.05, 0.15)  # 재시도 1·2차 지연(초) — 지터 곱해 사용


async def call_with_transient_retry(
    call: Callable[..., Awaitable[Any]], *args, idempotent: bool
) -> Any:
    """Redis awaitable(exists/get/eval …)에 일시 오류 한정 재시도를 얹는다.

    idempotent=False(비멱등 변경 — 마커 없는 INCRBYFLOAT 계열):
    ``MaxConnectionsError`` 만 재시도한다. 풀이 커넥션을
    못 내줘 커맨드가 소켓에 한 번도 안 나간 상태라 재실행이 안전하다.
    그 외 ``ConnectionError``("Connection closed by server", reset 등)는
    서버가 커맨드를 이미 실행한 뒤 응답만 유실됐을 수 있어 비멱등 재시도는
    이중 적용이다 — 제외한다. ``TimeoutError`` 도 같은 이유로 제외한다.
    (budget_deduct/reserve 는 request_id 마커를 얻은 뒤 idempotent=True
    분류로 옮겼다 — 아래)
    idempotent=True(request_id 마커 멱등 eval·읽기 전용 명령):
    ``TimeoutError``·``ConnectionError`` 도 재시도한다 — 서버측 멱등
    마커(budget_reserve 의 pending marker, budget_deduct 의 ``:ded:``
    키)나 무상태성이 이중 적용을 차단한다. reserve/deduct eval 호출부는
    이제 이 분류다 — 응답 유실 재시도는 Lua 가 ``dedup_hit`` 재생으로
    변환하므로 lost_amt 복구 경로로 빠지지 않는다.

    클라이언트 레벨 ``retry_on_error=[TimeoutError, ConnectionError]``
    (redis_client.py, retries=1)의 1회 재실행도 reserve/deduct 에
    대해서는 같은 Lua 멱등 마커가 흡수한다 — 비멱등 호출부만 이 분류를
    유지하면 된다.
    """
    retryable = (
        (RedisConnectionError, RedisTimeoutError)
        if idempotent
        else (MaxConnectionsError,)
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
