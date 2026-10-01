# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


# ── Requests ──


class RateLimitSetRequest(BaseModel):
    rpm: int | None = Field(None, ge=1, description="Requests per minute")
    tpm: int | None = Field(None, ge=1, description="Tokens per minute")
    cpm: Decimal | None = Field(None, ge=0, description="Cost per minute (USD), USER/TEAM scope")
    cph: Decimal | None = Field(None, ge=0, description="Cost per hour (USD), USER/TEAM scope")


# ── Responses ──


class RateLimitResponse(BaseModel):
    scope: str
    scope_id: str | None = None
    model_alias: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    cpm_limit_usd: Decimal | None = None
    cph_limit_usd: Decimal | None = None
    is_active: bool


class RateLimitScopeStatus(BaseModel):
    """단건 조회 — /users 패널이 한 스코프의 own/inherited 상태만 필요로 한다.

    own = 이 스코프에 직접 설정된 활성 설정. inherited = 상위(USER→TEAM)에서
    물려받은 유효 설정. 둘 다 없으면 제한 없음. ``get_rate_limit_tree`` 의
    USER 분기와 동일한 상속 규칙을 쓴다(두 경로가 갈리면 배지가 어긋난다).
    """

    own: "RateLimitConfigItem | None" = None
    inherited: "RateLimitConfigItem | None" = None
    inherited_scope: str | None = None  # "TEAM" — USER 에만 존재


# ── Rate Limit Tree ──


class RateLimitConfigItem(BaseModel):
    target_id: str
    scope: str
    rpm: int | None = None
    tpm: int | None = None
    cpm: Decimal | None = None
    cph: Decimal | None = None


class RateLimitTreeNode(BaseModel):
    id: str
    label: str
    scope: str  # USER | TEAM | GLOBAL
    is_active: bool = True
    config: RateLimitConfigItem | None = None
    children: list["RateLimitTreeNode"] = []
    inherited_from: str | None = None


RateLimitTreeNode.model_rebuild()
