# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Budget management integration tests — router + service + mocked DB."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

from app.models.auth import Team
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope


class TestSetTeamBudget:
    async def test_set_team_budget_returns_ok(self, client: AsyncClient, admin_headers: dict):
        team_id = str(uuid.uuid4())

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=MagicMock(spec=Team))
            MockBudgetRepo.return_value.upsert_config = AsyncMock()
            MockBudgetRepo.return_value.get_latest_config = AsyncMock(return_value=None)
            MockBudgetRepo.return_value.get_usage = AsyncMock(return_value=None)
            mock_audit.log = AsyncMock()

            resp = await client.put(
                f"/admin/budgets/team/{team_id}",
                json={"max_budget_usd": "1000.00", "policy": "HARD_BLOCK"},
                headers=admin_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAllocationMembership:
    """D-2/I-4: 배정 대상은 해당 팀의 현재 멤버여야 한다 — 비멤버는 403.

    예전 'ΣA > T → 400' 테스트는 D-1 에서 제거됐다 — CAP 모델은 초과 약정을
    허용하고 팀 총량 T 만 enforcement 한다.
    """

    async def test_non_member_allocation_returns_403(self, client: AsyncClient, admin_headers: dict):
        team_id = uuid.uuid4()

        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("100.00")
        team_config.policy = BudgetPolicy.HARD_BLOCK

        team_obj = MagicMock(spec=Team)
        team_obj.id = team_id
        team_obj.members = []  # 빈 팀 — 배정 대상은 전부 비멤버

        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=team_config)
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team_obj)

            resp = await client.put(
                f"/admin/budgets/team/{team_id}/allocate",
                json={
                    "allocations": [
                        {"user_id": str(uuid.uuid4()), "allocated_usd": "60.00"},
                        {"user_id": str(uuid.uuid4()), "allocated_usd": "60.00"},
                    ]
                },
                headers=admin_headers,
            )

        assert resp.status_code == 403
        assert "user_not_in_team" in resp.json()["error"]["message"]


class TestBudgetSummary:
    async def test_budget_summary_returns_items(
        self, client: AsyncClient, test_app, admin_headers: dict
    ):
        team_id = uuid.uuid4()

        config = MagicMock(spec=BudgetConfig)
        config.scope = BudgetScope.TEAM
        config.scope_id = team_id
        config.max_budget_usd = Decimal("500.00")
        config.default_user_cap_usd = None

        team_obj = MagicMock(spec=Team)
        team_obj.id = team_id
        team_obj.name = "TestTeam"
        team_obj.department = None

        # Mock DB session so _resolve_used's session.execute doesn't hit real Postgres
        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=execute_result)

        async def _mock_get_db():
            yield mock_session

        from app.core.db import get_db_session
        test_app.dependency_overrides[get_db_session] = _mock_get_db

        try:
            with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
                 patch("app.repositories.user_repository.UserRepository") as MockUserRepo:
                MockBudgetRepo.return_value.list_configs = AsyncMock(return_value=[config])
                MockUserRepo.return_value.iter_all_users = AsyncMock(return_value=[])
                MockUserRepo.return_value.list_all_teams = AsyncMock(return_value=[team_obj])

                resp = await client.get(
                    "/admin/budgets/summary?scope=team&period=2026-04",
                    headers=admin_headers,
                )
        finally:
            test_app.dependency_overrides.pop(get_db_session, None)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["summary"]) >= 1
