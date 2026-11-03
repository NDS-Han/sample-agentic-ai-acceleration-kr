# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import KeyStatus, PaginatedResponse


# ── Responses ──


class KeyResponse(BaseModel):
    key_id: str
    key_prefix: str
    user_id: str
    user_email: str | None = None
    status: KeyStatus
    issued_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    created_at: datetime
    kind: str = "exchange"
    issued_by_email: str | None = None
    last_request_at: datetime | None = None


class KeyCreateResponse(BaseModel):
    key_id: str
    key_prefix: str
    user_id: str
    status: KeyStatus
    created_at: datetime
    expires_at: datetime
    virtual_key: str | None = Field(None, description="Plaintext VK returned only on creation")


class KeyListResponse(PaginatedResponse[KeyResponse]):
    pass


class KeyCountResponse(BaseModel):
    count: int


# ── Service-account keys ──


class IssueServiceKeyRequest(BaseModel):
    user_id: uuid.UUID
    ttl_days: int | None = Field(
        None,
        ge=1,
        description="미지정 시 APP_KEY_MAX_TTL_DAYS. 상한 초과는 422.",
    )


class ServiceAccountReadiness(BaseModel):
    """발급 전 준비 상태 — UI 가 Issue 버튼 활성화/경고 표시에 쓴다."""

    team_budget_ok: bool
    allowed_clients_clear: bool
    usable_models: int


class ServiceAccountItem(BaseModel):
    user_id: str
    email: str
    display_name: str
    team_id: str | None = None
    team_name: str | None = None
    is_active: bool
    readiness: ServiceAccountReadiness
    active_key_id: str | None = None
    active_key_prefix: str | None = None
    active_key_expires_at: datetime | None = None
    last_request_at: datetime | None = None


class ServiceAccountListResponse(BaseModel):
    items: list[ServiceAccountItem]
    max_ttl_days: int


class ServiceAccountCreateRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=320)
