# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""audit_catalog — 액션 그룹 매핑의 계약.

뷰어 필터가 의존하는 것들:
- ``_CONFIRMED`` 접미사는 같은 그룹에 속해야 한다(budget 동적 액션명).
- 매핑 안 된 액션은 ``other`` — 필터에서 사라지면 안 된다.
- SQL IN 목록은 원본+접미사 변형을 모두 포함해야 한다.
"""
from __future__ import annotations

from app.core.audit_catalog import (
    ACTION_GROUPS,
    OTHER_GROUP,
    all_known_variants,
    group_for_action,
    normalize_action,
    sql_variants,
)


def test_normalize_strips_confirmed_suffix() -> None:
    assert normalize_action("SET_TEAM_BUDGET_CONFIRMED") == "SET_TEAM_BUDGET"
    assert normalize_action("DELETE_USER_BUDGET_CONFIRMED") == "DELETE_USER_BUDGET"


def test_normalize_leaves_plain_actions() -> None:
    assert normalize_action("CREATE_KEY") == "CREATE_KEY"
    assert normalize_action("REVOKE_KEY") == "REVOKE_KEY"


def test_confirmed_variants_lands_in_same_group() -> None:
    assert group_for_action("SET_TEAM_BUDGET") == "budgets_limits"
    assert group_for_action("SET_TEAM_BUDGET_CONFIRMED") == "budgets_limits"
    assert group_for_action("DELETE_USER_BUDGET_CONFIRMED") == "budgets_limits"


def test_known_actions_map_to_expected_groups() -> None:
    assert group_for_action("CREATE_KEY") == "keys_tokens"
    assert group_for_action("MARK_SERVICE_ACCOUNT") == "users_org"
    assert group_for_action("SET_TEAM_ALLOWED_MODELS") == "access_policy"
    assert group_for_action("UPDATE_MODEL") == "models_routing"
    assert group_for_action("VIEW_INVOCATION_LOG_BODY") == "system"


def test_unmapped_action_falls_into_other() -> None:
    assert group_for_action("SOME_FUTURE_ACTION") == OTHER_GROUP


def test_sql_variants_includes_confirmed_forms() -> None:
    variants = sql_variants("keys_tokens")
    assert "CREATE_KEY" in variants
    assert "CREATE_KEY_CONFIRMED" in variants  # 변형이 있어도 매칭된다
    assert len(variants) == 2 * len(ACTION_GROUPS["keys_tokens"])


def test_all_known_variants_covers_every_group() -> None:
    known = set(all_known_variants())
    for actions in ACTION_GROUPS.values():
        for a in actions:
            assert a in known
            assert f"{a}_CONFIRMED" in known


def test_sql_variants_rejects_unknown_group() -> None:
    import pytest

    with pytest.raises(KeyError):
        sql_variants("no_such_group")
