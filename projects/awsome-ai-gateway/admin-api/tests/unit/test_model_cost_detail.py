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

import pytest

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.core.auth import CurrentUser
from app.services.analytics_service import AnalyticsService

TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def mock_session():
    """하이브리드 롤업 읽기용 기록 세션(tests/rollup_double) — WHERE·바인드 검증이 목적."""
    from tests.rollup_double import recording_session

    session, seen = recording_session()
    session.seen = seen
    return session


def _executed_sqls(mock_session) -> list[str]:
    return [q for q, _ in mock_session.seen]


def _data(mock_session):
    from tests.rollup_double import data_branches

    return [(q, p) for q, p in mock_session.seen if data_branches(q)]


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
    assert mock_session.seen == []


@pytest.mark.unit
async def test_custom_range_uses_date_bounds(mock_session, admin_user):
    """custom 구간은 월이 아니라 그 일자 구간 — overview 와 같은 하이브리드 범위."""
    from datetime import date

    svc = AnalyticsService()
    out = await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user,
        start_date="2026-09-01", end_date="2026-09-22",
    )
    assert out["period"] == "2026-09-01~2026-09-22"
    data = _data(mock_session)
    assert len(data) == 2  # 모델별 + 일별
    for sql, p in data:
        assert (p["r_start"], p["r_end"]) == (date(2026, 9, 1), date(2026, 9, 22))
        # raw 분기는 시간 버킷 단위 requested_at 범위(sargable) — to_char 없음
        assert "l.requested_at >= bk.s AND l.requested_at < bk.e" in sql
        assert "to_char" not in sql.lower()


@pytest.mark.unit
async def test_month_period_uses_period_bounds(mock_session, admin_user):
    from datetime import date

    svc = AnalyticsService()
    out = await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user
    )
    assert out["period"] == "2026-09"
    for _sql, p in _data(mock_session):
        assert (p["r_start"], p["r_end"]) == (date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.unit
async def test_team_scope_applies_team_id_filter(mock_session, admin_user):
    from tests.rollup_double import team_isolated

    svc = AnalyticsService()
    await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user, scope=f"team:{TEAM_A}"
    )
    data = _data(mock_session)
    assert data and all(team_isolated(q, p, [TEAM_A]) for q, p in data)


@pytest.mark.unit
async def test_team_leader_all_scope_is_forced_to_own_team(mock_session):
    """TEAM_LEADER + scope='all' → 자동으로 본인 팀 격리(overview 와 동일 불변식)."""
    from tests.rollup_double import team_isolated

    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=TEAM_A,
    )
    svc = AnalyticsService()
    await svc.get_model_cost_detail(mock_session, period="2026-09", actor=leader)
    data = _data(mock_session)
    assert data and all(team_isolated(q, p, [TEAM_A]) for q, p in data)


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
    # 인가 판정용 auth.teams 조회는 허용 — 데이터 테이블 접근만 차단.
    assert _executed_sqls(mock_session)
    for sql in _executed_sqls(mock_session):
        assert "auth.teams" in sql and "usage" not in sql


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
    for sql in _executed_sqls(mock_session):
        assert "auth.teams" in sql and "usage" not in sql


@pytest.mark.unit
async def test_client_filter_applied(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(
        mock_session, period="2026-09", actor=admin_user, client="claude-code"
    )
    data = _data(mock_session)
    assert data
    for sql, p in data:
        assert "a.client = :r_client" in sql and "COALESCE(l.client, 'other') = :r_client" in sql
        assert p["r_client"] == "claude-code"


@pytest.mark.unit
async def test_admin_all_scope_has_no_team_filter(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(mock_session, period="2026-09", actor=admin_user)
    for sql in _executed_sqls(mock_session):
        assert ":r_teams" not in sql
