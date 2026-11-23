# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""custom 일자 구간 1년 상한(L1) — MAX_RANGE_DAYS(366, admin-ui period.ts 와 같은 값).

추이 차트용 from/to 를 받는 모든 엔드포인트(/admin/analytics, /models, /export)가
초과 구간을 422 + date_range_too_long 으로 거부한다. 형식 오류는 종전대로 400.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import usage_filters as uf
from app.core.config import get_settings
from app.core.exceptions import DateRangeTooLongError, ValidationError


@pytest.fixture
def reporting_tz(monkeypatch):
    def _set(tz: str) -> None:
        monkeypatch.setenv("REPORTING_TIMEZONE", tz)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


# ── L1: 구간 1년 상한 ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_max_range_days_is_one_year():
    assert uf.MAX_RANGE_DAYS == 366


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-01-01", "2026-12-31"),  # 365일
        ("2026-01-01", "2027-01-01"),  # 366일 (상한 그대로)
        ("2028-01-01", "2028-12-31"),  # 윤년 366일
        ("2026-11-01", "2026-11-01"),  # 하루
    ],
)
def test_range_within_one_year_is_accepted(reporting_tz, start, end):
    reporting_tz("America/New_York")
    uf.day_range_to_utc(start, end)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        ("2026-01-01", "2027-01-02", 367),
        ("2020-01-01", "2026-11-23", 2519),
    ],
)
def test_range_over_one_year_is_rejected(reporting_tz, start, end, days):
    reporting_tz("Asia/Kolkata")
    with pytest.raises(DateRangeTooLongError) as ei:
        uf.day_range_to_utc(start, end)
    assert f"{days} days" in ei.value.message and "366" in ei.value.message
    # 기존 `except ValidationError` 호출부도 그대로 잡는다
    assert isinstance(ei.value, ValidationError)
    assert ei.value.code == "date_range_too_long"


def _range_app() -> FastAPI:
    """analytics 라우터 + 프로덕션 예외 핸들러. 구간 검증은 DB 질의 전에 끝나므로
    세션은 더미로 충분하다."""
    from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
    from app.core.db import get_db_session
    from app.main import register_exception_handlers
    from app.models.auth import UserRole
    from app.routers import analytics
    from app.services.analytics_service import AnalyticsService

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(analytics.router)
    app.state.analytics_service = AnalyticsService()
    app.state.redis = None

    admin = CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="admin@example.com",
        role=UserRole.ADMIN,
        team_id=None,
    )

    async def _no_db():
        yield None

    app.dependency_overrides[require_admin_or_team_leader] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_db_session] = _no_db
    return app


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    ["/admin/analytics", "/admin/analytics/models", "/admin/analytics/export"],
)
async def test_trend_endpoints_reject_range_over_one_year_with_422(reporting_tz, path):
    reporting_tz("America/Los_Angeles")
    app = _range_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        res = await client.get(
            path,
            params={"period": "2026-01", "start_date": "2025-01-01", "end_date": "2026-01-02"},
        )
    assert res.status_code == 422, res.text
    err = res.json()["error"]
    assert err["code"] == "date_range_too_long"
    assert "367 days" in err["message"] and "366" in err["message"]


@pytest.mark.unit
async def test_malformed_range_stays_400(reporting_tz):
    """형식 오류는 종전대로 400 — 422 는 '해석은 되지만 너무 긴 구간' 전용."""
    reporting_tz("Asia/Kolkata")
    app = _range_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        res = await client.get(
            "/admin/analytics",
            params={"period": "2026-01", "start_date": "2026-1-1", "end_date": "2026-01-02"},
        )
    assert res.status_code == 400
