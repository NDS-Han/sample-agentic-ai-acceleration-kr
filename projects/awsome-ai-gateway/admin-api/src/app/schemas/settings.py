# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class BodyLoggingSetting(BaseModel):
    """Current state of the global body-logging toggle."""

    enabled: bool
    # 인프라 배선(BODY_LOG_S3_BUCKET env) 존재 여부 — false 면 런타임 토글을
    # 켜도 수집 경로가 없어 PUT 이 거부되고 UI 가 스위치를 잠근다.
    provisioned: bool = True


class BodyLoggingUpdateRequest(BaseModel):
    enabled: bool


class WebSearchPricingSetting(BaseModel):
    """AgentCore Web Search 단가 — 모니터링 전용(예산·한도와 무관)."""

    #: USD per 1,000 queries. 문자열 직렬화 — 부동소수 왕복 오차 없이 표시·편집.
    price_per_1k_queries: Decimal
    default_price_per_1k_queries: Decimal
    #: 저장된 값이 없어 정가를 쓰는 중인가.
    is_default: bool
    updated_at: datetime | None = None
    #: 워커가 처음으로 비용을 기록한 시각 — 그 이전 사용량은 "미추적".
    tracking_since: datetime | None = None


class WebSearchPricingUpdateRequest(BaseModel):
    price_per_1k_queries: Decimal = Field(ge=0, le=1000, max_digits=8, decimal_places=4)

    @field_validator("price_per_1k_queries")
    @classmethod
    def _finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("price must be a finite number")
        return v
