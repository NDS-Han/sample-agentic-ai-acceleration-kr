# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""poison-only 배치 XACK 회귀 (R3-11).

배경: ``_consume_live`` 는 ``batch_entries`` 가 비면 ``continue`` 로 다음 배치를
읽었다 — 디코딩에 실패한 메시지의 ID 는 ``batch_ids`` 에 들어있지만 flush 대상이
아니라 XACK 되지 않고 PEL 에 잔류했다. 이 consumer 는 ``>`` 신규만 읽으므로 그
ID 는 다시 읽히지 않고 XAUTOCLAIM 회수 루프만 계속 돌았다. 디코딩 불가 메시지는
재처리해도 영원히 실패하므로 읽는 즉시 ACK(drop)하는 것이 맞다.

고정하는 것: 배치가 **전부 poison** 이면 flush 는 건너뛰되 읽은 ID 는 XACK 한다.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_consumer(redis):
    from worker.stream_consumer import StreamConsumer

    settings = MagicMock()
    settings.cost_stream_key = "cost:stream"
    settings.cost_stream_group = "grp"
    settings.cost_stream_consumer = "c1"
    settings.batch_max_size = 10
    settings.batch_max_interval_sec = 0.2
    settings.xread_block_ms = 50
    settings.xautoclaim_interval_sec = 60
    settings.xautoclaim_min_idle_ms = 1000
    return StreamConsumer(redis=redis, flusher=AsyncMock(), settings=settings)


@pytest.mark.asyncio
async def test_poison_only_batch_is_acked():
    """payload 없는 poison 메시지만 읽힌 배치도 XACK 해 PEL 에 남기지 않는다."""
    redis = AsyncMock()
    poison = [[b"cost:stream", [(b"1-1", {b"not_payload": b"junk"})]]]
    calls = {"n": 0}

    async def xreadgroup(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return poison
        # 두 번째 호출부터는 빈 폴링 — 루프는 timeout 주기로 빠른 종료.
        return None

    redis.xreadgroup = AsyncMock(side_effect=xreadgroup)
    consumer = _make_consumer(redis)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(consumer._consume_live(), timeout=1.0)

    xack_calls = [
        c for c in redis.xack.await_args_list if "1-1" in c.args[2:]
    ]
    assert xack_calls, "poison 메시지 ID 가 XACK 되지 않았다"
    consumer._flusher.flush.assert_not_awaited()
