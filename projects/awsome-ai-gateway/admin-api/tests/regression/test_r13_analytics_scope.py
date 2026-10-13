# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R13 분석 경로 리뷰 회귀 — _resolve_scope 기본-거부 + 리드 팀 인가."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.models.usage import ROIScope
from app.services.analytics_service import _resolve_scope

TEAM_A = uuid.uuid4()
TEAM_B = uuid.uuid4()


def _admin():
    return CurrentUser(
        user_id=uuid.uuid4(), email="a@t.com", role=UserRole.ADMIN, team_id=None
    )


def _leader(team_id):
    return CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=team_id,
    )


def _session(led_teams=()):
    """auth.teams 조회가 led_teams 목록을 돌려주는 세션."""
    session = AsyncMock()
    result = MagicMock()
    result.all = MagicMock(return_value=[(t,) for t in led_teams])
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_unknown_scope_rejected_not_global():
    """scope=x 같은 임의 문자열이 GLOBAL 로 떨어지면 TEAM_LEADER 에게
    전사 분석이 새는 누출이었다 — 400 기본-거부."""
    with pytest.raises(ValidationError):
        await _resolve_scope(_leader(TEAM_A), "x", _session())
    with pytest.raises(ValidationError):
        await _resolve_scope(_admin(), "ALL", _session())


@pytest.mark.asyncio
async def test_bad_team_uuid_is_400_not_500():
    with pytest.raises(ValidationError):
        await _resolve_scope(_admin(), "team:not-a-uuid", _session())


@pytest.mark.asyncio
async def test_leader_scope_all_resolves_to_own_and_led_teams():
    """scope='all' 은 소속 팀 + leader_user_id 팀의 합집합으로 격리."""
    leader = _leader(TEAM_A)
    roi, ids = await _resolve_scope(leader, "all", _session(led_teams=[TEAM_B]))
    assert roi == ROIScope.TEAM
    assert set(ids) == {TEAM_A, TEAM_B}


@pytest.mark.asyncio
async def test_leader_can_open_led_team_not_just_own():
    """리드하는 다른 팀도 team:<uuid> 로 열 수 있다(M4)."""
    leader = _leader(TEAM_A)
    roi, ids = await _resolve_scope(
        leader, f"team:{TEAM_B}", _session(led_teams=[TEAM_B])
    )
    assert roi == ROIScope.TEAM and ids == [TEAM_B]


@pytest.mark.asyncio
async def test_leader_cannot_open_unrelated_team():
    with pytest.raises(ForbiddenError):
        await _resolve_scope(
            _leader(TEAM_A), f"team:{TEAM_B}", _session(led_teams=[])
        )


@pytest.mark.asyncio
async def test_admin_unrestricted():
    roi, ids = await _resolve_scope(_admin(), "all", _session())
    assert roi is None and ids is None
    roi, ids = await _resolve_scope(_admin(), f"team:{TEAM_B}", _session())
    assert roi == ROIScope.TEAM and ids == [TEAM_B]
