# Copyright 2026 © Amazon.com and Affiliates.
"""EffectivePolicy — 사용자에게 적용되는 정책 합성 뷰.

판정 코어는 순수 함수 compute_cells 로 분리돼 있다. 게이트웨이의
check_client_scope / check_client_model_scope 와 같은 의미를 따르는지가
검증 대상이다:

  * user_app  축: 빈 목록/None = 전체 허용 (fail-open)
  * model_app 축: None = 전체 허용, [] = 전면 거부 (fail-closed)
  * user_model 축: None = 전체 허용, 목록 = 화이트리스트
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.auth import User
from app.services.effective_policy_service import (
    EffectivePolicyService,
    compute_cells,
)

CLIENTS = ["claude-code", "codex", "cowork"]  # sorted(VALID_CLIENTS)


def _cell(cells, client, alias):
    return next(c for c in cells if c.client == client and c.model_alias == alias)


class TestComputeCells:
    MODELS = [("opus-5", None), ("sonnet-5", ["cowork"]), ("haiku-4.5", [])]

    def test_all_unrestricted_allows_everything(self):
        cells = compute_cells(None, None, [("opus-5", None), ("sonnet-5", None)])
        assert all(c.allowed for c in cells)
        assert len(cells) == len(CLIENTS) * 2

    def test_user_app_axis_blocks_unlisted_clients(self):
        cells = compute_cells(["cowork"], None, self.MODELS)
        assert _cell(cells, "cowork", "opus-5").allowed
        assert _cell(cells, "codex", "opus-5").blocked_by == ["user_app"]
        assert _cell(cells, "claude-code", "opus-5").blocked_by == ["user_app"]

    def test_model_app_axis_empty_list_denies_everywhere(self):
        # model_aliases.allowed_clients = [] → 명시적 전면 거부 (사용자 축과 반대 의미)
        cells = compute_cells(None, None, self.MODELS)
        for client in CLIENTS:
            assert _cell(cells, client, "haiku-4.5").blocked_by == ["model_app"]

    def test_model_app_axis_list_restricts_to_listed(self):
        cells = compute_cells(None, None, self.MODELS)
        assert _cell(cells, "cowork", "sonnet-5").allowed
        assert _cell(cells, "codex", "sonnet-5").blocked_by == ["model_app"]

    def test_user_model_axis_whitelists(self):
        cells = compute_cells(None, ["opus-5"], self.MODELS)
        assert _cell(cells, "cowork", "opus-5").allowed
        assert _cell(cells, "cowork", "sonnet-5").blocked_by == ["user_model"]

    def test_multiple_axes_can_block_same_cell(self):
        # cowork 미허용 사용자 × cowork 전용 모델 × 모델 목록 밖 모델
        cells = compute_cells(["codex"], ["opus-5"], self.MODELS)
        cell = _cell(cells, "cowork", "haiku-4.5")
        assert not cell.allowed
        assert set(cell.blocked_by) == {"user_app", "user_model", "model_app"}


def _exec_result(*, scalars=None, scalar=None, rows=None):
    """session.execute 반환값 모사 — scalars()/scalar_one_or_none()/all() 3종."""
    r = MagicMock()
    r.scalars.return_value = scalars if scalars is not None else []
    r.scalar_one_or_none.return_value = scalar
    r.all.return_value = rows if rows is not None else []
    return r


class TestGetForUser:
    """서비스 조립 — session.execute 호출 순서대로 canned 결과를 돌려준다.

    순서: User → Team → user_allowed_clients → user_allowed_models →
    (user 행 없을 때만) team_allowed_models → ModelAlias → BudgetConfig →
    BudgetUsage → RateLimitConfig → DowngradePolicy → RoutingProfile.
    """

    async def test_user_not_found_raises(self):
        session = MagicMock()
        session.execute = AsyncMock(return_value=_exec_result(scalar=None))
        svc = EffectivePolicyService(session)
        from app.core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await svc.get_for_user(uuid.uuid4())

    async def test_composes_all_axes(self):
        user = MagicMock(spec=User)
        user.id = uuid.uuid4()
        user.email = "dev@example.com"
        user.team_id = None

        session = MagicMock()
        session.execute = AsyncMock(
            side_effect=[
                _exec_result(scalar=user),                    # User
                _exec_result(scalars=["cowork"]),             # user_allowed_clients
                _exec_result(scalars=[]),                     # user_allowed_models
                _exec_result(rows=[("opus-5", None)]),        # ModelAlias
                _exec_result(scalars=[]),                     # BudgetConfig
                _exec_result(scalars=[]),                     # BudgetUsage
                _exec_result(scalars=[]),                     # RateLimitConfig
                _exec_result(scalars=[]),                     # DowngradePolicy
                _exec_result(scalars=[]),                     # RoutingProfile
            ]
        )
        res = await EffectivePolicyService(session).get_for_user(user.id)

        assert res.email == "dev@example.com"
        assert res.allowed_clients == ["cowork"]
        assert res.allowed_models_source == "none"
        assert _cell(res.cells, "cowork", "opus-5").allowed
        assert _cell(res.cells, "codex", "opus-5").blocked_by == ["user_app"]
