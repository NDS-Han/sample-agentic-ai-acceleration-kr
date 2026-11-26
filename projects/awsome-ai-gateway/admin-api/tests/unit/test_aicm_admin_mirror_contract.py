# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AICM admin mirror contract — docs/NDS/aicm/aicm-client-contract.json 을 파싱해 검사한다.

그 JSON은 wave 입력 `.agent-work/aicm-design/AICM_CLIENT_CONTRACT.md` 의 표에서 생성했다.
계약 #20/#22 는 method+path 가 같고 query scope 만 다르므로 실제 FastAPI route 는 25개다.
"""
from __future__ import annotations

import ipaddress
import json
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

_CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "NDS" / "aicm" / "aicm-client-contract.json"


def _rows():
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))["endpoints"]


def _canonical(path: str) -> str:
    # contract 는 대상을 `{id}` 로 적고 기존 admin 라우터는 `{user_id}` 를 쓴다.
    # group 은 FastAPI converter({groupname:path})로 선언되지만 OpenAPI/외부 템플릿은 같다.
    return (path.replace("{id}", "{user_id}")
                .replace("{groupname}", "{groupname:path}"))


def _app(monkeypatch, enabled=True):
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "AICM_API_ENABLED", enabled)
    return create_app()


def test_contract_has_26_rows_and_expected_unique_pairs():
    rows = _rows()
    assert len(rows) == 26
    pairs = {(r["method"], r["path"]) for r in rows}
    assert len(pairs) == 25  # #20/#22 는 같은 GET /admin/budgets/summary


def test_exactly_the_contract_pairs_are_mounted(monkeypatch):
    app = _app(monkeypatch, True)
    expected = {(r["method"], "/aicm/v1" + _canonical(r["path"])) for r in _rows()}
    actual = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/aicm/v1/admin/"):
            continue
        for method in route.methods or set():
            actual.add((method, route.path))
    assert actual == expected


def test_mirror_reuses_admin_endpoint_and_response_metadata(monkeypatch):
    app = _app(monkeypatch, True)
    mirror = {
        (m, r.path): r for r in app.routes if isinstance(r, APIRoute)
        for m in (r.methods or set()) if r.path.startswith("/aicm/v1/admin/")
    }
    admin = {
        (m, r.path): r for r in app.routes if isinstance(r, APIRoute)
        for m in (r.methods or set()) if r.path.startswith("/admin/")
    }
    for (method, mirrored_path), mr in mirror.items():
        ar = admin[(method, mirrored_path.removeprefix("/aicm/v1"))]
        assert mr.endpoint is ar.endpoint
        assert mr.response_model == ar.response_model
        assert mr.status_code == ar.status_code
        # mirror 의 dependency 가 원래 route dependency 이상이다(통합 scope + 선택적 target guard).
        assert len(mr.dependencies) >= len(ar.dependencies)


@pytest.fixture
async def mirror_client(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core.config import get_settings
    from app.core.db import get_db_session, install_commit_before_response
    from app.main import register_exception_handlers
    from app.routers.aicm_admin import router
    from app.services.integration_token_service import IntegrationTokenRecord, IntegrationTokenService

    monkeypatch.setattr(get_settings(), "AICM_API_ENABLED", True)
    good = uuid.uuid4()
    records = {
        "svc-" + "a" * 64: IntegrationTokenRecord(
            token_id=good, name="aicm", integration="aicm", scopes=frozenset({"tokens:read"}),
            allowed_cidrs=(ipaddress.ip_network("0.0.0.0/0"),), lineage_id=good),
        "svc-" + "b" * 64: IntegrationTokenRecord(
            token_id=uuid.uuid4(), name="aicm-noscope", integration="aicm", scopes=frozenset(),
            lineage_id=uuid.uuid4()),
        "itg-" + "a" * 64: IntegrationTokenRecord(
            token_id=uuid.uuid4(), name="legacy", integration="aicm", scopes=frozenset({"tokens:read"}),
            lineage_id=uuid.uuid4()),
    }

    async def verify(self, session, raw, prefixes=None):
        return records.get(raw)

    class _SM:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(IntegrationTokenService, "verify", verify)
    monkeypatch.setattr("app.core.integration_auth.AsyncSessionLocal", lambda: _SM())

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    install_commit_before_response(app)

    async def _no_db():
        yield None

    class _Tokens:
        async def list_tokens(self, session):
            return {"items": []}

    app.dependency_overrides[get_db_session] = _no_db
    app.state.service_token_service = _Tokens()
    app.state.redis = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.parametrize("token,status,code", [
    (None, 401, "invalid_integration_token"),
    ("human.jwt", 401, "invalid_integration_token"),
    ("svc-" + "f" * 64, 401, "invalid_integration_token"),  # generic/unknown svc
    ("svc-" + "b" * 64, 403, "insufficient_scope"),
    ("itg-" + "a" * 64, 403, "integration_surface_forbidden"),
    ("svc-" + "a" * 64, 200, None),
])
async def test_mirror_auth_matrix(mirror_client, token, status, code):
    headers = {"X-Forwarded-For": "10.0.0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await mirror_client.get("/aicm/v1/admin/service-tokens", headers=headers)
    assert r.status_code == status, r.text
    if code:
        assert r.json()["error"]["code"] == code


async def test_non_allowlisted_admin_path_is_not_mounted(mirror_client):
    r = await mirror_client.get(
        "/aicm/v1/admin/users/00000000-0000-4000-a000-000000000011/effective-policy",
        headers={"Authorization": "Bearer svc-" + "a" * 64},
    )
    assert r.status_code == 404
