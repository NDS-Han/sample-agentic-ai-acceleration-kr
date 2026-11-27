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
    # AICM 통합 토큰 — scopes 를 주면 scoped integration 토큰(svc-)으로 발급된다.
    # legacy /aicm/v1 bootstrap 라우트가 하던 일을 사람 admin 표면으로 이전한 것
    # (legacy 제거). scope 없는 평범한 발급은 아래 필드가 모두 비어야 한다.
    scopes: list[str] | None = Field(
        None, min_length=1, max_length=16,
        description="AICM integration scopes (예: users:sync, budgets:write). 지정 시 integration 토큰",
    )
    allowed_cidrs: list[str] | None = Field(
        None, max_length=16, description="호출 허용 원천 IP 대역. 생략 = 제한 없음"
    )
    rate_limit_per_min: int | None = Field(
        None, ge=1, le=10000, description="write 등급 분당 상한 재정의"
    )


class ServiceTokenCreateResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    token: str = Field(description="Plaintext token — returned only on issue/rotate")
    integration: str | None = None
    scopes: list[str] | None = None
    allowed_cidrs: list[str] | None = None


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
