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
        latest.policy = BudgetPolicy.SOFT_WARNING

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

    async def test_team_budget_preserves_policy_and_thresholds_when_omitted(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """금액만 바꾼 저장이 policy/thresholds 를 기본값으로 리셋하면 안 된다.

        다이얼로그가 두 필드를 생략하면: policy 는 직전 config 행에서,
        thresholds 는 Redis(유일한 저장소)에서 이어받는다.
        """
        import json as _json

        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("2000.00"))
        assert "policy" not in data.model_fields_set
        assert "alert_thresholds" not in data.model_fields_set

        latest = MagicMock(spec=BudgetConfig)
        latest.default_user_cap_usd = None
        latest.policy = BudgetPolicy.SOFT_WARNING

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(
            return_value=_json.dumps({"thresholds": [50, 75]})
        )
        budget_service._cache_mgr._redis = fake_redis

        synced = {}

        async def _capture_sync(scope, sid, **kw):
            synced.update(kw)

        written = []

        async def _immediate(_s, write):
            await write()

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit, \
             patch.object(budget_service, "_sync_redis_thresholds", side_effect=_capture_sync), \
             patch("app.services.budget_service.defer_redis_write_until_commit", side_effect=_immediate):
            MockUserRepo.return_value.get_team = AsyncMock(return_value=_team_mock(team_id))
            repo = MockBudgetRepo.return_value
            repo.get_latest_config = AsyncMock(return_value=latest)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock(side_effect=lambda cfg: written.append(cfg))
            mock_audit.log = AsyncMock()

            await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

        assert written[0].policy == BudgetPolicy.SOFT_WARNING
        assert synced["policy"] == BudgetPolicy.SOFT_WARNING
        assert synced["alert_thresholds"] == [50, 75]

    async def test_omitted_policy_thresholds_fall_back_when_nothing_stored(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """이전 config 없고 Redis 미스 → 스키마 기본값(HARD_BLOCK/[80,90,100])."""
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("2000.00"))

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=None)
        budget_service._cache_mgr._redis = fake_redis

        written = []

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=_team_mock(team_id))
            repo = MockBudgetRepo.return_value
            repo.get_latest_config = AsyncMock(return_value=None)
            repo.get_usage = AsyncMock(return_value=None)
            repo.upsert_config = AsyncMock(side_effect=lambda cfg: written.append(cfg))
            mock_audit.log = AsyncMock()

            await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

        assert written[0].policy == BudgetPolicy.HARD_BLOCK


class TestGetBudgetConfig:
    async def test_returns_db_policy_and_redis_thresholds(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        import json as _json

        scope_id = uuid.uuid4()
        cfg = MagicMock(spec=BudgetConfig)
        cfg.is_active = True
        cfg.max_budget_usd = Decimal("500.00")
        cfg.policy = BudgetPolicy.THROTTLE
        cfg.default_user_cap_usd = Decimal("20.00")

        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(
            return_value=_json.dumps({"thresholds": [90, 60], "policy": "throttle"})
        )
        budget_service._cache_mgr._redis = fake_redis

        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_latest_config = AsyncMock(return_value=cfg)
            res = await budget_service.get_budget_config(
                mock_session, scope=BudgetScope.TEAM, scope_id=scope_id, actor=admin_user
            )

        assert res.configured is True
        assert res.policy == BudgetPolicy.THROTTLE
        assert res.alert_thresholds == [60, 90]  # 정렬돼 돌아온다
        assert res.default_user_cap_usd == Decimal("20.00")

    async def test_unconfigured_returns_flag(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_latest_config = AsyncMock(return_value=None)
            res = await budget_service.get_budget_config(
                mock_session, scope=BudgetScope.USER, scope_id=uuid.uuid4(), actor=admin_user
            )
        assert res.configured is False
        assert res.policy is None

    async def test_inactive_config_is_not_prefilled(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """해제된 예산의 최신 행(is_active=False)은 configured=False 로 보고."""
        cfg = MagicMock(spec=BudgetConfig)
        cfg.is_active = False
        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_latest_config = AsyncMock(return_value=cfg)
            res = await budget_service.get_budget_config(
                mock_session, scope=BudgetScope.USER, scope_id=uuid.uuid4(), actor=admin_user
            )
        assert res.configured is False

    async def test_team_leader_cannot_read_other_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """BR-BUD-03: 리더는 자기 팀의 설정만 읽는다 — 타 팀은 403."""
        with pytest.raises(ForbiddenError):
            await budget_service.get_budget_config(
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=uuid.uuid4(),  # actor.team_id 가 아닌 팀
                actor=team_leader_user,
            )

    async def test_team_leader_cannot_read_other_team_member(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """BR-BUD-03: 타 팀 멤버의 USER 설정도 403 (존재 여부와 무관하게 거부)."""
        other = MagicMock(spec=User)
        other.team_id = uuid.uuid4()
        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=other)
            with pytest.raises(ForbiddenError):
                await budget_service.get_budget_config(
                    mock_session,
                    scope=BudgetScope.USER,
                    scope_id=uuid.uuid4(),
                    actor=team_leader_user,
                )

    async def test_team_leader_reads_own_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_latest_config = AsyncMock(return_value=None)
            res = await budget_service.get_budget_config(
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=team_leader_user.team_id,
                actor=team_leader_user,
            )
        assert res.configured is False  # 통과 + 미설정


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
            repo.get_latest_config = AsyncMock(return_value=None)
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
            repo.get_latest_config = AsyncMock(return_value=None)
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
            repo.get_latest_config = AsyncMock(return_value=None)
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
            repo.get_latest_config = AsyncMock(return_value=None)
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
            repo.get_latest_config = AsyncMock(return_value=None)
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
            repo.get_latest_config = AsyncMock(return_value=None)
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
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
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
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=admin_user,
            )

        target_types = sorted({i.target_type for i in result.summary})
        assert target_types == ["team", "user"]
        team_row = next(i for i in result.summary if i.target_type == "team")
        user_row = next(i for i in result.summary if i.target_type == "user")
        assert team_row.limit_usd == Decimal("1000")
        assert user_row.limit_usd is None  # 미설정 user → limit 없음

    @pytest.mark.asyncio
    async def test_budget_summary_requires_actor(
        self, budget_service: BudgetService, mock_session: AsyncMock
    ):
        """actor 는 필수다 — 기본값 None 이면 actor 를 빠뜨린 호출이 TEAM_LEADER 필터 없이
        전사 예산을 돌려준다(fail-open). 빠뜨리면 호출 시점에 TypeError 로 실패해야 한다."""
        with pytest.raises(TypeError):
            await budget_service.get_budget_summary(mock_session, period="2026-04")

    @pytest.mark.asyncio
    async def test_budget_summary_team_leader_sees_only_own_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """TEAM_LEADER 는 소속 팀 행과 소속 팀 사용자 행만 받는다 — scope/target_id 를
        비워 전사 요약을 요청해도 타 팀 예산·사용액이 나오면 안 된다(IDOR)."""
        own_team_id = team_leader_user.team_id
        other_team_id = uuid.uuid4()

        def _team(tid: uuid.UUID, name: str) -> MagicMock:
            t = MagicMock()
            t.id = tid
            t.name = name
            t.department = None
            t.members = []
            return t

        def _user(team_id: uuid.UUID, name: str) -> MagicMock:
            u = MagicMock()
            u.id = uuid.uuid4()
            u.team_id = team_id
            u.display_name = name
            u.email = f"{name.lower()}@b"
            u.is_active = True
            return u

        own_user = _user(own_team_id, "Own")
        other_user = _user(other_team_id, "Other")

        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[own_user, other_user])
            URepo.return_value.list_all_teams = AsyncMock(
                return_value=[_team(own_team_id, "Mine"), _team(other_team_id, "Theirs")]
            )

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=team_leader_user,
            )

        team_ids = {i.target_id for i in result.summary if i.target_type == "team"}
        user_ids = {i.target_id for i in result.summary if i.target_type == "user"}
        assert team_ids == {str(own_team_id)}
        assert user_ids == {str(own_user.id)}

    @pytest.mark.asyncio
    async def test_budget_summary_cluster_uses_mget_nonatomic(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """prod 는 RedisCluster — 카운터 키의 해시태그 {sid} 가 대상마다 달라
        multi-key mget 은 CROSSSLOT 으로 실패하므로 클러스터 클라이언트에서는
        mget_nonatomic(fan-out)으로 읽어야 한다. standalone 경로는 mget 을 유지."""
        from redis.asyncio.cluster import RedisCluster

        user_id = uuid.uuid4()
        user_cfg = MagicMock(spec=BudgetConfig)
        user_cfg.scope = BudgetScope.USER
        user_cfg.scope_id = user_id
        user_cfg.max_budget_usd = Decimal("100")
        user_cfg.max_requests = None
        user_cfg.enabled = True

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Alice"
        user_obj.email = "a@b"

        cluster = MagicMock(spec=RedisCluster)
        cluster.mget = AsyncMock(side_effect=AssertionError("mget called on cluster"))
        cluster.mget_nonatomic = AsyncMock(return_value=[b"7.50"])

        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[user_cfg])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=admin_user, redis=cluster,
            )

        cluster.mget_nonatomic.assert_awaited_once()
        cluster.mget.assert_not_called()
        row = next(i for i in result.summary if i.target_id == str(user_id))
        assert row.used_usd == Decimal("7.50")

    @pytest.mark.asyncio
    async def test_budget_summary_standalone_still_uses_mget(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """standalone Redis(및 기존 테스트의 평범한 Mock)은 한 번의 mget 유지 —
        클러스터 분기가 회귀로 퍼지지 않는지 가드."""
        user_id = uuid.uuid4()
        user_cfg = MagicMock(spec=BudgetConfig)
        user_cfg.scope = BudgetScope.USER
        user_cfg.scope_id = user_id
        user_cfg.max_budget_usd = Decimal("100")
        user_cfg.max_requests = None
        user_cfg.enabled = True

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Bob"
        user_obj.email = "b@c"

        standalone = MagicMock()  # spec 없음 → isinstance(RedisCluster) False
        standalone.mget = AsyncMock(return_value=["2.25"])

        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[user_cfg])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=admin_user, redis=standalone,
            )

        standalone.mget.assert_awaited_once()
        row = next(i for i in result.summary if i.target_id == str(user_id))
        assert row.used_usd == Decimal("2.25")

    @pytest.mark.asyncio
    async def test_budget_summary_includes_team_downgrade_badge_fields(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """TEAM 행의 다운그레이드 배지 — 최신 배치 규칙 수 + 활성 여부.

        펼친 패널(AutoDowngradeConfig)은 최신 배치를 is_active 무관하게 보여
        주므로 배지도 같은 기준이어야 한다 — is_active 집계면 꺼진 규칙이
        펼침에서는 보이는데 배지는 안 떠 어긋난다.
        """
        team_id = uuid.uuid4()
        team_obj = MagicMock()
        team_obj.id = team_id
        team_obj.name = "Eng"
        team_obj.department = None
        team_obj.members = []

        def _rows(values):
            r = MagicMock()
            r.all = MagicMock(return_value=values)
            return r

        # session.execute 호출 순서: team 사용량 집계 → 다운그레이드 집계.
        # (표시 사용자가 없어 user 집계는 miss 비어 skip 된다.)
        mock_session.execute = AsyncMock(
            side_effect=[
                _rows([]),                       # team usage aggregate (miss)
                _rows([(team_id, 3, True)]),     # downgrade_rows (scope_id, cnt, enabled)
            ]
        )

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[team_obj])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=admin_user,
            )

        team_row = next(i for i in result.summary if i.target_type == "team")
        assert team_row.downgrade_rule_count == 3
        assert team_row.downgrade_enabled is True

    @pytest.mark.asyncio
    async def test_budget_summary_no_downgrade_leaves_fields_null(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        team_obj = MagicMock()
        team_obj.id = team_id
        team_obj.name = "Eng"
        team_obj.department = None
        team_obj.members = []

        def _rows(values):
            r = MagicMock()
            r.all = MagicMock(return_value=values)
            return r

        # team 사용량 집계 → 다운그레이드 집계 (user 집계는 표시 대상 없어 skip).
        mock_session.execute = AsyncMock(
            side_effect=[_rows([]), _rows([])]
        )

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[team_obj])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04",
                actor=admin_user,
            )

        team_row = next(i for i in result.summary if i.target_type == "team")
        assert team_row.downgrade_rule_count is None
        assert team_row.downgrade_enabled is None


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

    await budget_service._sync_redis_thresholds(
        "user",
        uuid.UUID("00000000-0000-4000-a000-000000000001"),
        max_budget_usd=Decimal("100"),
        policy=BudgetPolicy.HARD_BLOCK,
        alert_thresholds=[80, 90, 100],
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
    async def test_enabled_save_rejects_user_scope(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """USER scope 규칙 저장은 ValidationError — 게이트웨이가 TEAM 규칙만
        평가하므로 유저 규칙은 적용되지 않는 죽은 설정이다.
        끄기/삭제는 과거 행의 정리 경로로 허용한다(위 테스트 커버)."""
        from app.schemas.budgets import AutoDowngradeConfigRequest, DowngradeRuleItem

        data = AutoDowngradeConfigRequest(
            enabled=True,
            rules=[
                DowngradeRuleItem(
                    from_model_alias="anthropic.claude-opus",
                    to_model_alias="anthropic.claude-haiku",
                    threshold_pct=80,
                )
            ],
        )
        with pytest.raises(ValidationError):
            await budget_service.set_downgrade_config(
                mock_session,
                scope=BudgetScope.USER,
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
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=uuid.uuid4(),
                actor=admin_user,
            )
        assert res.enabled is False
        assert len(res.rules) == 1
        assert res.rules[0].from_model_alias == "anthropic.claude-opus"

    @pytest.mark.asyncio
    async def test_get_downgrade_config_team_leader_other_team_forbidden(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """R2-7: TEAM_LEADER 가 타 팀의 다운그레이드 정책을 열람할 수 없다(IDOR)."""
        with pytest.raises(ForbiddenError):
            await budget_service.get_downgrade_config(
                mock_session,
                scope=BudgetScope.TEAM,
                scope_id=uuid.uuid4(),  # 다른 팀
                actor=team_leader_user,
            )

    @pytest.mark.asyncio
    async def test_get_downgrade_config_team_leader_other_team_member_forbidden(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        """R2-7: USER 스코프도 같은 검사 — 타 팀 멤버 정책 열람 불가."""
        other_team_user = MagicMock(spec=User)
        other_team_user.team_id = uuid.uuid4()  # 리더의 팀이 아님
        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=other_team_user)
            with pytest.raises(ForbiddenError):
                await budget_service.get_downgrade_config(
                    mock_session,
                    scope=BudgetScope.USER,
                    scope_id=uuid.uuid4(),
                    actor=team_leader_user,
                )


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


class TestReviewFixes:
    """Opus 리뷰 반영 — 중복 배정 거부, overcommit D 항, no-op 캐시 갱신, 정밀도."""

    async def test_allocate_rejects_duplicate_user_ids(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """한 배치의 중복 user_id — order-dependent 결과이므로 전체 거부."""
        team_id = uuid.uuid4()
        uid = uuid.uuid4()
        member = _member_mock(uid, team_id)

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.max_budget_usd = Decimal("100")
        team_cfg.policy = BudgetPolicy.HARD_BLOCK

        req = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(uid), allocated_usd=Decimal("10")),
            AllocateBudgetItem(user_id=str(uid), allocated_usd=Decimal("20")),
        ])

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id, members=[member])
            )
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=team_cfg)

            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=req, actor=admin_user,
                    confirm=True,
                )
            assert excinfo.value.code == "duplicate_allocation"

    async def test_overcommit_ratio_includes_default_cap_members(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """§0: overcommit = (ΣA_u + D × N_미설정) / T — D 커버 멤버도 분자에 포함."""
        team_id = uuid.uuid4()
        uid_a, uid_b = uuid.uuid4(), uuid.uuid4()
        member_a = _member_mock(uid_a, team_id, display_name="A")
        member_b = _member_mock(uid_b, team_id, display_name="B")
        member_a.role = None
        member_b.role = None

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.max_budget_usd = Decimal("100")
        team_cfg.default_user_cap_usd = Decimal("20")  # D=20

        user_cfg_a = MagicMock(spec=BudgetConfig)
        user_cfg_a.max_budget_usd = Decimal("10")  # A: explicit 10, B: unset → D=20
        # committed = 10 + 20×1 = 30 → ratio 0.3

        async def _first_cfg(scope, sid):
            if scope == BudgetScope.TEAM:
                return team_cfg
            return user_cfg_a if sid == uid_a else None

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id, members=[member_a, member_b])
            )
            repo = MockBudgetRepo.return_value
            repo.get_first_active_config = AsyncMock(side_effect=_first_cfg)
            repo.get_usage = AsyncMock(return_value=None)

            result = await budget_service.get_team_allocation(
                mock_session, team_id=team_id, period="2026-04", actor=admin_user
            )

        assert result.overcommit_ratio == Decimal("0.3")

    async def test_default_cap_noop_still_invalidates_cache(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        """동일 D 재설정 — DB 변경 없어도 stale 캐시를 즉시 갱신한다."""
        team_id = uuid.uuid4()
        cfg = MagicMock(spec=BudgetConfig)
        cfg.default_user_cap_usd = Decimal("20")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_team = AsyncMock(
                return_value=_team_mock(team_id)
            )
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=cfg)

            await budget_service.set_team_default_cap(
                mock_session, team_id=team_id, value=Decimal("20"), actor=admin_user,
            )

        mock_redis.delete.assert_awaited()

    async def test_over_precision_rejected_by_service_not_schema(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """5자리 소수도 스키마를 통과 → 서비스가 invalid_amount_precision 으로 거부."""
        team_id = uuid.uuid4()
        uid = uuid.uuid4()
        user = _member_mock(uid, team_id)
        # 스키마가 통과시켜야 한다(decimal_places 제약 제거됨).
        req = SetBudgetRequest(max_budget_usd=Decimal("1.23456"))

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            with pytest.raises(BudgetRuleError) as excinfo:
                await budget_service.set_user_budget(
                    mock_session, user_id=uid, data=req, actor=admin_user,
                )
            assert excinfo.value.code == "invalid_amount_precision"

    async def test_warm_cache_skips_d_only_rows(
        self, budget_service: BudgetService, mock_session: AsyncMock
    ):
        """T=NULL 'D-only' 행은 limit_usd:null 캐시를 쓰지 않는다(키 부재=unset)."""
        cfg = MagicMock(spec=BudgetConfig)
        cfg.scope = BudgetScope.TEAM
        cfg.scope_id = uuid.uuid4()
        cfg.max_budget_usd = None
        cfg.policy = BudgetPolicy.HARD_BLOCK

        fake_redis = MagicMock()
        fake_redis.set = AsyncMock()
        budget_service._cache_mgr._redis = fake_redis

        with patch("app.services.budget_service.BudgetRepository") as BRepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[cfg])
            count = await budget_service.warm_team_budget_cache(mock_session)

        assert count == 0
        fake_redis.set.assert_not_called()
class TestGetBudgetSummaryUsage:
    """예산 미설정 대상의 사용액 집계($0.00 버그 수정)와 Redis MGET 경로."""

    async def test_no_config_target_gets_real_usage(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """BudgetConfig 없는 사용자도 usage_logs 실사용액이 표시된다 — 이전엔
        cfg 없음 → used=0 하드코딩으로 사용액이 있어도 $0.00 으로 나왔다."""
        user_id = uuid.uuid4()

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Alice"
        user_obj.email = "a@b"
        user_obj.is_active = True
        user_obj.team_id = None

        # usage GROUP BY 결과 — 사용자 행만, 팀 행은 없음
        usage_rows = MagicMock()
        usage_rows.all = MagicMock(return_value=[(user_id, "12.50")])

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[])
            mock_session.execute = AsyncMock(return_value=usage_rows)

            result = await budget_service.get_budget_summary(
                mock_session, scope="user", target_id=None, period="2026-04",
                actor=admin_user, redis=None,
            )

        row = next(i for i in result.summary if i.target_id == str(user_id))
        assert row.used_usd == Decimal("12.50")
        assert row.limit_usd is None  # 미설정 — 하지만 사용액은 실값

    async def test_redis_mget_single_call_beats_per_target_get(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """대상마다 개별 GET 이 아니라 MGET 한 번으로 enforcement 카운터를 읽는다."""
        user_id = uuid.uuid4()

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Alice"
        user_obj.email = "a@b"
        user_obj.is_active = True
        user_obj.team_id = None

        redis = MagicMock()
        redis.mget = AsyncMock(return_value=[b"7.25"])
        redis.get = AsyncMock()

        # Redis 가 전부 커버 → SQL 미스분 없음(execute 는 호출돼도 빈 집계 경로)
        empty_rows = MagicMock()
        empty_rows.all = MagicMock(return_value=[])

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[])
            URepo.return_value.iter_all_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[])
            mock_session.execute = AsyncMock(return_value=empty_rows)

            result = await budget_service.get_budget_summary(
                mock_session, scope="user", target_id=None, period="2026-04",
                actor=admin_user, redis=redis,
            )

        redis.mget.assert_awaited_once()
        redis.get.assert_not_called()
        row = next(i for i in result.summary if i.target_id == str(user_id))
        assert row.used_usd == Decimal("7.25")
