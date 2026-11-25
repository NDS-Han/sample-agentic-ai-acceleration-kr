# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3 — finalize: 웹 검색 비용이 예산 정산액에 들어간다(DECISION 2), 단가 누락은 표시된다(B7),
CANCELLED 는 유효한 상태다(B1), cost:stream XADD 에 MAXLEN 이 없다(B4)."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import TokenUsage
from app.services import web_search_price as wsp
from app.services.cost_recorder import CostRecorder


def _payload(mock_redis) -> dict:
    args, kwargs = mock_redis.xadd.call_args
    assert "maxlen" not in kwargs  # B4
    return json.loads(args[1]["payload"])


@pytest.fixture(autouse=True)
def _price(monkeypatch):
    monkeypatch.setattr(wsp.PRICE, "_price", Decimal("7.00"))
    monkeypatch.setattr(wsp.PRICE, "_session_factory", None)
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"
    LuaScriptLoader._scripts.setdefault("budget_settle", "-- mock")


@pytest.mark.asyncio
async def test_web_search_cost_is_part_of_budget_charge(mock_redis, auth_context_vk,
                                                        model_config_bedrock):
    mock_redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    mock_redis.xadd = AsyncMock()
    usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500,
                       web_search_count=3)
    cost = await CostRecorder().finalize(
        redis=mock_redis, auth_context=auth_context_vk, model_config=model_config_bedrock,
        usage=usage, request_id="req-ws", is_stream=False, duration_ms=10)
    assert cost == Decimal("0.010500")  # 반환값·cost_usd 는 모델 토큰 비용 그대로
    # 예산 Lua(budget_deduct)는 토큰 + 검색 비용(3 × $7/1000 = $0.021)으로 정산한다.
    charges = {c.args[5] for c in mock_redis.eval.call_args_list if len(c.args) > 5}
    assert charges == {"0.031500"}, charges
    p = _payload(mock_redis)
    assert Decimal(p["cost_usd"]) == Decimal("0.010500")
    assert Decimal(p["web_search_cost_usd"]) == Decimal("0.021000")
    assert p["web_search_count"] == 3


@pytest.mark.asyncio
async def test_searches_with_zero_tokens_are_still_charged(mock_redis, auth_context_vk,
                                                           model_config_bedrock):
    """모델 호출이 토큰 없이 실패해도 실행된 검색은 청구된다 — 0 토큰 환불 분기로 가지 않는다."""
    mock_redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    mock_redis.xadd = AsyncMock()
    usage = TokenUsage(web_search_count=2)
    await CostRecorder().finalize(
        redis=mock_redis, auth_context=auth_context_vk, model_config=model_config_bedrock,
        usage=usage, request_id="req-ws0", is_stream=True, duration_ms=10, status="ERROR")
    p = _payload(mock_redis)
    assert p["status"] == "ERROR"
    assert Decimal(p["web_search_cost_usd"]) == Decimal("0.014000")
    charges = {c.args[5] for c in mock_redis.eval.call_args_list if len(c.args) > 5}
    assert charges == {"0.014000"}


@pytest.mark.asyncio
async def test_pricing_missing_is_flagged_not_silent(mock_redis, auth_context_vk,
                                                     model_config_bedrock):
    mock_redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    mock_redis.xadd = AsyncMock()
    cfg = model_config_bedrock.model_copy(update={
        "pricing": model_config_bedrock.pricing.model_copy(update={
            "input_per_1k": Decimal("0"), "output_per_1k": Decimal("0"),
            "pricing_missing": True})})

    class _M:
        def __init__(self):
            self.calls = []

        def add(self, n, attrs=None):
            self.calls.append((n, attrs))

    class _Metrics:
        token_usage_total = _M()
        cost_usd_total = _M()
        pricing_missing_total = _M()

    m = _Metrics()
    cost = await CostRecorder(metrics=m).finalize(
        redis=mock_redis, auth_context=auth_context_vk, model_config=cfg,
        usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        request_id="req-nop", is_stream=False, duration_ms=10)
    assert cost == Decimal("0")
    assert _payload(mock_redis)["pricing_missing"] is True
    assert m.pricing_missing_total.calls and m.pricing_missing_total.calls[0][0] == 1


@pytest.mark.asyncio
async def test_cancelled_status_passes_through(mock_redis, auth_context_vk, model_config_bedrock):
    mock_redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    mock_redis.xadd = AsyncMock()
    await CostRecorder().finalize(
        redis=mock_redis, auth_context=auth_context_vk, model_config=model_config_bedrock,
        usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, estimated=True),
        request_id="req-c", is_stream=True, duration_ms=10, status="CANCELLED")
    p = _payload(mock_redis)
    assert p["status"] == "CANCELLED" and p["estimated_usage"] is True


def test_web_search_cost_formula_matches_worker():
    assert wsp.web_search_cost(1000, Decimal("7.00")) == Decimal("7.000000")
    assert wsp.web_search_cost(1, Decimal("7.00")) == Decimal("0.007000")
    assert wsp.web_search_cost(0, Decimal("7.00")) == Decimal("0")
    assert wsp.parse_price('{"price_per_1k_queries": "5.5"}') == Decimal("5.5")
    assert wsp.parse_price({"price_per_1k_queries": "-1"}) is None
