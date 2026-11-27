# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""app-group ↔ per-app 예산 min() 합성 단위 테스트."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.models.budget import BudgetPolicy as OrmPolicy
from app.services import budget_degraded as bd
from app.services.budget_service import _compose_client_policy, _put_user_policy


def _row(amount, *, client=None, policy=OrmPolicy.HARD_BLOCK, thresholds=None):
    return SimpleNamespace(
        client=client,
        max_budget_usd=Decimal(str(amount)),
        policy=policy,
        alert_thresholds=[80] if thresholds is None else thresholds,
    )


def test_min_composition_both_directions_and_one_sided():
    app10 = _row(10, client="claude-code")
    group5 = _row(5)
    assert _compose_client_policy(app10, group5) is group5
    chosen = _compose_client_policy(_row(3, client="claude-code"), group5)
    assert chosen.max_budget_usd == Decimal("3")
    assert _compose_client_policy(app10, None) is app10
    assert _compose_client_policy(None, group5) is group5
    assert _compose_client_policy(None, None) is None


def test_zero_group_budget_is_effective_and_tie_prefers_app_policy():
    app10 = _row(10, client="claude-code", policy=OrmPolicy.SOFT_WARNING)
    group0 = _row(0)
    assert _compose_client_policy(app10, group0) is group0
    same_group = _row(10, policy=OrmPolicy.HARD_BLOCK)
    assert _compose_client_policy(app10, same_group) is app10


def test_user_policy_cache_contains_group_only_client_and_zero_cap():
    total = _row(30, client=None)
    group0 = _row(0)
    _put_user_policy("u1", [total], {"claude-code": group0})
    pc = bd.DEGRADED.policy
    user_cfg = pc.fresh(pc.user_key("u1"))
    client_cfg = pc.fresh(pc.client_key("u1", "claude-code"))
    cowork_cfg = pc.fresh(pc.client_key("u1", "cowork"))
    assert user_cfg["app_clients"] == ["claude-code"]
    assert client_cfg["limit_usd"] == "0"
    assert cowork_cfg is bd.ABSENT
