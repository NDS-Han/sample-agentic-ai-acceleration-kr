# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: Fallback loop exhaustion must return proper error, not crash.

Bug: When all candidates exhausted in streaming mode, fallback_loop returned
bytes in payload[0] which was then treated as an async iterator.
Fix: Guard with isinstance check, FallbackResult returns bytes body properly.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.services.fallback_loop import FallbackResult, run_fallback_loop


def _make_model_config(alias: str = "claude-sonnet") -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="us-east-1",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
        ),
        status=ModelStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_all_candidates_exhausted_returns_bytes_not_iterator():
    """When all candidates fail with 503, the result must have bytes payload,
    not an async iterator that would crash the caller."""
    mock_adapter = AsyncMock()
    # All candidates return 503
    mock_adapter.invoke_stream = AsyncMock(
        return_value=(503, AsyncMock(), {}, "req-123")
    )
    mock_adapter.invoke = AsyncMock(
        return_value=(503, b'{"error":"service unavailable"}', {}, TokenUsage())
    )

    mock_cb = AsyncMock()
    mock_cb.is_open = AsyncMock(return_value=False)
    mock_cb.record_failure = AsyncMock()
    mock_cb.record_success = AsyncMock()

    model_config = _make_model_config()

    result = await run_fallback_loop(
        try_order=["claude-sonnet", "claude-haiku"],
        original_alias="claude-sonnet",
        is_stream=False,  # Non-streaming path to exercise "all exhausted"
        req_data={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        redis=AsyncMock(),
        auth_context=None,
        state={},
        request_id="req-001",
        budget_status=None,
        adapter=mock_adapter,
        stream_kwargs={},
        nonstream_kwargs={},
        cb=mock_cb,
        router_service=MagicMock(),
        session_factory=MagicMock(),
        is_db_degraded=False,
        original_model_config=model_config,
        resolve_model_config=AsyncMock(return_value=_make_model_config("claude-haiku")),
        build_candidate_body=lambda req, cfg, s: (
            json.dumps(req).encode(),
            {},
            {},
        ),
        rewrite_model_id=lambda x, cfg=None: x,
    )

    # Must be a FallbackResult with status 503
    assert result.status == 503
    # payload[0] must be bytes (not an async generator/iterator)
    assert isinstance(result.payload[0], bytes)
    # The bytes should be valid JSON error
    error_body = json.loads(result.payload[0])
    assert "error" in error_body


@pytest.mark.asyncio
async def test_all_candidates_circuit_open_returns_503():
    """When all candidates are circuit-open, return 503 with all_open=True."""
    mock_cb = AsyncMock()
    mock_cb.is_open = AsyncMock(return_value=True)
    mock_cb.try_acquire_halfopen_probe = AsyncMock(return_value=False)

    model_config = _make_model_config()

    result = await run_fallback_loop(
        try_order=["claude-sonnet"],
        original_alias="claude-sonnet",
        is_stream=True,
        req_data={},
        redis=AsyncMock(),
        auth_context=None,
        state={},
        request_id="req-002",
        budget_status=None,
        adapter=AsyncMock(),
        stream_kwargs={},
        nonstream_kwargs={},
        cb=mock_cb,
        router_service=MagicMock(),
        session_factory=MagicMock(),
        is_db_degraded=False,
        original_model_config=model_config,
        resolve_model_config=AsyncMock(),
        build_candidate_body=lambda req, cfg, s: (json.dumps(req).encode(), {}, {}),
        rewrite_model_id=lambda x, cfg=None: x,
    )

    assert result.status == 503
    assert result.all_open is True
    assert isinstance(result.payload[0], bytes)
