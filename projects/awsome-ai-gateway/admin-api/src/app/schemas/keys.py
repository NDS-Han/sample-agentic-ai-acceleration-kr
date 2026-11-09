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
    active_key_issued_at: datetime | None = None
    active_key_expires_at: datetime | None = None
    last_request_at: datetime | None = None
    # 활성 키(prefix 귀속)의 마지막 사용 — 유저 단위 last_request_at 과 달리
    # 재발급된 새 키 기준으로만 잡힌다(구 attribution 이전 행은 NULL 이라 미포함).
    active_key_last_request_at: datetime | None = None
    # 현재 리포팅 월 집계 (§59: 비용·토큰은 SUCCESS 행만)
    month_requests: int = 0
    month_tokens: int = 0
    month_cost_usd: float = 0.0


class ServiceAccountListResponse(BaseModel):
    items: list[ServiceAccountItem]
    max_ttl_days: int
    # features.service_keys=false 배포면 false — UI 가 발급 버튼을 숨긴다
    service_keys_enabled: bool = True


class ServiceAccountCreateRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=320)


class IssueServiceAccountKeyRequest(BaseModel):
    """표지+발급 결합 요청 — user_id 는 경로에, TTL 만 본문."""

    ttl_days: int | None = None


# ── 서비스 계정 키 단위 사용량 (migration 0051 key_prefix 귀속) ──


class ServiceAccountKeyUsageItem(BaseModel):
    """해당 계정에 발급된 키 한 개의 수명주기 + 귀속 사용량."""

    key_id: str
    key_prefix: str
    status: KeyStatus
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    requests: int = 0
    cost_usd: float = 0.0
    last_request_at: datetime | None = None


class ServiceAccountUsageSummary(BaseModel):
    requests: int = 0
    success: int = 0
    error: int = 0
    timeout: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    web_search_count: int = 0
    cost_usd: float = 0.0
    avg_latency_ms: float | None = None
    last_request_at: datetime | None = None
    # key_prefix=NULL 행(배포 공백/구 행) — 어떤 키에도 귀속되지 않은 이 계정의 사용.
    unattributed_requests: int = 0
    unattributed_cost_usd: float = 0.0


class ServiceAccountUsageByModel(BaseModel):
    model_alias: str
    requests: int
    tokens: int
    cost_usd: float
    cost_share: float


class ServiceAccountUsageDaily(BaseModel):
    date: str  # 리포팅 타임존 기준 YYYY-MM-DD
    requests: int
    tokens: int
    cost_usd: float


class ServiceAccountUsageResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    team_id: str | None = None
    team_name: str | None = None
    period: str  # YYYY-MM (리포팅 타임존)
    # 키 단위 귀속이 시작된 시점 — 이보다 이전 행은 key_prefix=NULL 이라 제외된다.
    attribution_since: datetime
    keys: list[ServiceAccountKeyUsageItem]
    summary: ServiceAccountUsageSummary
    by_model: list[ServiceAccountUsageByModel]
    daily: list[ServiceAccountUsageDaily]
