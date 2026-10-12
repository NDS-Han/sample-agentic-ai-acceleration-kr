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


@pytest.mark.asyncio
async def test_trimmed_entry_fields_none_is_acked():
    """MAXLEN 트림으로 본문이 사라진 PEL entry(fields=None)도 XACK 한다.

    XAUTOCLAIM/XREADGROUP 은 PEL 만 남고 스트림 본문이 잘린 entry 를
    ``(id, None)`` 으로 돌려준다. fields.get 이 AttributeError 로 소비 루프를
    죽이기 전에 decode 단계에서 분류해 ACK 해야 한다 — 복구 불가 데이터라
    재처리해도 영원히 실패한다.
    """
    redis = AsyncMock()
    trimmed = [[b"cost:stream", [(b"3-3", None)]]]
    calls = {"n": 0}

    async def xreadgroup(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return trimmed
        return None

    redis.xreadgroup = AsyncMock(side_effect=xreadgroup)
    consumer = _make_consumer(redis)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(consumer._consume_live(), timeout=1.0)

    xack_calls = [c for c in redis.xack.await_args_list if "3-3" in c.args[2:]]
    assert xack_calls, "trimmed 메시지 ID 가 XACK 되지 않았다"
    consumer._flusher.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_failure_dead_letters_after_grace():
    """같은 배치 머리가 grace 를 넘어 flush 실패하면 행 단위 소화로 넘긴다.

    결정적 오류(예: UndefinedColumn)는 재시도로 절대 낫지 않는다 — grace
    안에서는 전파해 PEL 재처리(at-least-once)를 지키고, 초과하면 행 단위
    소화 + ACK 으로 스트림을 진행시킨다. 안 하면 MAXLEN 트림으로 뒤의
    정상 entry 까지 전량 유실된다.
    """
    redis = AsyncMock()
    consumer = _make_consumer(redis)
    consumer._dead_letter_grace = 0.0  # grace 0 → 두 번째 실패에서 즉시 dead-letter
    consumer._flusher.flush = AsyncMock(side_effect=RuntimeError("undefined column"))
    consumer._flusher.flush_dead_letter = AsyncMock(return_value=0)

    entries, ids = [MagicMock()], ["1-1"]
    with pytest.raises(RuntimeError):
        await consumer._flush_or_dead_letter(entries, ids)
    # 같은 head 의 두 번째 실패 — grace 초과 → 행 단위 소화 후 스트림 진행.
    await consumer._flush_or_dead_letter(entries, ids)
    consumer._flusher.flush_dead_letter.assert_awaited_once()
    # 배치 머리가 바뀌면 카운터가 리셋된다 — 새 배치는 다시 grace 를 받는다.
    with pytest.raises(RuntimeError):
        await consumer._flush_or_dead_letter([MagicMock()], ["2-1"])


@pytest.mark.asyncio
async def test_transient_failure_propagates_within_grace():
    """grace 안의 실패는 dead-letter 없이 전파 — PEL 재처리(at-least-once) 유지."""
    redis = AsyncMock()
    consumer = _make_consumer(redis)
    consumer._dead_letter_grace = 3600.0
    consumer._flusher.flush = AsyncMock(side_effect=RuntimeError("db down"))

    with pytest.raises(RuntimeError):
        await consumer._flush_or_dead_letter(
            entries=[MagicMock()], ids=["1-1"]
        )
    # 두 번째도 같은 head 인데 grace 안 — 역시 전파(dead-letter 아님).
    with pytest.raises(RuntimeError):
        await consumer._flush_or_dead_letter(entries=[MagicMock()], ids=["1-1"])
    consumer._flusher.flush_dead_letter.assert_not_called()
