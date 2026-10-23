# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R14 라우팅/폴백 리뷰 회귀 — read-timeout 폴백 금지·후보 에러 마스킹·예산·resolve 예외."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.base import ATTEMPT_UNSETTLED_HEADER
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.services.fallback_loop import run_fallback_loop


def _cfg(alias: str = "claude-sonnet") -> ModelConfigSchema:
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


def _cb(open_: bool = False) -> AsyncMock:
    cb = AsyncMock()
    cb.is_open = AsyncMock(return_value=open_)
    cb.try_acquire_halfopen_probe = AsyncMock(return_value=False)
    return cb


def _loop(adapter, *, resolver=None, original=None, try_order=None):
    return run_fallback_loop(
        try_order=try_order or ["claude-sonnet", "claude-haiku"],
        original_alias="claude-sonnet",
        is_stream=False,
        req_data={},
        redis=AsyncMock(),
        auth_context=None,
        state={},
        request_id="req-r14",
        budget_status=None,
        adapter=adapter,
        stream_kwargs={},
        nonstream_kwargs={},
        cb=_cb(),
        router_service=MagicMock(),
        session_factory=MagicMock(),
        is_db_degraded=False,
        original_model_config=original or _cfg("claude-sonnet"),
        resolve_model_config=resolver or AsyncMock(return_value=_cfg("claude-haiku")),
        build_candidate_body=lambda req, cfg, s: (json.dumps(req).encode(), {}, {}),
        rewrite_model_id=lambda x, cfg=None: x,
    )


@pytest.mark.asyncio
async def test_unsettled_read_timeout_aborts_fallback():
    """클라이언트측 read timeout(ATTEMPT_UNSETTLED)은 상류가 과금했을 수 있다 —
    다음 후보로 재시도하면 이중 청구. 마커가 있으면 루프는 즉시 그 오류를 돌려준다."""
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        side_effect=[
            (
                504,
                b'{"error":{"type":"provider_error","message":"Bedrock read timeout"}}',
                {ATTEMPT_UNSETTLED_HEADER: "1"},
                TokenUsage(),
            ),
            (200, b'{"ok":1}', {}, TokenUsage()),
        ]
    )
    result = await _loop(adapter)
    assert result.status == 504
    assert adapter.invoke.await_count == 1  # 두 번째 후보 호출 안 함


@pytest.mark.asyncio
async def test_server_side_timeout_still_falls_back():
    """마커 없는 504(서버가 명시 거부한 ModelTimeoutException)는 폴백 허용."""
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        side_effect=[
            (504, b'{"e":1}', {}, TokenUsage()),  # 마커 없음
            (200, b'{"ok":1}', {}, TokenUsage()),
        ]
    )
    result = await _loop(adapter)
    assert result.status == 200
    assert adapter.invoke.await_count == 2


@pytest.mark.asyncio
async def test_candidate_error_does_not_mask_original():
    """원본 503 뒤 후보가 비-폴백 오류(400)를 주면, 클라이언트에는 원본의 오류가
    가야 한다 — 요청은 원본 모델에 유효했으므로."""
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        side_effect=[
            (503, b'{"error":"overloaded"}', {}, TokenUsage()),      # 원본
            (400, b'{"error":"candidate rejected body"}', {}, TokenUsage()),  # 후보
        ]
    )
    result = await _loop(adapter)
    assert result.status == 503
    assert b"overloaded" in result.payload[0]


@pytest.mark.asyncio
async def test_candidate_resolve_exception_skips_slot_not_500():
    """후보 해석이 LookupError 가 아닌 예외(DB blip)로 죽어도 루프 밖으로 새지
    않고 슬롯을 건너뛴다 — 원본의 진짜 오류가 보존된다."""
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        return_value=(503, b'{"error":"overloaded"}', {}, TokenUsage())
    )
    resolver = AsyncMock(side_effect=RuntimeError("db down"))
    result = await _loop(adapter, resolver=resolver)
    assert result.status == 503  # 500 이 아니라 원본 오류


@pytest.mark.asyncio
async def test_skipped_candidate_errors_do_not_mask_all_unavailable():
    """원본이 회로개방으로 시도 못 하고 후보들이 전부 비-폴백 오류(400)면,
    후보의 400 이 아니라 503 service_unavailable 로 떨어져야 한다."""
    cb = AsyncMock()
    # 원본 회로는 열려 있고(프로브 패배), 후보 회로는 닫혀 있다.
    cb.is_open = AsyncMock(side_effect=[True, False])
    cb.try_acquire_halfopen_probe = AsyncMock(return_value=False)
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        return_value=(400, b'{"error":"bad pmid"}', {}, TokenUsage())
    )
    result = await run_fallback_loop(
        try_order=["claude-sonnet", "claude-haiku"],
        original_alias="claude-sonnet",
        is_stream=False,
        req_data={},
        redis=AsyncMock(),
        auth_context=None,
        state={},
        request_id="req-r14-mask",
        budget_status=None,
        adapter=adapter,
        stream_kwargs={},
        nonstream_kwargs={},
        cb=cb,
        router_service=MagicMock(),
        session_factory=MagicMock(),
        is_db_degraded=False,
        original_model_config=_cfg("claude-sonnet"),
        resolve_model_config=AsyncMock(return_value=_cfg("claude-haiku")),
        build_candidate_body=lambda req, cfg, s: (json.dumps(req).encode(), {}, {}),
        rewrite_model_id=lambda x, cfg=None: x,
    )
    assert result.status == 503
    assert b"temporarily unavailable" in result.payload[0]


@pytest.mark.asyncio
async def test_pool_timeout_is_503_not_unsettled():
    """PoolTimeout 은 연결을 못 얻은 것 — 요청이 안 나갔으니 폴백 안전(503)."""
    import httpx

    from app.providers.base import classify_transport_error

    status, hdrs, _ = classify_transport_error(httpx.PoolTimeout("full"))
    assert status == 503 and not hdrs
    status, hdrs, _ = classify_transport_error(httpx.WriteTimeout("w"))
    assert status == 503 and not hdrs
    status, hdrs, _ = classify_transport_error(httpx.ReadTimeout("r"))
    assert status == 504 and hdrs.get(ATTEMPT_UNSETTLED_HEADER) == "1"


@pytest.mark.asyncio
async def test_fallback_budget_stops_new_attempts():
    """총 예산을 넘으면 새 후보를 시도하지 않고 마지막 오류를 돌려준다."""
    adapter = AsyncMock()
    adapter.invoke = AsyncMock(
        return_value=(503, b'{"error":"overloaded"}', {}, TokenUsage())
    )
    calls = {"n": 0}

    def _mono() -> float:
        # 첫 호출(데드라인 계산)은 예산 안, 그 이후(후속 후보 평가)는 초과.
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 999.0

    with patch("app.services.fallback_loop.time.monotonic", side_effect=_mono):
        with patch(
            "app.services.fallback_loop.get_settings"
        ) as gs:
            gs.return_value.fallback_budget_sec = 10
            result = await _loop(adapter)
    assert result.status == 503
    assert adapter.invoke.await_count == 1
