# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""모델별 상세 비용(/admin/analytics/models) 필터·격리 규칙.

배경 — 이 엔드포인트는 예전엔 period(YYYY-MM) 만 받는 별도 페이지용이었다.
/analytics 개요에 상세 섹션으로 통합되면서, 같은 화면의 카드(overview 집계)와
표(모델 상세)가 같은 필터를 받지 않으면 숫자가 어긋난다. 그래서 overview 와
동일하게 start_date/end_date·client·scope·TEAM_LEADER 격리를 받는다.

못박는 것:
  1. custom 구간 — 둘 다 있어야 적용, 한쪽만 오면 400(overview 와 동일 규칙).
  2. scope=team:{uuid} → usage_logs.team_id IN 필터가 쿼리에 실린다.
  3. TEAM_LEADER 는 본인 팀만 — scope='all' 이어도 자동 팀 격리, 타팀 지정은 403.
  4. client 필터가 쿼리에 실린다.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.core.auth import CurrentUser
from app.services.analytics_service import AnalyticsService

TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture(autouse=True)
def _empty_results(mock_session):
    """두 질의(model/daily) 모두 빈 결과 — WHERE 절 검증이 목적."""
    result = MagicMock()
    result.all.return_value = []
    mock_session.execute.return_value = result


def _executed_sqls(mock_session) -> list[str]:
    return [
        str(c.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))
        for c in mock_session.execute.call_args_list
    ]


@pytest.mark.unit
async def test_rejects_partial_custom_range(mock_session, admin_user):
    svc = AnalyticsService()
    with pytest.raises(ValidationError):
        await svc.get_model_cost_detail(
            mock_session, period="2026-09", actor=admin_user, start_date="2026-09-01"
        )
    with pytest.raises(ValidationError):
        await svc.get_model_cost_detail(
            mock_session, period="2026-09", actor=admin_user, end_date="2026-09-30"
        )
    mock_session.execute.assert_not_called()


@pytest.mark.unit
async def test_custom_range_uses_date_bounds(mock_session, admin_user):
    """custom 구간은 월 필터가 아니라 일자 경계(반개구간) — overview 와 동일."""
    svc = AnalyticsService()
    out = await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user,
        start_date="2026-09-01", end_date="2026-09-22",
    )
    assert out["period"] == "2026-09-01~2026-09-22"
    assert mock_session.execute.call_count == 2  # 모델별 + 일별
    for sql in _executed_sqls(mock_session):
        assert "requested_at >=" in sql and "requested_at <" in sql
        assert "to_char" not in sql.lower()  # sargable


@pytest.mark.unit
async def test_month_period_uses_period_bounds(mock_session, admin_user):
    svc = AnalyticsService()
    out = await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user
    )
    assert out["period"] == "2026-09"
    for sql in _executed_sqls(mock_session):
        assert "requested_at >=" in sql and "requested_at <" in sql


@pytest.mark.unit
async def test_team_scope_applies_team_id_filter(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user, scope=f"team:{TEAM_A}"
    )
    for sql in _executed_sqls(mock_session):
        assert "team_id" in sql and "IN" in sql


@pytest.mark.unit
async def test_team_leader_all_scope_is_forced_to_own_team(mock_session):
    """TEAM_LEADER + scope='all' → 자동으로 본인 팀 격리(overview 와 동일 불변식)."""
    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=TEAM_A,
    )
    svc = AnalyticsService()
    await svc.get_model_cost_detail(mock_session, period="2026-09", actor=leader)
    for sql in _executed_sqls(mock_session):
        assert "team_id" in sql and "IN" in sql


@pytest.mark.unit
async def test_team_leader_other_team_forbidden(mock_session):
    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=TEAM_A,
    )
    svc = AnalyticsService()
    with pytest.raises(ForbiddenError):
        await svc.get_model_cost_detail(
            mock_session, period="2026-09", actor=leader, scope=f"team:{TEAM_B}"
        )
    mock_session.execute.assert_not_called()


@pytest.mark.unit
async def test_team_leader_without_team_forbidden(mock_session):
    """team_id 가 없는 TEAM_LEADER 를 통과시키면 전사 데이터가 나간다 — 403."""
    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=None,
    )
    svc = AnalyticsService()
    with pytest.raises(ForbiddenError):
        await svc.get_model_cost_detail(mock_session, period="2026-09", actor=leader)
    mock_session.execute.assert_not_called()


@pytest.mark.unit
async def test_client_filter_applied(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user, client="claude-code"
    )
    for sql in _executed_sqls(mock_session):
        assert "client" in sql


@pytest.mark.unit
async def test_admin_all_scope_has_no_team_filter(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(mock_session, period="2026-09", actor=admin_user)
    for sql in _executed_sqls(mock_session):
        assert "team_id" not in sql
