# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""app-group ↔ per-app 예산 다중 정책 합성 단위 테스트."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.models.budget import BudgetPolicy as OrmPolicy
from app.services import budget_degraded as bd
from app.services.budget_service import (
    _client_config_data,
    _compose_client_policy,
    _put_user_policy,
)


def _row(amount, *, client=None, policy=OrmPolicy.HARD_BLOCK, thresholds=None):
    return SimpleNamespace(
        client=client,
        max_budget_usd=Decimal(str(amount)),
        policy=policy,
        alert_thresholds=[80] if thresholds is None else thresholds,
    )


def _layers(composed):
    assert composed is not None and composed.layers is not None
    return {layer["source"]: layer for layer in composed.layers}


def test_mixed_rows_preserve_both_policies_and_one_sided_rows():
    app10 = _row(10, client="claude-code")
    group5 = _row(5, policy=OrmPolicy.THROTTLE)
    composed = _compose_client_policy(app10, group5)
    layers = _layers(composed)
    assert layers["app"]["limit_usd"] == "10"
    assert layers["app"]["policy"] == "hard_block"
    assert layers["group"]["limit_usd"] == "5"
    assert layers["group"]["policy"] == "throttle"
    # 최상위 필드는 layers 를 모르는 구버전 소비자용 보수적 차단 폴백이다.
    assert composed.max_budget_usd == Decimal("10")
    assert composed.policy.value == "hard_block"

    reverse = _compose_client_policy(
        _row(3, client="claude-code"), _row(5, policy=OrmPolicy.THROTTLE)
    )
    assert _layers(reverse)["app"]["limit_usd"] == "3"
    assert _compose_client_policy(app10, None) is app10
    assert _compose_client_policy(None, group5) is group5
    assert _compose_client_policy(None, None) is None


def test_zero_cap_and_soft_cap_survive_composition():
    app10 = _row(10, client="claude-code", policy=OrmPolicy.SOFT_WARNING)
    zero = _compose_client_policy(app10, _row(0, policy=OrmPolicy.THROTTLE))
    assert zero.max_budget_usd == Decimal("0")
    assert zero.policy.value == "hard_block"
    assert _layers(zero)["group"]["policy"] == "throttle"

    soft = _compose_client_policy(
        _row(10, client="claude-code"),
        _row(5, policy=OrmPolicy.SOFT_WARNING),
    )
    # soft 5 의 실제 차단 cap 은 5.5 — hard 10 보다 먼저다.
    assert soft.max_budget_usd == Decimal("5.5")
    assert soft.policy.value == "hard_block"


def test_throttle_only_composition_keeps_lowest_nominal_throttle_fallback():
    composed = _compose_client_policy(
        _row(10, client="claude-code", policy=OrmPolicy.THROTTLE, thresholds=[90]),
        _row(5, policy=OrmPolicy.THROTTLE, thresholds=[80]),
    )
    assert composed.max_budget_usd == Decimal("5")
    assert composed.policy.value == "throttle"
    assert composed.alert_thresholds == [80, 90]


def test_user_policy_cache_serializes_group_layers_and_zero_cap():
    total = _row(30, client=None)
    app10 = _row(10, client="claude-code")
    group5 = _row(5, policy=OrmPolicy.THROTTLE)
    _put_user_policy("u1", [total, app10], {"claude-code": group5})
    pc = bd.DEGRADED.policy
    user_cfg = pc.fresh(pc.user_key("u1"))
    client_cfg = pc.fresh(pc.client_key("u1", "claude-code"))
    cowork_cfg = pc.fresh(pc.client_key("u1", "cowork"))
    assert user_cfg["app_clients"] == ["claude-code"]
    assert {layer["source"] for layer in client_cfg["layers"]} == {"app", "group"}
    assert cowork_cfg is bd.ABSENT

    _put_user_policy("u2", [total], {"claude-code": _row(0)})
    zero_cfg = pc.fresh(pc.client_key("u2", "claude-code"))
    assert zero_cfg["limit_usd"] == "0"


def test_client_config_data_includes_policy_parameters_for_layers():
    composed = _compose_client_policy(
        _row(10, client="claude-code"),
        _row(5, policy=OrmPolicy.SOFT_WARNING),
    )
    data = _client_config_data(composed)
    assert data["soft_limit_pct"] == 110
    assert data["throttle_rpm_pct"] == 50
    assert all("soft_limit_pct" in layer for layer in data["layers"])
