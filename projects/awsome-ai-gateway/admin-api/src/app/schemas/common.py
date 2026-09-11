# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ── Enums ──


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    TEAM_LEADER = "TEAM_LEADER"
    DEVELOPER = "DEVELOPER"


class KeyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class BudgetPolicy(str, enum.Enum):
    HARD_BLOCK = "HARD_BLOCK"
    SOFT_WARNING = "SOFT_WARNING"
    THROTTLE = "THROTTLE"


class ProviderEnum(str, enum.Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio)
    # GPT-5.6 on the standard bedrock-runtime plane (SigV4 + CRIS ids). Migration 0031.
    # Must stay in lockstep with models.model.Provider — this one gates the REQUEST body,
    # so omitting it makes the new plane un-creatable through the admin API even though
    # the DB and the gateway accept it.
    BEDROCK_RUNTIME_OPENAI = "BEDROCK_RUNTIME_OPENAI"


class ApiFormatEnum(str, enum.Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC_MESSAGES = "ANTHROPIC_MESSAGES"  # Mantle /anthropic/v1/messages
    OPENAI_RESPONSES = "OPENAI_RESPONSES"  # Mantle /openai/v1/responses (GPT-5.x)


class ScopeEnum(str, enum.Enum):
    USER = "USER"
    TEAM = "TEAM"
    GLOBAL = "GLOBAL"


# ── Pagination ──

# 커서 페이지네이션 limit 의 **단일 출처**.
# ⚠️ 아래 PaginationParams 는 오랫동안 아무도 임포트하지 않는 죽은 스키마였고, 그래서
#    /admin/keys·/admin/users 의 limit 은 `limit: int = 50` 로 아무 경계가 없었다.
#    ?limit=1000000 은 한 번의 요청으로 테이블 전체를 끌어오고, ?limit=0/-1 은
#    PostgreSQL 이 "LIMIT must not be negative" 로 거부해 정체 불명의 500 이 됐다.
#    라우터들은 이 상수를 Query(...) 에 직접 물려 쓴다 — 값이 갈라지지 않게.
PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200


class PaginationParams(BaseModel):
    cursor: str | None = Field(None, description="Last item ID for cursor-based pagination")
    limit: int = Field(
        PAGE_LIMIT_DEFAULT,
        ge=PAGE_LIMIT_MIN,
        le=PAGE_LIMIT_MAX,
        description="Number of items per page",
    )


class PaginationMeta(BaseModel):
    cursor: str | None = None
    limit: int
    has_more: bool


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    pagination: PaginationMeta


# ── Error ──


class ErrorDetail(BaseModel):
    type: str
    message: str
    code: str
    retry_after: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
