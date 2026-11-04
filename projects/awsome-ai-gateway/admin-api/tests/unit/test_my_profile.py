# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GET /admin/my/profile — admin-ui 권한 게이트가 읽는 유효 역할 엔드포인트.

회귀 배경: IdP id_token 에는 `role` 클레임이 없어서 admin-ui 의 middleware/Sidebar 가
TEAM_LEADER/DEVELOPER 를 구별할 수 없었다(전부 undefined → 전 페이지 403). 이
엔드포인트가 get_current_user 의 판정(DB role + 그룹 병합)을 그대로 돌려주므로,
응답 계약(role 값, team_id null 허용)이 바뀌면 UI 게이트가 깨진다.
"""
import pytest

from app.routers.my import get_my_profile


@pytest.mark.asyncio
async def test_profile_returns_effective_role_and_team(team_leader_user):
    body = await get_my_profile(team_leader_user)
    assert body["role"] == "TEAM_LEADER"
    assert body["team_id"] == str(team_leader_user.team_id)
    assert body["email"] == "leader@test.com"
    assert body["user_id"] == str(team_leader_user.user_id)


@pytest.mark.asyncio
async def test_profile_developer(developer_user):
    body = await get_my_profile(developer_user)
    assert body["role"] == "DEVELOPER"


@pytest.mark.asyncio
async def test_profile_admin_without_team(admin_user):
    admin_user.team_id = None
    body = await get_my_profile(admin_user)
    assert body["role"] == "ADMIN"
    assert body["team_id"] is None


def _seq_session(*results):
    """session.execute 호출 순서대로 결과를 돌려주는 세션 목."""
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    return session


@pytest.mark.asyncio
async def test_my_budget_includes_team_pool(developer_user):
    """개인 예산이 없어도 team 블록이 내려간다 — 팀 풀이 별도로 차단하므로
    사용자에게 T/D/잔여가 보여야 한다(ko: 팀 예산이 적용됩니다… 의 후속)."""
    from decimal import Decimal
    from unittest.mock import MagicMock

    from app.routers.my import get_my_budget

    def _res(scalar_one_or_none=None, scalar_one=None):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
        r.scalar_one = MagicMock(return_value=scalar_one)
        return r

    team_cfg = MagicMock()
    team_cfg.max_budget_usd = Decimal("1000.00")
    team_cfg.default_user_cap_usd = Decimal("50.00")

    session = _seq_session(
        _res(scalar_one_or_none=None),      # 1) 개인 config 없음
        _res(scalar_one=0),                 # 2) 개인 used
        _res(scalar_one_or_none=team_cfg),  # 3) 팀 config
        _res(scalar_one="123.0"),           # 4) 팀 used
        _res(scalar_one_or_none="Dev"),     # 5) 팀 이름
    )
    body = await get_my_budget(None, developer_user, session)

    assert body["budget"]["limit_usd"] is None
    team = body["team"]
    assert team["team_name"] == "Dev"
    assert team["limit_usd"] == 1000.00
    assert team["used_usd"] == 123.0
    assert team["remaining_usd"] == 877.0
    assert team["usage_pct"] == 12.3
    assert team["default_cap_usd"] == 50.00


@pytest.mark.asyncio
async def test_my_budget_team_null_when_no_team(admin_user):
    """팀 미소속이면 team 은 null — 프론트가 팀 섹션을 숨긴다."""
    from unittest.mock import MagicMock

    from app.routers.my import get_my_budget

    def _res(scalar_one_or_none=None, scalar_one=None):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
        r.scalar_one = MagicMock(return_value=scalar_one)
        return r

    admin_user.team_id = None
    session = _seq_session(
        _res(scalar_one_or_none=None),  # 개인 config 없음
        _res(scalar_one=0),             # 개인 used
    )
    body = await get_my_budget(None, admin_user, session)
    assert body["team"] is None
