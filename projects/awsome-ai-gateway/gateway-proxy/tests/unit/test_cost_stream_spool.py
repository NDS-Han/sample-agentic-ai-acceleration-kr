# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""P0-②: cost:stream dead-letter spool — XADD failure must not lose the record.

Covers:
- spool enqueue/drain semantics (FIFO, re-XADD, interrupted drain re-buffers)
- bounded drop-oldest with visible counter
- CostRecorder enqueues the payload on XADD failure when a spool is wired
- CostRecorder does NOT raise on XADD failure (gateway response already sent)
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.resilience.cost_stream_spool import CostStreamSpool


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_republishes_in_fifo_order():
    spool = CostStreamSpool(stream_key="cost:stream")
    spool.enqueue("p1")
    spool.enqueue("p2")
    spool.enqueue("p3")

    redis = MagicMock()
    redis.xadd = AsyncMock()

    drained = await spool.drain(redis)

    assert drained == 3
    assert spool.size == 0
    published = [c.kwargs.get("fields") or c.args for c in redis.xadd.call_args_list]
    # payload values in order
    seen = [c.args[1]["payload"] for c in redis.xadd.call_args_list]
    assert seen == ["p1", "p2", "p3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_interrupted_rebuffers_remaining():
    spool = CostStreamSpool(stream_key="cost:stream")
    spool.enqueue("p1")
    spool.enqueue("p2")
    spool.enqueue("p3")

    redis = MagicMock()
    # first xadd ok, second raises → drain stops, p2+p3 remain
    redis.xadd = AsyncMock(side_effect=[None, RuntimeError("redis down"), None])

    drained = await spool.drain(redis)

    assert drained == 1
    assert spool.size == 2  # p2, p3 re-buffered (front)
    # next successful drain finishes the rest in order
    redis.xadd = AsyncMock()
    drained2 = await spool.drain(redis)
    assert drained2 == 2
    seen = [c.args[1]["payload"] for c in redis.xadd.call_args_list]
    assert seen == ["p2", "p3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_enqueue_during_drain_does_not_lose_inflight():
    """P0-② (Codex review #2): pop-and-hold. The in-flight payload is popped into
    a LOCAL var before the await, so a concurrent enqueue (even at capacity)
    cannot evict it. On XADD FAILURE the in-flight item is appendleft'd back and
    is NOT lost."""
    spool = CostStreamSpool(stream_key="cost:stream", maxlen=2)
    spool.enqueue("p1")
    spool.enqueue("p2")  # at capacity: [p1, p2]

    calls = {"n": 0}

    async def _xadd(stream, fields, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # While p1 (in-flight, in a local var) is being published, a concurrent
            # request enqueues p3 at capacity → enqueue drops the OLDEST present
            # (p2), NOT the in-flight p1. Then make THIS xadd fail.
            spool.enqueue("p3")
            raise RuntimeError("redis blip mid-publish")
        return None

    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=_xadd)

    drained = await spool.drain(redis)

    # First xadd failed → p1 re-buffered, drain stopped. p1 NOT lost.
    assert drained == 0
    assert "p1" in list(spool._buf)
    # A subsequent healthy drain re-publishes p1 (FIFO front).
    redis.xadd = AsyncMock()
    await spool.drain(redis)
    published = [c.args[1]["payload"] for c in redis.xadd.call_args_list]
    assert published[0] == "p1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_during_xadd_does_not_strand_payload():
    """P0-② (Codex review #3): if drain() is cancelled while awaiting xadd (e.g.
    health-check task cancelled on shutdown), CancelledError is a BaseException
    not caught by `except Exception` — the `finally` must put the in-flight
    payload back so it is never stranded outside the buffer."""
    import asyncio

    spool = CostStreamSpool(stream_key="cost:stream")
    spool.enqueue("p1")
    spool.enqueue("p2")

    async def _xadd(stream, fields, **kw):
        raise asyncio.CancelledError()

    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=_xadd)

    with pytest.raises(asyncio.CancelledError):
        await spool.drain(redis)

    # p1 (in-flight when cancelled) was put back; nothing lost.
    assert "p1" in list(spool._buf)
    assert spool.size == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_noop_when_redis_none_or_empty():
    spool = CostStreamSpool(stream_key="cost:stream")
    assert await spool.drain(None) == 0
    assert await spool.drain(MagicMock(xadd=AsyncMock())) == 0  # empty


@pytest.mark.unit
def test_bounded_drop_oldest_counts():
    spool = CostStreamSpool(stream_key="cost:stream", maxlen=2)
    counter = MagicMock()
    spool.set_metrics(counter)
    spool.enqueue("p1")
    spool.enqueue("p2")
    spool.enqueue("p3")  # evicts p1

    assert spool.size == 2
    assert spool.dropped == 1
    counter.add.assert_called_once_with(1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_recorder_enqueues_payload_on_xadd_failure():
    """When XADD fails, CostRecorder spools the payload and does NOT raise."""
    from app.schemas.domain import (
        ApiFormat,
        AuthContext,
        AuthType,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
        Role,
        TokenUsage,
    )
    from app.services.cost_recorder import CostRecorder

    spool = CostStreamSpool(stream_key="cost:stream")
    recorder = CostRecorder(metrics=None, spool=spool)

    redis = MagicMock()
    # budget/settle evals succeed; XADD raises (Redis down for stream write).
    redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    redis.xadd = AsyncMock(side_effect=RuntimeError("redis down"))

    auth = AuthContext(
        user_id="u1", team_id="t1", dept_id="d1", roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY, key_id="k1",
    )
    model = ModelConfigSchema(
        provider_model_id="anthropic.claude-x", alias="claude-x",
        provider=ProviderType.BEDROCK, api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="", pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )
    usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)

    # Must NOT raise even though XADD failed.
    cost = await recorder.finalize(
        redis, auth, model, usage, request_id="req-xyz",
        is_stream=False, duration_ms=42,
    )

    assert cost > Decimal("0")
    # The failed payload was spooled (request_id present in the buffered JSON).
    assert spool.size == 1
    buffered = spool._buf[0]
    assert "req-xyz" in buffered


def test_make_uses_request_start_for_bucket_labels():
    """F5: period/date/requested_at 은 완료 시각이 아니라 요청 시작 시각에서 파생.

    월 경계를 넘긴 스트리밍(시작 9월 KST → 완료 10월 KST)이 Redis 카운터
    (request_period, 시작 월)와 다른 budget_usages 행(완료 월)에 귀속되던
    불일치를 막는다.
    """
    from datetime import datetime

    from app.schemas.cost_stream import CostStreamEntry

    # KST 2026-10-01 00:30 완료, 요청 시작은 KST 2026-09-30 23:55
    # (= UTC 2026-09-30 14:55)
    started = datetime.fromisoformat("2026-09-30T14:55:00+00:00")
    entry = CostStreamEntry.make(
        request_id="r1",
        user_id="u",
        team_id="t",
        dept_id="d",
        model_alias="m",
        provider="BEDROCK",
        input_tokens=1,
        output_tokens=1,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd="0.001",
        latency_ms=100,
        is_streaming=True,
        estimated_usage=False,
        downgraded_from=None,
        requested_at=started,
    )
    assert entry.requested_at == started.isoformat()
    assert entry.period == "2026-09"  # 완료 월(2026-10)이 아니라 시작 월
    assert entry.date == "2026-09-30"


def test_make_without_start_falls_back_to_now():
    """미들웨어 우회(테스트 등) 시에는 완료 시각 버킷으로 폴백."""
    from app.schemas.cost_stream import CostStreamEntry

    entry = CostStreamEntry.make(
        request_id="r1",
        user_id="u",
        team_id="t",
        dept_id="d",
        model_alias="m",
        provider="BEDROCK",
        input_tokens=1,
        output_tokens=1,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd="0.001",
        latency_ms=1,
        is_streaming=False,
        estimated_usage=False,
        downgraded_from=None,
    )
    assert entry.requested_at == entry.completed_at
    assert entry.period and entry.date
