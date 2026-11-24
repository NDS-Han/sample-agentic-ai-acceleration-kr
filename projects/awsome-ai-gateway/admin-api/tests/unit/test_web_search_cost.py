# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AgentCore 웹 검색 비용(모니터링 전용) — admin-api 읽기 헬퍼·단가 설정 API·CSV.

  - NULL(미추적) 과 0(추적·검색 없음) 을 구별한다 — 미추적을 $0 으로 접지 않는다
  - 단가 GET/PUT: 정가 기본값, 문자열 저장, 감사 로그 before/after, 범위 검증
  - CSV export: 모델 비용 표와 분리된 웹 검색 구획 + 무료분 안내
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.auth import CurrentUser
from app.core.audit_catalog import group_for_action
from app.models.auth import UserRole
from app.routers.settings import get_web_search_pricing, set_web_search_pricing
from app.schemas.analytics import AnalyticsResponse, CostSummary, ModelBreakdown
from app.schemas.settings import WebSearchPricingUpdateRequest
from app.services.analytics_service import AnalyticsService
from app.services.web_search_cost import (
    DEFAULT_PRICE_PER_1K,
    PRICING_KEY,
    TRACKING_SINCE_KEY,
    WebSearchUsage,
    parse_price,
    ws_columns,
    ws_from_row,
)

SINCE = "2026-11-24T03:00:00+00:00"


def _row(queries, cost, untracked, since=None):
    return SimpleNamespace(ws_queries=queries, ws_cost=cost, ws_untracked=untracked,
                           ws_since=since)


class TestWsFromRow:
    def test_no_tracked_rows_is_none_not_zero(self):
        ws = ws_from_row(_row(12, None, 12))
        assert ws.queries == 12
        assert ws.cost_usd is None  # 미추적 — "$0(무료)" 가 아니다
        assert ws.untracked_queries == 12
        assert ws.tracking_since is None

    def test_tracked_with_no_searches_is_zero(self):
        ws = ws_from_row(_row(0, Decimal("0"), 0, {"since": SINCE}))
        assert ws.cost_usd == 0.0
        assert ws.untracked_queries == 0
        assert ws.tracking_since == datetime(2026, 11, 24, 3, tzinfo=timezone.utc)

    def test_mixed_period_reports_partial_cost_and_untracked_count(self):
        ws = ws_from_row(_row(10, Decimal("0.028000"), 6, {"since": SINCE}))
        assert (ws.queries, ws.cost_usd, ws.untracked_queries) == (10, 0.028, 6)

    def test_empty_period(self):
        ws = ws_from_row(_row(0, None, 0))
        assert ws == WebSearchUsage()

    @pytest.mark.parametrize("since", [None, "garbage", {"since": 5}, {"x": SINCE}])
    def test_bad_tracking_marker_is_none(self, since):
        assert ws_from_row(_row(0, None, 0, since)).tracking_since is None

    def test_columns_have_no_status_filter_and_read_marker(self):
        sql = " ".join(str(c) for c in ws_columns())
        assert "status" not in sql  # 실패 요청의 검색도 과금 — SUCCESS 로 거르지 않는다
        assert "web_search_cost_usd IS NULL" in sql
        assert "system_settings" in sql


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"price_per_1k_queries": "7.00"}, Decimal("7.00")),
        ({"price_per_1k_queries": 0}, Decimal("0")),
        ({"price_per_1k_queries": "-1"}, None),
        ({"price_per_1k_queries": "1000.5"}, None),
        ({"price_per_1k_queries": "Infinity"}, None),
        ({"price_per_1k_queries": False}, None),
        ({}, None),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


# ── 단가 설정 API ───────────────────────────────────────────────────────────────
def _admin() -> CurrentUser:
    return CurrentUser(user_id=uuid.uuid4(), email="a@test", role=UserRole.ADMIN, team_id=None)


class _Repo:
    def __init__(self, rows: dict):
        self.rows = rows
        self.upserts: list = []

    async def get(self, key):
        return self.rows.get(key)

    async def upsert(self, key, value, updated_by=None):
        self.upserts.append((key, value, updated_by))
        self.rows[key] = SimpleNamespace(value=value, updated_at=datetime(2026, 11, 24))


def _patch_repo(repo: _Repo):
    factory = MagicMock(return_value=repo)
    return (
        patch("app.routers.settings.SystemSettingsRepository", factory),
        patch("app.services.web_search_cost.SystemSettingsRepository", factory),
    )


class TestPricingApi:
    async def test_get_defaults_to_list_price(self, mock_session):
        repo = _Repo({})
        p1, p2 = _patch_repo(repo)
        with p1, p2:
            resp = await get_web_search_pricing(_admin(), mock_session)
        assert resp.price_per_1k_queries == DEFAULT_PRICE_PER_1K == Decimal("7.00")
        assert resp.is_default is True
        assert resp.tracking_since is None

    async def test_get_returns_saved_price_and_tracking_since(self, mock_session):
        repo = _Repo({
            PRICING_KEY: SimpleNamespace(value={"price_per_1k_queries": "5.5"},
                                         updated_at=datetime(2026, 11, 20)),
            TRACKING_SINCE_KEY: SimpleNamespace(value={"since": SINCE}, updated_at=None),
        })
        p1, p2 = _patch_repo(repo)
        with p1, p2:
            resp = await get_web_search_pricing(_admin(), mock_session)
        assert resp.price_per_1k_queries == Decimal("5.5")
        assert resp.is_default is False
        assert resp.tracking_since == datetime(2026, 11, 24, 3, tzinfo=timezone.utc)

    async def test_put_stores_string_and_audits_before_after(self, mock_session):
        repo = _Repo({})
        admin = _admin()
        req = MagicMock()
        req.client.host = "10.0.0.1"
        req.state.request_id = "rid"
        p1, p2 = _patch_repo(repo)
        with p1, p2, patch("app.routers.settings.audit_logger") as al:
            al.log = AsyncMock()
            resp = await set_web_search_pricing(
                req, WebSearchPricingUpdateRequest(price_per_1k_queries=Decimal("6.25")),
                admin, mock_session,
            )
        assert repo.upserts == [(PRICING_KEY, {"price_per_1k_queries": "6.25"}, admin.user_id)]
        kw = al.log.await_args.kwargs
        assert kw["action"] == "SET_WEB_SEARCH_PRICING"
        assert kw["resource_id"] == PRICING_KEY
        assert kw["changes"] == {
            "before": {"price_per_1k_queries": "7.00", "is_default": True},
            "after": {"price_per_1k_queries": "6.25"},
        }
        mock_session.commit.assert_awaited()
        assert resp.price_per_1k_queries == Decimal("6.25")
        assert resp.is_default is False

    @pytest.mark.parametrize("bad", ["-0.01", "1000.01", "1.23456", "NaN"])
    def test_put_validation(self, bad):
        with pytest.raises(ValidationError):
            WebSearchPricingUpdateRequest(price_per_1k_queries=Decimal(bad))

    def test_audit_action_grouped_with_model_pricing(self):
        assert group_for_action("SET_WEB_SEARCH_PRICING") == group_for_action("SET_PRICING")


# ── CSV export ─────────────────────────────────────────────────────────────────
def _resp(ws: WebSearchUsage) -> AnalyticsResponse:
    return AnalyticsResponse(
        period="2026-11",
        cost_summary=CostSummary(total_requests=1, total_tokens=1,
                                 total_cost_usd=Decimal("1.5"), active_users=1,
                                 avg_cost_per_user_usd=Decimal("1.5")),
        by_model=[ModelBreakdown(model="m", cost_usd=Decimal("1.5"), requests=1)],
        web_search=ws,
    )


def test_csv_web_search_section_is_separate_from_model_cost():
    text = AnalyticsService._to_csv(
        _resp(WebSearchUsage(queries=4, cost_usd=0.028, untracked_queries=0, deployed=True,
                             tracking_since=datetime(2026, 11, 24, tzinfo=timezone.utc))),
        "model",
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["period", "model", "display_name", "requests", "cost_usd"]
    assert rows[1][-1] == "1.5"  # 모델 비용 행은 그대로
    assert rows[2] == []
    assert rows[3] == ["period", "web_search_queries", "web_search_cost_usd",
                       "web_search_untracked_queries"]
    assert rows[4] == ["2026-11", "4", "0.028000", "0"]
    # 추적 시작·무료분 문구는 내보내지 않는다.
    assert "tracking" not in text and "free" not in text


def test_csv_untracked_cost_is_blank_not_zero():
    rows = list(csv.reader(io.StringIO(AnalyticsService._to_csv(
        _resp(WebSearchUsage(queries=3, cost_usd=None, untracked_queries=3, deployed=True)), "team",
    ))))
    assert rows[-1][1:4] == ["3", "", "3"]


@pytest.mark.parametrize(
    ("deployed", "cost", "shown"),
    [(True, 0.0, True), (True, None, True), (False, 0.5, True), (False, 0.0, False),
     (False, None, False)],
)
def test_csv_section_follows_visibility_rule(deployed, cost, shown):
    text = AnalyticsService._to_csv(
        _resp(WebSearchUsage(queries=0, cost_usd=cost, deployed=deployed)), "model")
    assert ("web_search_queries" in text) is shown


# ── 표시 규칙·배포 플래그·경과일 ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("enabled", "url", "expected"),
    [(True, "https://gw", True), (True, "", False), (False, "https://gw", False)],
)
def test_deployed_flag_matches_features_web_search(enabled, url, expected):
    from app.services import web_search_cost as m

    with patch("app.core.config.get_settings",
               return_value=SimpleNamespace(WEB_SEARCH_ENABLED=enabled, AGENTCORE_GATEWAY_URL=url)):
        assert m.web_search_deployed() is expected
        assert ws_from_row(_row(0, None, 0)).deployed is expected


@pytest.mark.parametrize(
    ("deployed", "cost", "visible"),
    [(True, 0.0, True), (True, None, True), (False, 0.01, True), (False, 0.0, False),
     (False, None, False)],
)
def test_visibility_rule(deployed, cost, visible):
    from app.services.web_search_cost import web_search_visible

    assert web_search_visible(WebSearchUsage(cost_usd=cost, deployed=deployed)) is visible


def test_elapsed_days_reporting_calendar():
    from datetime import date

    from app.services.web_search_cost import elapsed_days

    today = date(2026, 11, 24)
    assert elapsed_days("2026-11", today) == 24   # 당월 — 오늘 포함
    assert elapsed_days("2026-10", today) == 31   # 지난 달 — 그 달 일수
    assert elapsed_days("2026-02", today) == 28
    assert elapsed_days("2026-12", today) == 0    # 미래 달
