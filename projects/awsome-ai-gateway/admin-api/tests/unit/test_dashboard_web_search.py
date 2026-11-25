# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""대시보드 'WEB SEARCH · AGENTCORE' 섹션 API — 앱별·일별 검색 수, 일평균, 요청당 검색 수."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.routers.dashboard import dashboard_web_search

ADMIN = CurrentUser(user_id=uuid.uuid4(), email="a@x", role=UserRole.ADMIN, team_id=None)
REQ = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))


def _result(one=None, rows=()):
    r = MagicMock()
    r.one.return_value = one
    r.all.return_value = list(rows)
    return r


def _session(total, by_client, daily):
    """하이브리드 롤업 읽기(services/usage_reads) 질의를 SQL 모양으로 골라 답한다."""
    from tests.rollup_double import EmptyAggRow

    async def _execute(stmt, params=None):
        sql = str(stmt)
        if "AS total_requests" in sql:
            return _result(one=total)
        if "HAVING COALESCE(sum(wq), 0) > 0" in sql:
            return _result(rows=by_client)
        if "WHERE wq > 0 GROUP BY day" in sql:
            return _result(rows=daily)
        if "AS delayed" in sql:
            return _result(one=EmptyAggRow())
        return _result(rows=[])  # system_settings(추적 시작·단가)

    s = AsyncMock()
    s.execute = AsyncMock(side_effect=_execute)
    return s


async def _call(total, by_client=(), daily=(), *, today=date(2026, 11, 24), deployed=True,
                period="2026-11"):
    with patch("app.routers.dashboard.get_price",
               AsyncMock(return_value=(Decimal("7.00"), True, None))), \
         patch("app.routers.dashboard.reporting_today", return_value=today), \
         patch("app.services.web_search_cost.web_search_deployed", return_value=deployed):
        return await dashboard_web_search(REQ, period, None, ADMIN,
                                          _session(total, by_client, daily))


def _total(queries, cost, requests, untracked=0):
    return SimpleNamespace(ws_queries=queries, ws_cost=cost, ws_untracked=untracked,
                           total_requests=requests, error_requests=0, total_cost=0,
                           active_users=0, input_tokens=0, output_tokens=0,
                           cache_creation_tokens=0, cache_read_tokens=0)


@pytest.mark.asyncio
async def test_numbers_for_current_month():
    p = await _call(
        _total(96, Decimal("0.672"), 480),
        by_client=[SimpleNamespace(client="claude-code", queries=60, cost=Decimal("0.42")),
                   SimpleNamespace(client="cowork", queries=36, cost=None)],
        daily=[SimpleNamespace(day=date(2026, 11, 3), q=40),
               SimpleNamespace(day=date(2026, 11, 20), q=56)],
    )
    assert p["deployed"] is True
    assert p["price_per_1k_queries"] == "7.00"
    assert (p["queries"], p["cost_usd"], p["successful_requests"]) == (96, 0.672, 480)
    assert p["elapsed_days"] == 24
    assert p["avg_queries_per_day"] == 4.0          # 96 / 24
    assert p["queries_per_request"] == 0.2          # 96 / 480
    assert p["by_client"] == [
        {"client": "claude-code", "queries": 60, "cost_usd": 0.42},
        {"client": "cowork", "queries": 36, "cost_usd": None},   # 미추적은 None
    ]
    assert p["daily"] == [{"date": "2026-11-03", "queries": 40},
                          {"date": "2026-11-20", "queries": 56}]


@pytest.mark.asyncio
async def test_past_month_uses_days_in_month_and_two_decimals():
    p = await _call(_total(10, Decimal("0.07"), 3), period="2026-10")
    assert p["elapsed_days"] == 31
    assert p["avg_queries_per_day"] == 0.32         # 10 / 31
    assert p["queries_per_request"] == 3.33333333   # 10 / 3 — 표시 자릿수는 UI


@pytest.mark.asyncio
async def test_small_per_request_ratio_is_not_rounded_to_zero():
    # dev 실측: 3 검색 / 1,818 성공 요청 — 소수 2자리로 자르면 0.0 이 되어 UI 가 '0.00' 을 보였다.
    p = await _call(_total(3, None, 1818))
    assert p["queries_per_request"] == round(3 / 1818, 8) and p["queries_per_request"] > 0


@pytest.mark.asyncio
async def test_enabled_with_no_usage_is_zero_not_missing():
    p = await _call(_total(0, Decimal("0"), 0))
    assert (p["queries"], p["cost_usd"]) == (0, 0.0)
    assert p["avg_queries_per_day"] == 0.0
    assert p["queries_per_request"] is None         # 요청 0건 — 비율 정의 안 됨
    assert p["by_client"] == [] and p["daily"] == []


@pytest.mark.asyncio
async def test_future_month_has_no_daily_average():
    p = await _call(_total(0, None, 0), period="2026-12", deployed=False)
    assert p["deployed"] is False
    assert p["avg_queries_per_day"] is None
