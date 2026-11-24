# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""웹 검색 통제(검색 수 기준) 스키마 — budget.web_search_policies."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

#: 월 한도 입력 상한 — 오타(0 하나 더)로 사실상 무제한이 되는 것을 막는 정도의 값.
MAX_MONTHLY_QUERIES = 10_000_000


class WebSearchPolicyUpdate(BaseModel):
    """행 전체를 바꾼다. NULL = 상속(ORG 는 무제한/기본 상한). 월 한도 0 = 금지."""

    monthly_query_limit: int | None = Field(default=None, ge=0, le=MAX_MONTHLY_QUERIES)
    per_request_cap: int | None = Field(default=None, ge=1, le=50)


class WebSearchPolicyRow(BaseModel):
    monthly_query_limit: int | None = None
    per_request_cap: int | None = None
    updated_at: datetime | None = None


class WebSearchQuotaEffective(BaseModel):
    """사용자(또는 팀)의 유효 웹 검색 통제 + 이번 달 사용량(리포팅 TZ 월)."""

    period: str
    #: 개인 월 한도(None = 무제한). 팀 뷰에서는 None.
    monthly_limit: int | None = None
    #: "user" | "organization" | "none"
    monthly_limit_source: str = "none"
    used: int = 0
    remaining: int | None = None
    #: 팀 공용 풀(None = 무제한 또는 팀 없음).
    team_pool_limit: int | None = None
    team_used: int = 0
    team_remaining: int | None = None
    per_request_cap: int
    #: "user" | "team" | "organization" | "default"
    per_request_cap_source: str = "default"


class WebSearchQuotaTeamItem(BaseModel):
    team_id: str
    team_name: str
    dept_name: str | None = None
    monthly_query_limit: int | None = None
    per_request_cap: int | None = None
    used: int = 0


class WebSearchQuotaUserItem(BaseModel):
    user_id: str
    email: str
    display_name: str | None = None
    team_name: str | None = None
    monthly_query_limit: int | None = None
    per_request_cap: int | None = None
    used: int = 0


class WebSearchQuotaOverview(BaseModel):
    period: str
    default_per_request_cap: int
    org: WebSearchPolicyRow | None = None
    #: 이번 달 전체 검색 수.
    org_used: int = 0
    teams: list[WebSearchQuotaTeamItem] = []
    #: 개인 덮어쓰기 행이 있는 사용자만.
    users: list[WebSearchQuotaUserItem] = []
