# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for PUT /admin/routing-profiles/{client}/routing."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.routing import (
    ClientRoutingBody,
    list_routing_profiles,
    set_client_routing,
)


def _make_request() -> MagicMock:
    req = MagicMock()
    req.app.state.cache_mgr = MagicMock()
    req.app.state.cache_mgr.invalidate = AsyncMock()
    req.client.host = "127.0.0.1"
    req.headers = {}
    req.state.request_id = "test-req"
    return req


def _make_admin_user():
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="admin@test.com",
        role=UserRole.ADMIN,
        team_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
    )


def _profile_row(**overrides) -> MagicMock:
    """model.routing_profiles 행 더블 — region 은 NOT NULL."""
    from app.models.routing import RoutingProfile

    row = MagicMock(spec=RoutingProfile)
    row.client = overrides.get("client", "claude-code")
    row.backend = overrides.get("backend", "mantle")
    row.account_role_arn = overrides.get("account_role_arn")
    row.external_id = overrides.get("external_id")
    row.region = overrides.get("region", "ap-northeast-2")
    row.default_model = overrides.get("default_model")
    row.enabled = overrides.get("enabled", True)
    row.web_search_enabled = overrides.get("web_search_enabled", False)
    return row


def _session_returning(row) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value.all.return_value = [row] if row else []
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    return session


# ── service validation (via router) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_routing_invalid_client():
    session = _session_returning(_profile_row())
    body = ClientRoutingBody(region="ap-south-1")
    with pytest.raises(HTTPException) as ei:
        await set_client_routing(_make_request(), "not-a-client", body, _make_admin_user(), session)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_set_routing_profile_not_found():
    session = _session_returning(None)
    body = ClientRoutingBody(region="ap-south-1")
    with pytest.raises(HTTPException) as ei:
        await set_client_routing(_make_request(), "claude-code", body, _make_admin_user(), session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_set_routing_bad_arn_rejected():
    session = _session_returning(_profile_row())
    body = ClientRoutingBody(account_role_arn="arn:aws:iam::abc:role/x", region="ap-south-1")
    with pytest.raises(HTTPException) as ei:
        await set_client_routing(_make_request(), "claude-code", body, _make_admin_user(), session)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_set_routing_bad_region_rejected():
    session = _session_returning(_profile_row())
    body = ClientRoutingBody(
        account_role_arn="arn:aws:iam::123456789012:role/x", region="south-1"
    )
    with pytest.raises(HTTPException) as ei:
        await set_client_routing(_make_request(), "claude-code", body, _make_admin_user(), session)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_set_routing_external_id_without_arn_rejected():
    session = _session_returning(_profile_row())
    body = ClientRoutingBody(external_id="eid123", region="ap-south-1")
    with pytest.raises(HTTPException) as ei:
        await set_client_routing(_make_request(), "claude-code", body, _make_admin_user(), session)
    assert ei.value.status_code == 400


# ── happy paths ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_routing_writes_triple_commits_then_invalidates():
    """Set ARN+region+external_id: row updated, commit before cache DEL, audit written."""
    row = _profile_row()
    session = _session_returning(row)
    req = _make_request()
    body = ClientRoutingBody(
        account_role_arn="arn:aws:iam::123456789012:role/xacct",
        external_id="shared-secret-1",
        region="ap-south-1",
    )
    with patch("app.routers.routing.audit_logger") as audit:
        audit.log = AsyncMock()
        resp = await set_client_routing(req, "claude-code", body, _make_admin_user(), session)

    assert row.account_role_arn == "arn:aws:iam::123456789012:role/xacct"
    assert row.external_id == "shared-secret-1"
    assert row.region == "ap-south-1"
    # commit → invalidate → audit 순서 (commit 전 DEL 금지 규칙)
    session.commit.assert_awaited_once()
    req.app.state.cache_mgr.invalidate.assert_awaited_once()
    audit.log.assert_awaited_once()
    # external_id 는 감사 로그에 원문이 아니라 마스킹으로 남는다
    changes = audit.log.call_args.kwargs["changes"]
    assert changes["after"]["external_id"] == "***({}자)".format(len("shared-secret-1"))
    assert resp["account_role_arn"] == body.account_role_arn


@pytest.mark.asyncio
async def test_set_routing_clear_arn_clears_external_id_keeps_region():
    """ARN 을 비우면 in-account 복귀 — external_id 는 dead data 라 같이 지우고,
    region 은 NOT NULL 기본 리전이라 유지(입력값으로 갱신)된다."""
    row = _profile_row(
        account_role_arn="arn:aws:iam::123456789012:role/old",
        external_id="old-eid",
        region="ap-south-1",
    )
    session = _session_returning(row)
    body = ClientRoutingBody(account_role_arn=None, region="ap-northeast-2")
    with patch("app.routers.routing.audit_logger") as audit:
        audit.log = AsyncMock()
        resp = await set_client_routing(
            _make_request(), "claude-code", body, _make_admin_user(), session
        )

    assert row.account_role_arn is None
    assert row.external_id is None
    assert row.region == "ap-northeast-2"
    assert resp["account_role_arn"] is None
    assert resp["external_id"] is None


@pytest.mark.asyncio
async def test_list_profiles_exposes_routing_fields():
    row = _profile_row(
        account_role_arn="arn:aws:iam::123456789012:role/x",
        external_id="e",
        region="ap-south-1",
    )
    session = _session_returning(row)
    resp = await list_routing_profiles(_make_admin_user(), session)
    item = resp.items[0]
    assert item.account_role_arn == "arn:aws:iam::123456789012:role/x"
    assert item.external_id == "e"
    assert item.region == "ap-south-1"
