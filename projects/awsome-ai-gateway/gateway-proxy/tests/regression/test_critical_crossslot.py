# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: CROSSSLOT bug — rate_limit_service must never bundle keys from
different hash slots in a single redis.eval call.

Bug: USER/TEAM/GLOBAL keys were passed together in one eval() call.
Fix: Per-scope calls — each eval receives keys from a single hash slot only.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.rate_limit_service import RateLimitService
from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor


def _crc16_slot(key: str) -> int:
    """Compute Redis Cluster hash slot for a key (simplified CRC16 mod 16384).

    Respects {hashtag} convention.
    """
    # Extract hash tag if present
    start = key.find("{")
    if start != -1:
        end = key.find("}", start + 1)
        if end != -1 and end != start + 1:
            key = key[start + 1 : end]

    # CRC16/CCITT (Redis uses XMODEM variant)
    crc = 0
    for ch in key.encode():
        crc ^= ch << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc % 16384


@pytest.mark.asyncio
async def test_multi_scope_rpm_calls_eval_per_scope_no_crossslot():
    """Each redis.eval call must receive keys from only ONE hash slot."""
    eval_calls: list[tuple] = []

    async def track_eval(*args):
        eval_calls.append(args)
        return json.dumps(
            {"allowed": True, "remaining": 50, "limit": 60, "retry_after": None, "window_reset": 0}
        ).encode()

    redis = AsyncMock()
    redis.eval = track_eval

    descriptors = [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id="user-001",
            model_alias="claude-sonnet",
            rpm_limit=60,
        ),
        ScopeDescriptor(
            scope=RateLimitScope.TEAM,
            scope_id="team-001",
            model_alias="claude-sonnet",
            rpm_limit=200,
        ),
        ScopeDescriptor(
            scope=RateLimitScope.GLOBAL,
            scope_id="global",
            model_alias="claude-sonnet",
            rpm_limit=1000,
        ),
    ]

    svc = RateLimitService()
    # 공용 Redis 브레이커는 conftest 가 닫아 둔다 — 성공 eval 은 그대로 통과한다.
    with patch("app.services.rate_limit_service.LuaScriptLoader") as mock_lua:
        mock_lua.get.return_value = "-- dummy lua script"
        result = await svc.check_multi_scope_rpm(redis, descriptors, request_id="req-1")

    assert result.allowed is True

    # Verify we made separate eval calls (one per scope)
    assert len(eval_calls) == 3, f"Expected 3 separate eval calls, got {len(eval_calls)}"

    # Each call should have keys belonging to a single hash slot
    for call_args in eval_calls:
        # call_args: (script, num_keys, key1, ..., argv...)
        script = call_args[0]
        num_keys = call_args[1]
        keys = call_args[2 : 2 + num_keys]

        if len(keys) > 1:
            slots = {_crc16_slot(k) for k in keys}
            assert len(slots) == 1, (
                f"CROSSSLOT detected: keys {keys} map to slots {slots}"
            )


@pytest.mark.asyncio
async def test_multi_scope_tpm_calls_eval_per_scope_no_crossslot():
    """TPM check: each redis.eval must receive keys from only ONE hash slot."""
    eval_calls: list[tuple] = []

    async def track_eval(*args):
        eval_calls.append(args)
        return json.dumps(
            {"allowed": True, "remaining": 5000, "limit": 10000, "retry_after": None, "window_reset": 0}
        ).encode()

    redis = AsyncMock()
    redis.eval = track_eval

    descriptors = [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id="user-001",
            model_alias="claude-sonnet",
            tpm_limit=10000,
        ),
        ScopeDescriptor(
            scope=RateLimitScope.TEAM,
            scope_id="team-001",
            model_alias="claude-sonnet",
            tpm_limit=50000,
        ),
    ]

    svc = RateLimitService()
    # 공용 Redis 브레이커는 conftest 가 닫아 둔다 — 성공 eval 은 그대로 통과한다.
    with patch("app.services.rate_limit_service.LuaScriptLoader") as mock_lua:
        mock_lua.get.return_value = "-- dummy lua script"
        result = await svc.check_multi_scope_tpm(redis, descriptors, reserved_tokens=500)

    assert result.allowed is True
    # One eval per scope
    assert len(eval_calls) == 2

    for call_args in eval_calls:
        num_keys = call_args[1]
        keys = call_args[2 : 2 + num_keys]
        slots = {_crc16_slot(k) for k in keys}
        assert len(slots) == 1, (
            f"CROSSSLOT detected in TPM: keys {keys} map to slots {slots}"
        )
