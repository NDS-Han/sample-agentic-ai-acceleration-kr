# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.services.circuit_breaker import CircuitBreakerService


@pytest.mark.asyncio
async def test_opens_after_min_calls_failures(fake_redis):
    cb = CircuitBreakerService(window_sec=30, min_calls=5, error_rate=0.5, open_sec=30, halfopen_ttl_ms=8000)
    pmid = "global.anthropic.claude-opus-4-8"
    for _ in range(4):
        await cb.record_failure(fake_redis, pmid)
    assert await cb.is_open(fake_redis, pmid) is False
    await cb.record_failure(fake_redis, pmid)
    assert await cb.is_open(fake_redis, pmid) is True


@pytest.mark.asyncio
async def test_fail_open_when_redis_none():
    cb = CircuitBreakerService()
    assert await cb.is_open(None, "x") is False        # Redis down => closed
    assert await cb.record_failure(None, "x") is False  # no-op, no raise
    assert await cb.record_success(None, "x") is False  # no-op, no raise
    assert await cb.try_acquire_halfopen_probe(None, "x") is True  # let request through


@pytest.mark.asyncio
async def test_fail_open_on_redis_exception():
    cb = CircuitBreakerService()
    redis = MagicMock()
    redis.exists = AsyncMock(side_effect=ConnectionError("boom"))
    redis.eval = AsyncMock(side_effect=ConnectionError("boom"))
    redis.time = AsyncMock(side_effect=ConnectionError("boom"))
    redis.set = AsyncMock(side_effect=ConnectionError("boom"))
    redis.delete = AsyncMock(side_effect=ConnectionError("boom"))
    assert await cb.is_open(redis, "x") is False
    assert await cb.record_failure(redis, "x") is False
    assert await cb.record_success(redis, "x") is False  # must not raise even though delete raises
    assert await cb.try_acquire_halfopen_probe(redis, "x") is True


@pytest.mark.asyncio
async def test_halfopen_single_flight(fake_redis):
    cb = CircuitBreakerService(halfopen_ttl_ms=8000)
    pmid = "m"
    first = await cb.try_acquire_halfopen_probe(fake_redis, pmid)
    second = await cb.try_acquire_halfopen_probe(fake_redis, pmid)
    assert first is True and second is False


@pytest.mark.asyncio
async def test_plain_success_does_not_close_open(fake_redis):
    """R22 M4 — 회로가 열리기 전 시작된 in-flight 요청의 늦은 성공이 open 을
    지워 flap 하면 안 된다. probe_token 없는 성공은 카운터만 기록한다."""
    cb = CircuitBreakerService()
    pmid = "m"
    await fake_redis.set(f"cb:{{{pmid}}}:open", "1")
    result = await cb.record_success(fake_redis, pmid)
    assert result is True  # Lua 는 open 존재 시 1 — 즉 회로가 그대로 열려 있다
    assert await fake_redis.exists(f"cb:{{{pmid}}}:open") == 1


@pytest.mark.asyncio
async def test_probe_owner_success_closes_open(fake_redis):
    """half-open 프로브 소유자의 성공만 open + 슬롯을 함께 지운다."""
    cb = CircuitBreakerService(halfopen_ttl_ms=8000)
    pmid = "m"
    await fake_redis.set(f"cb:{{{pmid}}}:open", "1")
    assert await cb.try_acquire_halfopen_probe(fake_redis, pmid, token="tok-1") is True
    await cb.record_success(fake_redis, pmid, probe_token="tok-1")
    assert await fake_redis.exists(f"cb:{{{pmid}}}:open") == 0
    assert await fake_redis.exists(f"cb:{{{pmid}}}:halfopen") == 0


@pytest.mark.asyncio
async def test_stale_probe_token_closes_open_but_keeps_new_probe(fake_redis):
    """스테일 토큰도 open 은 닫는다(프로브 성공 자체가 회복 증거) — 다만 다른
    파드가 따낸 halfopen 슬롯은 지우지 않는다(R22 r2 N1)."""
    cb = CircuitBreakerService(halfopen_ttl_ms=8000)
    pmid = "m"
    await fake_redis.set(f"cb:{{{pmid}}}:open", "1")
    await fake_redis.set(f"cb:{{{pmid}}}:halfopen", "tok-other")
    await cb.record_success(fake_redis, pmid, probe_token="tok-stale")
    assert await fake_redis.exists(f"cb:{{{pmid}}}:open") == 0
    assert await fake_redis.get(f"cb:{{{pmid}}}:halfopen") == "tok-other"
