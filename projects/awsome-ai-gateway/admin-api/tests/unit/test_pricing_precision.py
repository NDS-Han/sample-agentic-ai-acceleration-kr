"""단가 입력 정밀도 소수 8자리 — Haiku 5.5 의 0.0001375 를 받는다 (US-19, 2026-10-10).

DB 단가 열이 NUMERIC(12,8) 로 넓어졌으므로 API 검증과 가격표 동기화의 반올림도 8자리여야 한다.
6자리일 때 0.0001375 는 422 였다(정본 phase-2 0043 docstring 의 실측과 같음).
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.models import MAX_PRICE_PER_1K, ModelCreateRequest, PricingRequest
from app.services.model_service import ModelService

BASE = {
    "alias": "claude-haiku-5-5",
    "provider": "BEDROCK",
    "provider_model_id": "us.anthropic.claude-haiku-5-5",
    "api_format": "BEDROCK_NATIVE",
}
PRICE = {"input_price_per_1k_tokens": Decimal("0.00011"), "output_price_per_1k_tokens": Decimal("0.00055")}


def test_seven_and_eight_place_rates_are_accepted():
    m = ModelCreateRequest(
        **BASE,
        **PRICE,
        cache_creation_5m_price_per_1k_tokens=Decimal("0.0001375"),
        cache_read_price_per_1k_tokens=Decimal("0.00000001"),
    )
    assert m.cache_creation_5m_price_per_1k_tokens == Decimal("0.0001375")
    p = PricingRequest(
        **PRICE, cache_creation_5m_price_per_1k_tokens=Decimal("0.0006875"), effective_from="2026-10-10T00:00:00Z"
    )
    assert p.cache_creation_5m_price_per_1k_tokens == Decimal("0.0006875")


def test_nine_places_are_rejected():
    with pytest.raises(ValidationError):
        ModelCreateRequest(**BASE, **PRICE, cache_creation_5m_price_per_1k_tokens=Decimal("0.000000001"))


def test_upper_bound_follows_numeric_12_8():
    assert MAX_PRICE_PER_1K == Decimal("9999.99999999")
    with pytest.raises(ValidationError):
        ModelCreateRequest(**{**BASE, **PRICE, "input_price_per_1k_tokens": Decimal(10000)})


@pytest.mark.parametrize("method", ["preview_price_sync", "apply_price_sync"])
def test_price_sync_keeps_eight_places(method):
    default = inspect.signature(getattr(ModelService, method)).parameters["quantize"].default
    assert default == Decimal("0.00000001")
