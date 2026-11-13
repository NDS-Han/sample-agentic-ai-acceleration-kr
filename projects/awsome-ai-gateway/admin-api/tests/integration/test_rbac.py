# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""RBAC integration tests — verify role-based access control across all routers."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestAdminOnlyEndpoints:
    """Endpoints that require ADMIN role."""

    ADMIN_ONLY_ENDPOINTS = [
        ("GET", "/admin/keys"),
        ("GET", "/admin/models"),
        ("GET", "/admin/users"),
    ]

    @pytest.mark.parametrize("method,url", ADMIN_ONLY_ENDPOINTS)
    async def test_developer_rejected(self, client: AsyncClient, dev_headers: dict, method: str, url: str):
        resp = await client.request(method, url, headers=dev_headers)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,url", ADMIN_ONLY_ENDPOINTS)
    async def test_no_auth_rejected(self, client: AsyncClient, method: str, url: str):
        resp = await client.request(method, url)
        assert resp.status_code == 401

    async def test_invalid_token_rejected(self, client: AsyncClient):
        resp = await client.get("/admin/keys", headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401


class TestAdminOrTeamLeaderEndpoints:
    """Endpoints that allow both ADMIN and TEAM_LEADER."""

    async def test_leader_can_access_budget_summary(self, client: AsyncClient, leader_headers: dict):
        # This will fail at the service layer (no DB), but should not fail at auth
        resp = await client.get(
            "/admin/budgets/summary?period=2026-04",
            headers=leader_headers,
        )
        # 500 is expected (no DB), but not 403
        assert resp.status_code != 403

    async def test_leader_can_access_analytics(self, client: AsyncClient, leader_headers: dict):
        resp = await client.get(
            "/admin/analytics?period=2026-04",
            headers=leader_headers,
        )
        assert resp.status_code != 403

    async def test_developer_rejected_from_budget_summary(self, client: AsyncClient, dev_headers: dict):
        resp = await client.get(
            "/admin/budgets/summary?period=2026-04",
            headers=dev_headers,
        )
        assert resp.status_code == 403

    async def test_developer_rejected_from_analytics(self, client: AsyncClient, dev_headers: dict):
        resp = await client.get(
            "/admin/analytics?period=2026-04",
            headers=dev_headers,
        )
        assert resp.status_code == 403


class TestLeaderBudgetMutations:
    """R32-M1 — 팀 스코프 예산 mutation 은 라우터에서 leader 를 통과시킨다.

    과거엔 require_human_admin 으로 라우터에서 403 이었고, 서비스의 리더
    스코핑(own-team/self/leader-target 검사)에 도달조차 못 했다. 게이트를
    지난 뒤의 팀 경계는 서비스가 403 으로 돌린다 — 여기선 라우터 통과만 본다.
    """

    async def test_leader_reaches_user_budget_mutation(self, client: AsyncClient, leader_headers: dict):
        resp = await client.put(
            "/admin/budgets/user/00000000-0000-0000-0000-000000000001",
            json={"max_budget_usd": 10},
            headers=leader_headers,
        )
        # 서비스/DB 계층 실패는 허용 — 라우터 게이트의 403 만 아니면 된다
        assert resp.status_code != 403

    async def test_leader_reaches_allocate(self, client: AsyncClient, leader_headers: dict):
        resp = await client.put(
            "/admin/budgets/team/00000000-0000-0000-0000-000000000001/allocate",
            json={"allocations": []},
            headers=leader_headers,
        )
        assert resp.status_code != 403

    async def test_developer_still_rejected_from_mutation(self, client: AsyncClient, dev_headers: dict):
        resp = await client.put(
            "/admin/budgets/user/00000000-0000-0000-0000-000000000001",
            json={"max_budget_usd": 10},
            headers=dev_headers,
        )
        assert resp.status_code == 403


class TestCLIEndpointsNoAuth:
    """CLI endpoints do not require JWT auth."""

    async def test_cli_setup_no_auth_required(self, client: AsyncClient):
        resp = await client.post("/cli/setup", json={
            "device_name": "test-device",
            "os": "darwin",
            "arch": "arm64",
            "detected_tools": ["claude-code"],
        })
        assert resp.status_code == 200

    async def test_cli_download_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/cli/download/darwin/arm64")
        # No auth gate: 200 when the built CLI tarball is present in CLI_DIST_DIR,
        # 404 when it isn't (router-level tests don't build the artifact). Either
        # way the endpoint must not be auth-gated (never 401/403). Mirrors
        # test_health_no_auth_required's infra-tolerant assertion.
        assert resp.status_code in (200, 404)
        assert resp.status_code not in (401, 403)


class TestHealthEndpoint:
    async def test_health_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/health")
        # May be 503 (degraded) without real DB/Redis, but should not be 401/403
        assert resp.status_code in (200, 503)
