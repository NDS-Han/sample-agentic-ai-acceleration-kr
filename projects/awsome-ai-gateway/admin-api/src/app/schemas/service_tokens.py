# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ServiceTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 경계 없으면 OverflowError(500) 또는 사실상 영구 ADMIN 자격증명.
    # 상한 365d 는 기본 수명(90d)보다 긴 최초 발급을 허용하되 영구를 막는다 —
    # rotate 는 구 토큰 잔여 수명으로 상한되므로 연쇄 수명 연장은 불가능하다.
    expiry_days: int | None = Field(
        None, ge=1, le=365, description="Override default expiry (days)"
    )


class ServiceTokenCreateResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    token: str = Field(description="Plaintext token — returned only on issue/rotate")


class ServiceTokenItem(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    # rotate 계보 — 유출 토큰의 self-rotate 사슬을 관리자가 볼 수 있어야 한다.
    rotated_from: str | None = None


class ServiceTokenListResponse(BaseModel):
    items: list[ServiceTokenItem]
