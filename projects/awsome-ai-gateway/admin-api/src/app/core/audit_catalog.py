# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""감사 액션 카탈로그 — 뷰어 필터 그룹의 단일 출처.

audit_logger 가 기록하는 action 문자열을 조사 가능한 그룹으로 묶는다.
프론트는 ``GET /admin/audit/action-catalog`` 로 이 목록을 그대로 받아
필터 pill/select 를 만든다 — 액션-그룹 매핑이 프론트에 이중으로 박히면
새 액션 추가 시 UI 필터가 조용히 틀어진다.

## `_CONFIRMED` 정규화
budget_service._confirmed_action() 이 confirm=True 경로에서
``SET_TEAM_BUDGET`` → ``SET_TEAM_BUDGET_CONFIRMED`` 처럼 동적으로 접미사를
붙인다. 그룹 조회는 정규화된 이름으로 하고, SQL IN 은 원본+접미사 변형을
둘 다 넣는다.

## 매핑 안 된 액션
``other`` 그룹으로 보낸다. 새 액션이 코드에 추가돼도 필터에서 사라지지 않는다.
"""
from __future__ import annotations

_CONFIRMED_SUFFIX = "_CONFIRMED"

# 그룹 키 → 정규화된(_CONFIRMED 제거) 액션 이름 튜플.
ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "keys_tokens": (
        "CREATE_KEY",
        "REVOKE_KEY",
        "FORCE_REAUTH_TEAM",
        "SERVICE_TOKEN_ISSUE",
        "SERVICE_TOKEN_ROTATE",
        "SERVICE_TOKEN_END_GRACE",
        "SERVICE_TOKEN_REVOKE",
    ),
    "users_org": (
        "CREATE_DEPARTMENT",
        "CREATE_TEAM",
        "SET_TEAM_LEADER",
        "UNSET_TEAM_LEADER",
        "TRANSFER_USER",
        "MARK_SERVICE_ACCOUNT",
        "UNMARK_SERVICE_ACCOUNT",
        "COGNITO_SYNC_ALL",
        "COGNITO_SYNC_USER",
        "COGNITO_SYNC_GROUP",
    ),
    "access_policy": (
        "SET_TEAM_ALLOWED_MODELS",
        "CLEAR_TEAM_ALLOWED_MODELS",
        "SET_USER_ALLOWED_MODELS",
        "CLEAR_USER_ALLOWED_MODELS",
        "SET_TEAM_ALLOWED_CLIENTS",
        "CLEAR_TEAM_ALLOWED_CLIENTS",
        "SET_USER_ALLOWED_CLIENTS",
        "CLEAR_USER_ALLOWED_CLIENTS",
        "SET_ORG_ALLOWED_CLIENTS",
        "CLEAR_ORG_ALLOWED_CLIENTS",
    ),
    "models_routing": (
        "CREATE_MODEL",
        "UPDATE_MODEL",
        "DELETE_MODEL",
        "PATCH_MODEL_STATUS",
        "SET_PRICING",
        "TOGGLE_APP_MODEL",
        "SET_APP_DEFAULT_MODEL",
        "SET_CLIENT_ROUTING",
        "SET_WEB_SEARCH",
    ),
    "budgets_limits": (
        "SET_TEAM_BUDGET",
        "SET_TEAM_DEFAULT_CAP",
        "SET_USER_BUDGET",
        "SET_USER_CLIENT_BUDGET",
        "ALLOCATE_TEAM_BUDGET",
        "EQUAL_SPLIT_TEAM_BUDGET",
        "DELETE_TEAM_BUDGET",
        "DELETE_USER_BUDGET",
        "CLEAR_USER_CLIENT_BUDGET",
        "SEED_BUDGET_SPENT",
        "SET_AUTO_DOWNGRADE",
        "DELETE_AUTO_DOWNGRADE",
        "SET_RATE_LIMIT",
        "DELETE_RATE_LIMIT",
    ),
    "system": (
        "SET_BODY_LOGGING",
        "VIEW_INVOCATION_LOG_BODY",
    ),
}

OTHER_GROUP = "other"

_ACTION_TO_GROUP = {
    action: group for group, actions in ACTION_GROUPS.items() for action in actions
}


def normalize_action(action: str) -> str:
    """``X_CONFIRMED`` → ``X``. 접미사가 없으면 그대로."""
    if action.endswith(_CONFIRMED_SUFFIX):
        return action[: -len(_CONFIRMED_SUFFIX)]
    return action


def group_for_action(action: str) -> str:
    return _ACTION_TO_GROUP.get(normalize_action(action), OTHER_GROUP)


def sql_variants(group: str) -> list[str]:
    """그룹의 SQL IN 목록 — 원본 + ``_CONFIRMED`` 변형을 모두 포함."""
    actions = ACTION_GROUPS[group]
    return [*actions, *[a + _CONFIRMED_SUFFIX for a in actions]]


def all_known_variants() -> list[str]:
    """``other`` 그룹의 NOT IN 목록 — 카탈로그에 있는 전부."""
    return [v for group in ACTION_GROUPS for v in sql_variants(group)]
