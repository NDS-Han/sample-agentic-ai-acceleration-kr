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
