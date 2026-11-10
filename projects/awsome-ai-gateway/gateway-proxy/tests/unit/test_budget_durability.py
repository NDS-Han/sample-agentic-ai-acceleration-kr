# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""예산 차감 내구성 — 재시도 래퍼, :lost 센티넬, suspect 큐.

배경: finalize 의 budget Lua 실패는 로그만 남고 영구 유실됐다 — Redis 카운터가
budget_usages(집행 원장)보다 얇아져 그 기간 내내 하드 한도가 헐거워진다.
복구 경로는 기존 admission rebuild 트리거(:lost 센티넬)를 재사용한다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import MaxConnectionsError, TimeoutError as RedisTimeoutError

from app.resilience.budget_suspect import (
    SENTINEL_TTL_SECONDS,
    BudgetSuspectSet,
)
from app.resilience.redis_retry import call_with_transient_retry
from app.services.cost_recorder import _mark_budget_lost


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """재시도 백오프를 0 으로 — 테스트는 즉시."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


# ── _eval_budget_op ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_recovers_max_connections_burst():
    """관측된 실패 형태 — 풀고갈 MaxConnectionsError 는 재시도로 흡수된다."""
    redis = AsyncMock()
    redis.eval = AsyncMock(
        side_effect=[MaxConnectionsError("Too many connections"), b"ok"]
    )

    result = await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=False)

    assert result == b"ok"
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_no_retry_timeout_for_non_idempotent():
    """idempotent=False 분류(마커 없는 쓰기)는 TimeoutError 를 재시도하지
    않는다 — 서버 실행 여부가 모호해 재시도하면 이중 적용. 한 번만 호출하고
    바로 올린다. (reserve/deduct 호출부는 마커 멱등이라 idempotent=True 로
    옮겨졌다 — 이 분기는 남은 비멱등 호출을 위한 계약이다.)"""
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=RedisTimeoutError("timeout"))

    with pytest.raises(RedisTimeoutError):
        await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=False)

    assert redis.eval.await_count == 1


@pytest.mark.asyncio
async def test_retry_timeout_for_idempotent_settle():
    """settle 은 마커 멱등 — 모호한 TimeoutError 도 안전하게 재시도한다."""
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=[RedisTimeoutError("timeout"), b"ok"])

    result = await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=True)

    assert result == b"ok"
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_non_retryable_error_propagates_immediately():
    """연결 계열 밖의 오류(로직 버그 등)는 재시도 없이 올린다."""
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=ValueError("bad lua"))

    with pytest.raises(ValueError):
        await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=True)

    assert redis.eval.await_count == 1


@pytest.mark.asyncio
async def test_attempts_exhausted_raises_last_error():
    """버스트가 재시도 창을 넘으면 마지막 오류를 올린다 — 호출부의
    except → 센티넬 경로로 넘어간다."""
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=MaxConnectionsError("down"))

    with pytest.raises(MaxConnectionsError):
        await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=False)

    assert redis.eval.await_count == 3


@pytest.mark.asyncio
async def test_no_retry_generic_connection_error_for_non_idempotent():
    """일반 ConnectionError("Connection closed by server" 등)는 커맨드가
    소켓에 나간 뒤 응답만 끊겼을 수 있다 — 비멱등 재시도는 이중 적용이라
    즉시 올린다. 커맨드 미전송이 확실한 MaxConnectionsError 만 재시도된다."""
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=RedisConnectionError("closed"))

    with pytest.raises(RedisConnectionError):
        await call_with_transient_retry(redis.eval, "script", 1, "k", idempotent=False)

    assert redis.eval.await_count == 1


# ── _mark_budget_lost ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_lost_sets_sentinel_directly():
    """센티넬 SET 이 성공하면 큐는 비어 있다."""
    suspects = BudgetSuspectSet()
    redis = AsyncMock()

    await _mark_budget_lost(
        redis, "budget:user:{u1}:2026-11", suspects=suspects, scope="user"
    )

    redis.set.assert_awaited_once_with(
        "budget:user:{u1}:2026-11:lost", "1", ex=SENTINEL_TTL_SECONDS
    )
    assert suspects.size == 0


@pytest.mark.asyncio
async def test_mark_lost_with_amount_accumulates_lost_amt():
    """차감 실패 금액은 :lost_amt 에 누적된다 — 원장 기록이 늦는 창에
    rebuild 가 센티넬만 지우고 cur+lost_amt 플로어를 적용한다."""
    suspects = BudgetSuspectSet()
    redis = AsyncMock()

    await _mark_budget_lost(
        redis, "budget:user:{u1}:2026-11", suspects=suspects, scope="user",
        amount="0.0234",
    )

    redis.incrbyfloat.assert_awaited_once_with(
        "budget:user:{u1}:2026-11:lost_amt", 0.0234
    )
    redis.expire.assert_awaited_once_with(
        "budget:user:{u1}:2026-11:lost_amt", SENTINEL_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_mark_lost_enqueues_when_sentinel_write_fails():
    """센티넬 SET 마저 실패하면(실제 장애) suspect 큐로 넘긴다."""
    suspects = BudgetSuspectSet()
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=RedisConnectionError("down"))

    await _mark_budget_lost(
        redis, "budget:user:{u1}:2026-11", suspects=suspects, scope="user"
    )

    assert suspects.size == 1


@pytest.mark.asyncio
async def test_mark_lost_without_suspects_still_safe():
    """suspects 미연결(테스트/조기 경로)이어도 예외를 올리지 않는다."""
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=RedisConnectionError("down"))

    await _mark_budget_lost(
        redis, "budget:user:{u1}:2026-11", suspects=None, scope="user"
    )


# ── BudgetSuspectSet ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_delivers_sentinels_for_queued_keys():
    suspects = BudgetSuspectSet()
    suspects.enqueue("budget:user:{u1}:2026-11")
    suspects.enqueue("budget:team:{t1}:2026-11")
    redis = AsyncMock()

    delivered = await suspects.drain(redis)

    assert delivered == 2
    assert suspects.size == 0
    calls = {c.args[0] for c in redis.set.await_args_list}
    assert calls == {
        "budget:user:{u1}:2026-11:lost",
        "budget:team:{t1}:2026-11:lost",
    }


@pytest.mark.asyncio
async def test_drain_rebuffers_on_redis_failure():
    """실패한 키는 큐로 되돌아간다 — 다음 복구 틱이 다시 심는다."""
    suspects = BudgetSuspectSet()
    suspects.enqueue("budget:user:{u1}:2026-11")
    suspects.enqueue("budget:user:{u2}:2026-11")
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=RedisConnectionError("still down"))

    delivered = await suspects.drain(redis)

    assert delivered == 0
    # 첫 키 실패 시 멈춘다 — Redis 가 아직 나쁜데 나머지를 태우지 않는다.
    assert suspects.size == 2


@pytest.mark.asyncio
async def test_drain_partial_then_recovers_next_tick():
    """중간 실패 → 남은 건 큐에 남고, 다음 틱에 이어서 배달된다."""
    suspects = BudgetSuspectSet()
    suspects.enqueue("budget:user:{u1}:2026-11")
    suspects.enqueue("budget:user:{u2}:2026-11")
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=[None, RedisConnectionError("flap"), None])

    assert await suspects.drain(redis) == 1
    assert suspects.size == 1
    assert await suspects.drain(redis) == 1
    assert suspects.size == 0


@pytest.mark.asyncio
async def test_enqueue_dedupes_queued_key():
    """같은 키가 큐에 있는 동안 재실패해도 한 번만 배달된다 — 센티넬 SET 은
    멱등이라 두 번째 실패가 새 정보를 주지 않는다."""
    suspects = BudgetSuspectSet()
    suspects.enqueue("k1")
    suspects.enqueue("k1")
    suspects.enqueue("k2")
    suspects.enqueue("k1")

    assert suspects.size == 2
    redis = AsyncMock()
    assert await suspects.drain(redis) == 2


@pytest.mark.asyncio
async def test_enqueue_bounded_drops_oldest_visibly():
    """유한 큐 — 넘치면 가장 오래된 것을 버리고 카운트한다."""
    suspects = BudgetSuspectSet(maxlen=2)
    suspects.enqueue("k1")
    suspects.enqueue("k2")
    suspects.enqueue("k3")

    assert suspects.size == 2
    assert suspects.dropped == 1
    redis = AsyncMock()
    await suspects.drain(redis)
    sent = {c.args[0] for c in redis.set.await_args_list}
    # k1 이 버려졌고 k2·k3 만 배달된다 — 손실이 침묵이 아니라 카운트된다.
    assert sent == {"k2:lost", "k3:lost"}


# ── finalize 통합 — deduct 실패가 센티넬 경로로 이어지는지 ──────────────────


@pytest.mark.asyncio
async def test_finalize_marks_lost_when_deduct_fails_persistently(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """재시도도 소진하는 deduct 실패 → :lost 센티넬이 심어져 다음
    admission 이 원장 기준 rebuild 를 발화한다."""
    from app.services.lua_loader import LuaScriptLoader
    from app.schemas.domain import TokenUsage
    from app.services.cost_recorder import CostRecorder

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"
    LuaScriptLoader._scripts["budget_settle"] = "-- mock"

    suspects = BudgetSuspectSet()
    # eval 은 계속 실패, set(센티넬)은 성공 — 관측된 버스트 형태(무거운 eval 은
    # 풀을 못 얻어도 가벼운 SET 은 통과).
    mock_redis.eval = AsyncMock(side_effect=MaxConnectionsError("pool burst"))
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder(suspects=suspects)
    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(input_tokens=500, output_tokens=300, total_tokens=800),
        request_id="req-burst",
        is_stream=False,
        duration_ms=150,
    )

    sentinel_calls = [
        c for c in mock_redis.set.await_args_list
        if str(c.args[0]).endswith(":lost")
    ]
    assert sentinel_calls, "차감 실패 카운터에 :lost 센티넬이 안 심어졌다"
    assert suspects.size == 0


@pytest.mark.asyncio
async def test_finalize_enqueues_suspect_when_even_sentinel_fails(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """eval 실패 + 센티넬 SET 도 실패 → suspect 큐로 넘긴다."""
    from app.services.lua_loader import LuaScriptLoader
    from app.schemas.domain import TokenUsage
    from app.services.cost_recorder import CostRecorder

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"
    LuaScriptLoader._scripts["budget_settle"] = "-- mock"

    suspects = BudgetSuspectSet()
    mock_redis.eval = AsyncMock(side_effect=MaxConnectionsError("pool burst"))
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(side_effect=RedisConnectionError("down"))
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder(suspects=suspects)
    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(input_tokens=500, output_tokens=300, total_tokens=800),
        request_id="req-outage",
        is_stream=False,
        duration_ms=150,
    )

    assert suspects.size >= 1, "센티넬마저 실패했는데 suspect 큐가 비어 있다"
