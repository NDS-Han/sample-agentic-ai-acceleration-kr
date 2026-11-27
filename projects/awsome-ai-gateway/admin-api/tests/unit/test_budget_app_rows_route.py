# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""GET /admin/budgets/user/{id}/apps — 저장된 per-app 행 직렬화 회귀."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.auth import CurrentUser, require_admin_or_team_leader
from app.core.db import get_db_session
from app.models.auth import UserRole
from app.routers import budgets


async def test_nonempty_app_rows_serialize_and_preserve_managed_metadata(monkeypatch):
    app = FastAPI()
    app.include_router(budgets.router)
    user_id = uuid.uuid4()
    svc = MagicMock()
    svc.get_user_app_budgets = AsyncMock(return_value=[{
        "client": "claude-code",
        "max_budget_usd": "12.3400",
        "period_type": "MONTHLY",
        "policy": "HARD_BLOCK",
        "alert_thresholds": [80, 90],
        "effective_from": "2026-10-01",
    }])
    svc.has_user_total_budget = AsyncMock(return_value=False)
    app.state.budget_service = svc

    actor = CurrentUser(
        user_id=uuid.uuid4(), email="a@t", role=UserRole.ADMIN, team_id=None,
    )
    app.dependency_overrides[require_admin_or_team_leader] = lambda: actor

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_db_session] = _db
    managed = SimpleNamespace(
        managed_by="aicm",
        last_written_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        facet="app-budget:claude-code",
    )
    rows = AsyncMock(return_value=[managed])
    monkeypatch.setattr("app.routers.budgets.managed_rows", rows)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/admin/budgets/user/{user_id}/apps")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_total_budget"] is False
    assert body["apps"] == [{
        "client": "claude-code",
        "max_budget_usd": "12.3400",
        "policy": "HARD_BLOCK",
        "managed_by": {
            "source": "aicm",
            "since": "2026-10-02T00:00:00Z",
            "field": "app-budget:claude-code",
        },
    }]
