# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import (
    BudgetRuleError,
    ConfirmationRequiredError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.models.auth import Team, User
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope
from app.schemas.budgets import AllocateBudgetItem, AllocateBudgetRequest, SetBudgetRequest
from app.services.budget_service import BUDGET_CONFIG_CACHE_TTL, BudgetService


def _team_mock(team_id, members=()):
    t = MagicMock(spec=Team)
    t.id = team_id
    t.name = "Dev"
    t.dept_id = uuid.uuid4()
    t.department = None
    t.members = list(members)
    return t


def _member_mock(user_id, team_id=None, *, display_name="M", email="m@x", active=True):
    m = MagicMock()
    m.id = user_id
    m.team_id = team_id
    m.display_name = display_name
    m.email = email
    m.is_active = active
    return m

# ── BUDGET_CONFIG_CACHE_TTL 값 확인 ──
assert BUDGET_CONFIG_CACHE_TTL == 300, "BUDGET_CONFIG_CACHE_TTL 은 300초 (5분) 여야 함"


@pytest.fixture
def budget_service(cache_mgr: CacheInvalidationManager) -> BudgetService:
    return BudgetService(cache_mgr=cache_mgr)


class TestSetTeamBudget:
    async def test_set_team_budget_success(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("1000.00"))

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id)
            )
            repo = MockBudgetRepo.return_value
            repo.get_latest_config = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

        MockBudgetRepo.return_value.upsert_config.assert_called_once()

    async def test_set_team_budget_not_found(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("1000.00"))

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

    async def test_team_budget_below_usage_requires_confirmation(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§3-1: team_used ≥ T_new → 409 confirmation_required. confirm=true 재시도는 통과."""
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("10.00"))

        usage = MagicMock()
        usage.used_usd = Decimal("50.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=_team_mock(team_id))
            repo = MockBudgetRepo.return_value
            repo.get_latest_config = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=usage)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            with pytest.raises(ConfirmationRequiredError) as excinfo:
                await budget_service.set_team_budget(
                    mock_session, team_id=team_id, data=data, actor=admin_user
                )
            assert excinfo.value.code == "confirmation_required"
            assert repo.upsert_config.call_count == 0

            # confirm=true → 저장되고 audit action 은 *_CONFIRMED
            await budget_service.set_team_budget(
                mock_session, team_id=team_id, data=data, actor=admin_user, confirm=True
            )
            assert repo.upsert_config.call_count == 1
            assert mock_audit.log.call_args.kwargs["action"] == "SET_TEAM_BUDGET_CONFIRMED"

    async def test_team_budget_preserves_default_cap_on_upsert(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§3-1: T 편집은 D 를 건드리지 않는다 — 필드 미전송 시 기존 D 이어받기."""
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("2000.00"))

        latest = MagicMock(spec=BudgetConfig)
        latest.default_user_cap_usd = Decimal("25.00")

        written = []

        async def _capture(cfg):
            written.append(cfg)

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=_team_mock(team_id))
            repo = MockBudgetRepo.return_value
            repo.get_latest_config = AsyncMock(return_value=latest)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock(side_effect=_capture)
            mock_audit.log = AsyncMock()

            await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

        assert written[0].default_user_cap_usd == Decimal("25.00")


class TestSetUserBudget:
    async def test_team_leader_cannot_set_other_team_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("100.00"))

        other_team_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = other_team_id  # Different team

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)

            with pytest.raises(ForbiddenError):
                await budget_service.set_user_budget(mock_session, user_id=user_id, data=data, actor=team_leader_user)

    async def test_user_without_team_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """I-4/§4-6: team_id 없는 유저 → 403 user_not_in_team."""
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("100.00"))

        user = MagicMock(spec=User)
        user.team_id = None

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_budget(
                    mock_session, user_id=user_id, data=data, actor=admin_user
                )
            assert excinfo.value.code == "user_not_in_team"
            assert excinfo.value.status_code == 403

    async def test_team_leader_cannot_set_own_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """D-13: 리더가 본인 예산을 설정하려 하면 403 self_allocation_forbidden."""
        user = MagicMock(spec=User)
        user.team_id = team_leader_user.team_id

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_budget(
                    mock_session,
                    user_id=team_leader_user.user_id,  # 본인
                    data=SetBudgetRequest(max_budget_usd=Decimal("50.00")),
                    actor=team_leader_user,
                )
            assert excinfo.value.code == "self_allocation_forbidden"
            assert excinfo.value.status_code == 403

    async def test_overcommit_allowed(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """D-1: ΣA_u ≤ T 검증은 사라졌다 — CAP 모델은 초과 약정을 허용한다.
        리더도 팀 예산을 넘는 개별 cap 을 설정할 수 있다(T 만 실질 상한)."""
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("600.00"))

        user = MagicMock(spec=User)
        user.team_id = team_leader_user.team_id

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.max_app_budget = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            # 팀 예산(T=1000)과 무관하게 저장된다 — 합계 조회 자체를 하지 않는다.
            await budget_service.set_user_budget(
                mock_session, user_id=user_id, data=data, actor=team_leader_user
            )

        repo.upsert_config.assert_called_once()
        repo.sum_member_budgets.assert_not_called()

    async def test_user_budget_below_app_cap_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """D-4/I-2: A_u_new < max(app_c) → 422 user_budget_below_app_cap."""
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("5.00"))

        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.max_app_budget = AsyncMock(return_value=Decimal("10.00"))

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_budget(
                    mock_session, user_id=user_id, data=data, actor=admin_user
                )
            assert excinfo.value.code == "user_budget_below_app_cap"
            repo.upsert_config.assert_not_called()

    async def test_user_budget_below_usage_requires_confirmation(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§4-2: A_u_new ≤ used_u → 409; confirm=true → *_CONFIRMED audit."""
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("10.00"))

        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        usage = MagicMock()
        usage.used_usd = Decimal("20.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.max_app_budget = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=usage)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            with pytest.raises(ConfirmationRequiredError):
                await budget_service.set_user_budget(
                    mock_session, user_id=user_id, data=data, actor=admin_user
                )
            repo.upsert_config.assert_not_called()

            await budget_service.set_user_budget(
                mock_session, user_id=user_id, data=data, actor=admin_user, confirm=True
            )
            assert repo.upsert_config.call_count == 1
            assert mock_audit.log.call_args.kwargs["action"] == "SET_USER_BUDGET_CONFIRMED"

    async def test_cent_precision_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """D-17: 센트 초과 정밀도(3자리+) → 422 invalid_amount_precision."""
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("10.005"))

        with pytest.raises(BudgetRuleError) as excinfo:
            await budget_service.set_user_budget(
                mock_session, user_id=user_id, data=data, actor=admin_user
            )
        assert excinfo.value.code == "invalid_amount_precision"

    async def test_user_budget_replaces_existing(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("200.00"))

        user = MagicMock(spec=User)
        user.team_id = team_id

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.max_app_budget = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            await budget_service.set_user_budget(mock_session, user_id=user_id, data=data, actor=admin_user)

        repo.upsert_config.assert_called_once()


class TestAllocateTeamBudget:
    async def test_team_leader_cannot_allocate_other_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        other_team_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(uuid.uuid4()), allocated_usd=Decimal("100.00")),
        ])

        with pytest.raises(ForbiddenError):
            await budget_service.allocate_team_budget(
                mock_session, team_id=other_team_id, data=data, actor=team_leader_user
            )

    async def test_team_leader_can_allocate_own_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        team_id = team_leader_user.team_id
        member_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(member_id), allocated_usd=Decimal("100.00")),
        ])

        team = _team_mock(team_id, members=[_member_mock(member_id, team_id)])
        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")
        team_config.policy = BudgetPolicy.HARD_BLOCK

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=team_config)
            repo.max_app_budget = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            await budget_service.allocate_team_budget(
                mock_session, team_id=team_id, data=data, actor=team_leader_user
            )

        repo.upsert_config.assert_called_once()

    async def test_allocate_rejects_non_member(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """D-2/I-4: 배정 대상이 팀 멤버가 아니면 403 user_not_in_team."""
        team_id = uuid.uuid4()
        outsider = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(outsider), allocated_usd=Decimal("100.00")),
        ])

        team = _team_mock(team_id, members=[])  # 멤버 없음
        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=team_config)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=data, actor=admin_user
                )
            assert excinfo.value.code == "user_not_in_team"
            assert excinfo.value.status_code == 403

    async def test_allocate_leader_self_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """D-13: 리더가 배정 목록에 본인을 넣으면 403 self_allocation_forbidden."""
        team_id = team_leader_user.team_id
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(
                user_id=str(team_leader_user.user_id), allocated_usd=Decimal("100.00")
            ),
        ])

        team = _team_mock(
            team_id, members=[_member_mock(team_leader_user.user_id, team_id)]
        )
        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=team_config)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=data, actor=team_leader_user
                )
            assert excinfo.value.code == "self_allocation_forbidden"

    async def test_get_team_allocation_forbidden_for_other_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """get_team_allocation 은 이전에 actor 검사가 없어 임의 team_id 로 타 팀
        배정을 읽을 수 있었다 — 소속 팀 외에는 403."""
        with pytest.raises(ForbiddenError):
            await budget_service.get_team_allocation(
                mock_session,
                team_id=uuid.uuid4(),
                period="2026-09",
                actor=team_leader_user,
            )

    async def test_get_team_allocation_allowed_for_own_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        team_id = team_leader_user.team_id
        team = MagicMock(spec=Team)
        team.id = team_id
        team.name = "Dev"
        team.dept_id = uuid.uuid4()
        team.department = None
        team.members = []

        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("500.00")
        team_config.default_user_cap_usd = None
        usage = MagicMock()
        usage.used_usd = Decimal("10.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            repo = MockBudgetRepo.return_value
            repo.get_first_active_config = AsyncMock(return_value=team_config)
            repo.get_usage = AsyncMock(return_value=usage)

            result = await budget_service.get_team_allocation(
                mock_session, team_id=team_id, period="2026-09", actor=team_leader_user
            )

        assert result is not None
        assert result.team_id == str(team_id)
        assert result.total_budget_usd == Decimal("500.00")

    async def test_allocation_overcommit_allowed(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """D-1: ΣA_u > T 도 허용된다 — CAP 모델은 초과 약정을 정보로만 둔다."""
        team_id = uuid.uuid4()
        m1, m2 = uuid.uuid4(), uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(m1), allocated_usd=Decimal("600.00")),
            AllocateBudgetItem(user_id=str(m2), allocated_usd=Decimal("600.00")),
        ])

        team = _team_mock(
            team_id,
            members=[_member_mock(m1, team_id), _member_mock(m2, team_id)],
        )
        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")
        team_config.policy = BudgetPolicy.HARD_BLOCK

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=team_config)
            repo.max_app_budget = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            await budget_service.allocate_team_budget(
                mock_session, team_id=team_id, data=data, actor=admin_user
            )

        assert repo.upsert_config.call_count == 2

    async def test_allocation_requires_team_budget_first(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        member_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(member_id), allocated_usd=Decimal("100.00")),
        ])

        team = _team_mock(team_id, members=[_member_mock(member_id, team_id)])

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

            with pytest.raises(ValidationError, match="Team budget must be set"):
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=data, actor=admin_user
                )


class TestGetBudgetSummary:
    @pytest.mark.asyncio
    async def test_budget_summary_returns_user_and_team_rows(
        self, budget_service: BudgetService, mock_session: AsyncMock
    ):
        team_id = uuid.uuid4()
        user_id = uuid.uuid4()

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.scope = BudgetScope.TEAM
        team_cfg.scope_id = team_id
        team_cfg.max_budget_usd = Decimal("1000")
        team_cfg.default_user_cap_usd = None

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Alice"
        user_obj.email = "a@b"
        team_obj = MagicMock()
        team_obj.id = team_id
        team_obj.name = "Eng"
        team_obj.department = None

        # _resolve_used falls through to session.execute when redis=None;
        # configure the awaited result so scalar_one() returns a Decimal-safe string
        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[team_cfg])
            # ⚠️ iter_all_users 다 — 예전엔 list_users(limit=500) 이었고, 그건
            #    created_at desc 로 정렬한 뒤 앞에서 잘라서 500번째 이후 사용자의 예산
            #    행이 조용히 빠졌다(오류 없이 틀린 사용률). 전수 조회로 바뀌었다.
            URepo.return_value.iter_all_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[team_obj])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04"
            )

        target_types = sorted({i.target_type for i in result.summary})
        assert target_types == ["team", "user"]
        team_row = next(i for i in result.summary if i.target_type == "team")
        user_row = next(i for i in result.summary if i.target_type == "user")
        assert team_row.limit_usd == Decimal("1000")
        assert user_row.limit_usd is None  # 미설정 user → limit 없음


@pytest.mark.asyncio
async def test_sync_redis_thresholds_sets_5min_ttl(budget_service):
    """USER 예산 설정 캐시는 5분 TTL 이어야 한다(Z 정책).

    ⚠️ USER 경로는 이제 `redis.set` 이 아니라 **Lua(`redis.eval`)** 로 쓴다 —
       app_clients 를 애플리케이션에서 GET-modify-SET 하면 로그인/동시 요청이 서로의
       필드를 지웠기 때문이다(core/budget_cache.py 참조). TTL 은 스크립트의 ARGV[2] 로
       넘어가므로 그 인자를 검사한다. 계약은 그대로다: 5분.
    """
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=1)
    budget_service._cache_mgr._redis = fake_redis

    data = SetBudgetRequest(
        max_budget_usd=Decimal("100"),
        policy=BudgetPolicy.HARD_BLOCK,
    )
    await budget_service._sync_redis_thresholds(
        "user", uuid.UUID("00000000-0000-4000-a000-000000000001"), data
    )

    fake_redis.eval.assert_awaited_once()
    args = fake_redis.eval.await_args.args
    # eval(script, numkeys, key, payload_json, ttl_str)
    assert args[1] == 1, "단일 키 스크립트여야 한다(클러스터 슬롯 안전)"
    assert str(BUDGET_CONFIG_CACHE_TTL) in args[4], (
        f"USER budget config cache 는 5분 TTL 이어야 함 (Z 정책). 받은 인자: {args[4]!r}"
    )
    # ⚠️ 이 경로에서 redis.set 을 쓰면 안 된다 — 그게 클로버의 형태다.
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_warm_team_budget_cache_writes_all_active_team_configs(
    budget_service: BudgetService, mock_session: AsyncMock
):
    """startup warmup: 활성 TEAM BudgetConfig 전체를 Redis에 캐싱."""
    team_id_1 = uuid.uuid4()
    team_id_2 = uuid.uuid4()

    cfg1 = MagicMock(spec=BudgetConfig)
    cfg1.scope = BudgetScope.TEAM
    cfg1.scope_id = team_id_1
    cfg1.max_budget_usd = Decimal("5000")
    cfg1.policy = BudgetPolicy.HARD_BLOCK

    cfg2 = MagicMock(spec=BudgetConfig)
    cfg2.scope = BudgetScope.TEAM
    cfg2.scope_id = team_id_2
    cfg2.max_budget_usd = Decimal("1000")
    cfg2.policy = BudgetPolicy.HARD_BLOCK

    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    budget_service._cache_mgr._redis = fake_redis

    with patch("app.services.budget_service.BudgetRepository") as BRepo:
        BRepo.return_value.list_configs = AsyncMock(return_value=[cfg1, cfg2])
        count = await budget_service.warm_team_budget_cache(mock_session)

    assert count == 2
    assert fake_redis.set.call_count == 2

    # Redis Cluster hash-tag braces 포함 여부 확인
    keys_called = [c.args[0] for c in fake_redis.set.call_args_list]
    assert f"budget:config:team:{{{team_id_1}}}" in keys_called
    assert f"budget:config:team:{{{team_id_2}}}" in keys_called

    # EX TTL = BUDGET_CONFIG_CACHE_TTL (300s) 확인
    for c in fake_redis.set.call_args_list:
        assert c.kwargs.get("ex") == BUDGET_CONFIG_CACHE_TTL, (
            f"warm_team_budget_cache 는 {BUDGET_CONFIG_CACHE_TTL}초 TTL 이어야 함"
        )


@pytest.mark.asyncio
async def test_warm_team_budget_cache_empty_returns_zero(
    budget_service: BudgetService, mock_session: AsyncMock
):
    """활성 TEAM BudgetConfig 없으면 0 반환, Redis SET 없음."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    budget_service._cache_mgr._redis = fake_redis

    with patch("app.services.budget_service.BudgetRepository") as BRepo:
        BRepo.return_value.list_configs = AsyncMock(return_value=[])
        count = await budget_service.warm_team_budget_cache(mock_session)

    assert count == 0
    fake_redis.set.assert_not_called()


class TestSetDowngradeConfigDisabled:
    """enabled=False(끄기 저장) 경로 — 규칙 존재 자체가 enabled 이므로
    끄기는 활성 규칙 전부 비활성화이며, 규칙·예산 검증을 거치지 않는다."""

    @pytest.mark.asyncio
    async def test_disabled_save_deactivates_rules(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        from app.schemas.budgets import AutoDowngradeConfigRequest

        data = AutoDowngradeConfigRequest(enabled=False, rules=[])
        with patch("app.services.budget_service.DowngradePolicyRepository") as Repo:
            Repo.return_value.deactivate_rules = AsyncMock(return_value=3)
            res = await budget_service.set_downgrade_config(
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=uuid.uuid4(),
                data=data,
                actor=admin_user,
            )
        Repo.return_value.deactivate_rules.assert_awaited_once()
        assert res.enabled is False
        assert res.rules == []

    @pytest.mark.asyncio
    async def test_disabled_save_works_without_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """예산 미설정 스코프에서도 끄기는 성공해야 한다 — 예산 검증을 건너뛴다."""
        from app.schemas.budgets import AutoDowngradeConfigRequest

        data = AutoDowngradeConfigRequest(enabled=False)
        with patch("app.services.budget_service.DowngradePolicyRepository") as Repo, \
             patch("app.services.budget_service.BudgetRepository") as BRepo:
            Repo.return_value.deactivate_rules = AsyncMock(return_value=0)
            res = await budget_service.set_downgrade_config(
                mock_session,
                scope=BudgetScope.USER,
                scope_id=uuid.uuid4(),
                data=data,
                actor=admin_user,
            )
            BRepo.assert_not_called()  # 끄기는 예산 존재 여부를 조회하지 않는다
        assert res.enabled is False

    @pytest.mark.asyncio
    async def test_enabled_save_with_empty_rules_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """enabled=True 인데 규칙이 없으면 ValidationError — 켜기는 규칙을 요구한다."""
        from app.schemas.budgets import AutoDowngradeConfigRequest

        data = AutoDowngradeConfigRequest(enabled=True, rules=[])
        with pytest.raises(ValidationError):
            await budget_service.set_downgrade_config(
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=uuid.uuid4(),
                data=data,
                actor=admin_user,
            )
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_disabled_config_preserves_rules(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """끄기 저장 후에도 최신 배치의 규칙이 GET 에 그대로 보인다 —
        비활성화는 삭제가 아니므로 리로드해도 규칙이 사라지지 않아야 한다."""
        from datetime import datetime, timezone

        rule = MagicMock(
            id=uuid.uuid4(),
            from_model_alias="anthropic.claude-opus",
            to_model_alias="anthropic.claude-haiku",
            threshold_pct=80,
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        with patch("app.services.budget_service.DowngradePolicyRepository") as Repo:
            Repo.return_value.get_current_rules = AsyncMock(return_value=[rule])
            res = await budget_service.get_downgrade_config(
                mock_session, scope=BudgetScope.TEAM, scope_id=uuid.uuid4()
            )
        assert res.enabled is False
        assert len(res.rules) == 1
        assert res.rules[0].from_model_alias == "anthropic.claude-opus"


class TestDeleteUserBudget:
    async def test_admin_delete_cascades_app_budgets(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """I-3: 개별 cap 삭제는 하위 app_c 를 연쇄 비활성화한다."""
        user_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        existing = MagicMock(spec=BudgetConfig)
        existing.id = uuid.uuid4()
        existing.max_budget_usd = Decimal("50.00")

        app_row = MagicMock(spec=BudgetConfig)
        app_row.client = "claude-code"

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.default_user_cap_usd = None  # D 없음 → 차단 경고 없음

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            # get_active_config: USER 기존 cap, TEAM 조회 순
            repo.get_active_config = AsyncMock(side_effect=[existing, team_cfg])
            repo.list_active_app_configs = AsyncMock(return_value=[app_row])
            repo.deactivate_app_configs_for_user = AsyncMock(return_value=["claude-code"])
            mock_audit.log = AsyncMock()

            # app cascade 는 차단이 아니라 완화 방향이라도, 삭제될 app 이 있으면
            # 확인이 필요하다 — confirm=true 로 호출.
            await budget_service.delete_user_budget(
                mock_session, user_id=user_id, actor=admin_user, confirm=True
            )

        assert existing.is_active is False
        repo.deactivate_app_configs_for_user.assert_awaited_once()
        assert mock_audit.log.call_args.kwargs["action"] == "DELETE_USER_BUDGET_CONFIRMED"

    async def test_delete_requires_confirmation_when_d_blocks(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§4-2: A_u 삭제 후 D 로 이행할 때 used_u ≥ D → 즉시 차단 → 409."""
        user_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        existing = MagicMock(spec=BudgetConfig)
        existing.max_budget_usd = Decimal("50.00")

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.default_user_cap_usd = Decimal("10.00")

        usage = MagicMock()
        usage.used_usd = Decimal("15.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(side_effect=[existing, team_cfg])
            repo.list_active_app_configs = AsyncMock(return_value=[])
            repo.get_usage = AsyncMock(return_value=usage)

            with pytest.raises(ConfirmationRequiredError):
                await budget_service.delete_user_budget(
                    mock_session, user_id=user_id, actor=admin_user
                )
            repo.deactivate_app_configs_for_user.assert_not_called()

    async def test_leader_cannot_delete_own_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        user = MagicMock(spec=User)
        user.team_id = team_leader_user.team_id

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.delete_user_budget(
                    mock_session, user_id=team_leader_user.user_id, actor=team_leader_user
                )
            assert excinfo.value.code == "self_allocation_forbidden"


class TestSetUserClientBudget:
    async def test_app_cap_exceeds_user_budget_rejected(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """I-2: app_c > A_u → 422 app_cap_exceeds_user_budget."""
        user_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        parent = MagicMock(spec=BudgetConfig)
        parent.max_budget_usd = Decimal("10.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.user_allowed_client_service.UserAllowedClientService") as MockUAC:
            MockUAC.return_value.get = AsyncMock(return_value=[])
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=parent)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_client_budget(
                    mock_session,
                    user_id=user_id,
                    client="claude-code",
                    data=SetBudgetRequest(max_budget_usd=Decimal("15.00")),
                    actor=admin_user,
                )
            assert excinfo.value.code == "app_cap_exceeds_user_budget"

    async def test_app_budget_requires_parent(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """I-3: 부모 USER 예산 없이 app cap → 422 app_budget_requires_user_budget."""
        user_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = uuid.uuid4()

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.user_allowed_client_service.UserAllowedClientService") as MockUAC:
            MockUAC.return_value.get = AsyncMock(return_value=[])
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_client_budget(
                    mock_session,
                    user_id=user_id,
                    client="cowork",
                    data=SetBudgetRequest(max_budget_usd=Decimal("5.00")),
                    actor=admin_user,
                )
            assert excinfo.value.code == "app_budget_requires_user_budget"


class TestSetTeamDefaultCap:
    async def test_creates_null_t_row_when_no_config(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """G-6: 팀 예산 미설정 상태에서도 D 를 쓸 수 있다 — max_budget_usd=NULL 행 생성."""
        team_id = uuid.uuid4()
        written = []

        async def _capture(cfg):
            written.append(cfg)

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id)
            )
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=None)
            repo.list_configured_member_ids = AsyncMock(return_value=set())
            repo.get_usages_for_scope_ids = AsyncMock(return_value={})
            repo.upsert_config = AsyncMock(side_effect=_capture)
            mock_audit.log = AsyncMock()

            result = await budget_service.set_team_default_cap(
                mock_session, team_id=team_id, value=Decimal("20.00"), actor=admin_user
            )

        assert result == Decimal("20.00")
        assert written[0].max_budget_usd is None
        assert written[0].default_user_cap_usd == Decimal("20.00")

    async def test_cap_below_member_usage_requires_confirmation(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§3-2: D 설정으로 미설정 멤버가 즉시 차단되면 409."""
        team_id = uuid.uuid4()
        member = _member_mock(uuid.uuid4(), team_id, display_name="Bob")
        team = _team_mock(team_id, members=[member])

        cfg = MagicMock(spec=BudgetConfig)
        cfg.id = uuid.uuid4()
        cfg.max_budget_usd = Decimal("100.00")
        cfg.default_user_cap_usd = None
        cfg.policy = BudgetPolicy.HARD_BLOCK

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=cfg)
            repo.list_configured_member_ids = AsyncMock(return_value=set())
            repo.get_usages_for_scope_ids = AsyncMock(
                return_value={member.id: Decimal("30.00")}
            )

            with pytest.raises(ConfirmationRequiredError) as excinfo:
                await budget_service.set_team_default_cap(
                    mock_session, team_id=team_id, value=Decimal("20.00"), actor=admin_user
                )
            warnings = excinfo.value.details["warnings"]
            assert warnings[0]["impact"] == "default_cap_blocks_members"
            assert warnings[0]["affected"][0]["user_id"] == str(member.id)


class TestDeleteTeamBudget:
    async def test_delete_always_requires_confirmation(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§3-1: 팀 예산 해제는 전원 차단 → 항상 confirm 필요."""
        team_id = uuid.uuid4()
        cfg = MagicMock(spec=BudgetConfig)
        cfg.id = uuid.uuid4()
        cfg.max_budget_usd = Decimal("100.00")

        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=cfg)

            with pytest.raises(ConfirmationRequiredError):
                await budget_service.delete_team_budget(
                    mock_session, team_id=team_id, actor=admin_user
                )
            assert cfg.is_active is not False


class TestEqualSplit:
    async def test_nothing_to_allocate_when_no_team_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        member = _member_mock(uuid.uuid4(), team_id)

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id, members=[member])
            )
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.equal_split_team_budget(
                    mock_session, team_id=team_id, clear_individual=False, actor=admin_user
                )
            assert excinfo.value.code == "equal_split_nothing_to_allocate"

    async def test_floor_cent_computation(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """D = floor_cent(T/N): $100/3 = $33.33 (버림)."""
        team_id = uuid.uuid4()
        members = [_member_mock(uuid.uuid4(), team_id) for _ in range(3)]
        team = _team_mock(team_id, members=members)

        cfg = MagicMock(spec=BudgetConfig)
        cfg.id = uuid.uuid4()
        cfg.max_budget_usd = Decimal("100.00")
        cfg.policy = BudgetPolicy.HARD_BLOCK
        cfg.default_user_cap_usd = None

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=team)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(return_value=cfg)
            repo.list_configured_member_ids = AsyncMock(return_value=set())
            repo.get_usages_for_scope_ids = AsyncMock(return_value={})
            mock_audit.log = AsyncMock()

            result = await budget_service.equal_split_team_budget(
                mock_session, team_id=team_id, clear_individual=False, actor=admin_user,
                confirm=True,
            )

        assert result["default_user_cap_usd"] == "33.33"
        assert cfg.default_user_cap_usd == Decimal("33.33")
