# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json

import pytest

from app.middleware.budget import BudgetMiddleware


class FakeApp:
    async def __call__(self, scope, receive, send):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected_code,status_label", [
    ("team_budget_unset",    "team_budget_unset",    "팀 예산이 설정되지 않았습니다"),
    ("team_budget_exceeded", "team_budget_exceeded", "Budget limit exceeded"),
    ("user_budget_exceeded", "user_budget_exceeded", "Budget limit exceeded"),
    ("hard_block",           "hard_block",           "Monthly budget exhausted"),
    ("no_budget_assigned",   "no_budget_assigned",   "No budget assigned"),
    ("team_soft_limit_exceeded", "team_soft_limit_exceeded", "Budget limit exceeded"),
    ("user_soft_limit_exceeded", "user_soft_limit_exceeded", "Budget limit exceeded"),
])
async def test_send_429_budget_codes(reason, expected_code, status_label):
    sent = []
    async def send(msg): sent.append(msg)
    mw = BudgetMiddleware(FakeApp())
    await mw._send_429_budget(scope={"type": "http"}, send=send, reason=reason)
    start = sent[0]
    body = json.loads(sent[1]["body"])
    assert start["status"] == 429
    assert body["error"]["code"] == expected_code
    assert status_label in body["error"]["message"]


@pytest.mark.asyncio
async def test_backend_unavailable_returns_503_not_429(monkeypatch):
    """DB+Redis 양쪽 예산 체크 불가 → 인프라 장애는 503.

    'no_budget_assigned' 429 로 오보하면 운영자는 예산 미설정으로 오진하고
    클라이언트는 재시도 정책을 잘못 고른다 — 상태 코드 계약 회귀 방지.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.middleware import budget as budget_mod
    from app.services.budget_service import BudgetBackendUnavailableError

    monkeypatch.setattr(
        budget_mod._budget_service,
        "check_budget",
        AsyncMock(side_effect=BudgetBackendUnavailableError("db+redis down")),
    )

    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "path": "/v1/messages",
        "state": {
            "auth_context": SimpleNamespace(user_id="u1", team_id="t1"),
        },
    }
    await BudgetMiddleware(FakeApp())(scope, None, send)

    assert sent[0]["status"] == 503
    body = json.loads(sent[1]["body"])
    # Anthropic canonical: service_unavailable → overloaded_error (SDK 재시도 분기)
    assert body["error"]["type"] == "overloaded_error"


@pytest.mark.asyncio
async def test_policy_denial_still_429(monkeypatch):
    """일반 PermissionError(예산 거절)는 여전히 429 — 503 과 구분된다."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.middleware import budget as budget_mod

    monkeypatch.setattr(
        budget_mod._budget_service,
        "check_budget",
        AsyncMock(side_effect=PermissionError("team_budget_exceeded")),
    )

    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "path": "/v1/messages",
        "state": {
            "auth_context": SimpleNamespace(user_id="u1", team_id="t1"),
        },
    }
    await BudgetMiddleware(FakeApp())(scope, None, send)

    assert sent[0]["status"] == 429
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "team_budget_exceeded"
