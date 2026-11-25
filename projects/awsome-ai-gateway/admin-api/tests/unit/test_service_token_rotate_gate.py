# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""POST /admin/service-tokens/rotate 게이트 회귀 — svc 토큰 self-rotate 만 허용.

R32(92670d53)가 service_tokens 의 모든 mutation 을 require_human_admin 으로 묶으면서
rotate 도 바꿨다. rotate 핸들러는 호출 svc 토큰 자신만 교체하므로 결과는
svc → 403, 사람 → 400 — **아무도 호출할 수 없었다**. 여기 테스트는 실제 라우터 +
실제 get_current_user(svc 분기는 verify 만 대역) 로 HTTP 상태를 본다. 게이트를
require_human_admin 으로 되돌리면 test_svc_token_self_rotate_200 이 403 으로 실패한다.

실 PostgreSQL 수명주기(grace·분기 차단·동시 rotate·계보 폐기)는
tests/regression/test_service_token_rotate_realpg.py.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.auth import (
    JWTVerifier,
    require_human_admin,
    require_service_token_self,
)
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError, RateLimitedError, ValidationError
from app.models.auth import UserRole
from app.schemas.service_tokens import ServiceTokenCreateResponse
from app.services.service_token_service import (
    SERVICE_TOKEN_ROTATE_MAX_ANCESTORS_IN_GRACE,
    ServiceTokenService,
)

SVC_RAW = "svc-" + "ab" * 32  # 형식 검사(64 hex) 통과용 — 실제 검증은 verify 대역
CALLER_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()
ADMIN_SUB = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _StubJWT(JWTVerifier):
    def __init__(self) -> None:  # 키 로드 없음
        self._public_keys = {}

    def verify(self, token: str) -> dict:
        from jose import JWTError

        if token == "admin-jwt":
            return {"sub": str(ADMIN_SUB), "email": "a@t", "role": "ADMIN"}
        if token == "dev-jwt":
            return {"sub": str(uuid.uuid4()), "email": "d@t", "role": "DEVELOPER"}
        raise JWTError("bad")


def _new_resp() -> ServiceTokenCreateResponse:
    now = datetime.now(timezone.utc)
    return ServiceTokenCreateResponse(
        id=str(uuid.uuid4()), name="aicm", token_prefix="svc-cdcdcdcd",
        created_at=now, expires_at=now + timedelta(days=30), token="svc-" + "cd" * 32,
    )


@pytest.fixture
def harness(monkeypatch):
    from app.main import register_exception_handlers
    from app.routers import service_tokens

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(service_tokens.router)
    app.state.jwt_verifier = _StubJWT()
    svc = MagicMock()
    svc.rotate = AsyncMock(side_effect=lambda *a, **k: _new_resp())
    svc.issue = AsyncMock(side_effect=lambda *a, **k: _new_resp())
    svc.revoke = AsyncMock(return_value=[])
    svc.end_grace = AsyncMock(return_value=(False, None))
    app.state.service_token_service = svc

    session = MagicMock()
    session.add = MagicMock()

    async def _db(request: Request):
        yield session

    app.dependency_overrides[get_db_session] = _db

    # get_current_user 의 svc 분기 — 실제 코드 경로, DB 조회만 대역.
    tok = MagicMock()
    tok.id = CALLER_ID

    @asynccontextmanager
    async def _sl():
        yield MagicMock()

    monkeypatch.setattr("app.core.auth.AsyncSessionLocal", _sl)
    verify = AsyncMock(return_value=tok)
    monkeypatch.setattr(ServiceTokenService, "verify", verify)

    audit = AsyncMock()
    monkeypatch.setattr("app.routers.service_tokens.audit_logger.log", audit)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return client, svc, audit, verify


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def test_svc_token_self_rotate_200(harness):
    client, svc, audit, _ = harness
    resp = await client.post("/admin/service-tokens/rotate", headers=_h(SVC_RAW))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"].startswith("svc-")
    svc.rotate.assert_awaited_once()
    assert svc.rotate.await_args.kwargs["token_id"] == CALLER_ID
    # 감사: SERVICE_TOKEN_ROTATE + 행위 토큰 id
    kw = audit.await_args.kwargs
    assert kw["action"] == "SERVICE_TOKEN_ROTATE"
    assert kw["service_token_id"] == CALLER_ID
    assert kw["changes"]["before"]["token_id"] == str(CALLER_ID)
    assert kw["changes"]["after"]["token_id"] == body["id"]
    assert kw["resource_id"] == body["id"]


async def test_svc_token_cannot_target_another_token(harness):
    """본문·쿼리로 다른 토큰 id 를 실어도 대상은 호출 토큰 자신."""
    client, svc, _, _ = harness
    resp = await client.post(
        f"/admin/service-tokens/rotate?token_id={OTHER_ID}",
        json={"token_id": str(OTHER_ID), "id": str(OTHER_ID)},
        headers=_h(SVC_RAW),
    )
    assert resp.status_code == 200
    assert svc.rotate.await_args.kwargs["token_id"] == CALLER_ID


@pytest.mark.parametrize("jwt_token", ["admin-jwt", "dev-jwt"])
async def test_human_jwt_gets_403(harness, jwt_token):
    client, svc, audit, _ = harness
    resp = await client.post("/admin/service-tokens/rotate", headers=_h(jwt_token))
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["type"] == "forbidden"
    assert "service token" in err["message"].lower()
    svc.rotate.assert_not_awaited()
    audit.assert_not_awaited()


async def test_unauthenticated_401(harness):
    client, svc, _, _ = harness
    assert (await client.post("/admin/service-tokens/rotate")).status_code == 401
    svc.rotate.assert_not_awaited()


async def test_invalid_or_expired_svc_token_401(harness):
    client, svc, _, verify = harness
    verify.return_value = None  # verify 가 만료/폐기/grace 종료를 None 으로 돌린다
    resp = await client.post("/admin/service-tokens/rotate", headers=_h(SVC_RAW))
    assert resp.status_code == 401
    svc.rotate.assert_not_awaited()


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/admin/service-tokens"),
        ("DELETE", f"/admin/service-tokens/{OTHER_ID}"),
        ("POST", f"/admin/service-tokens/{OTHER_ID}/end-grace"),
    ],
)
async def test_other_mutations_stay_human_only(harness, method, path):
    client, svc, _, _ = harness
    kwargs = {"json": {"name": "x"}} if path == "/admin/service-tokens" else {}
    resp = await client.request(method, path, headers=_h(SVC_RAW), **kwargs)
    assert resp.status_code == 403
    svc.issue.assert_not_awaited()
    svc.revoke.assert_not_awaited()
    svc.end_grace.assert_not_awaited()


async def test_rotate_rate_limited_maps_to_429_with_retry_after(harness):
    client, svc, audit, _ = harness
    svc.rotate.side_effect = RateLimitedError(
        "too many", code="service_token_rotate_rate_limited", retry_after=120
    )
    resp = await client.post("/admin/service-tokens/rotate", headers=_h(SVC_RAW))
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "120"
    assert resp.json()["error"]["code"] == "service_token_rotate_rate_limited"
    audit.assert_not_awaited()


def test_rotate_route_dependency_identity():
    """게이트 함수 identity — require_human_admin 으로 회귀하면 실패."""
    from fastapi.routing import APIRoute

    from app.routers.service_tokens import router

    by_key = {
        (r.path, m): {d.call for d in r.dependant.dependencies}
        for r in router.routes if isinstance(r, APIRoute) for m in r.methods
    }
    rotate = by_key[("/admin/service-tokens/rotate", "POST")]
    assert require_service_token_self in rotate
    assert require_human_admin not in rotate
    for key in [
        ("/admin/service-tokens", "POST"),
        ("/admin/service-tokens/{token_id}", "DELETE"),
        ("/admin/service-tokens/{token_id}/end-grace", "POST"),
    ]:
        assert require_human_admin in by_key[key], key


async def test_require_service_token_self_unit():
    svc = MagicMock(is_service_token=True, service_token_id=uuid.uuid4(), role=UserRole.ADMIN)
    assert await require_service_token_self(user=svc) is svc
    human = MagicMock(is_service_token=False, service_token_id=None, role=UserRole.ADMIN)
    with pytest.raises(ForbiddenError):
        await require_service_token_self(user=human)
    broken = MagicMock(is_service_token=True, service_token_id=None, role=UserRole.ADMIN)
    with pytest.raises(ForbiddenError):
        await require_service_token_self(user=broken)


# ── 서비스 계층: 분기 차단 메시지 + 계보 rotate 빈도 상한 ──────────────────────


def _tok(*, rotated_from=None, revoked_at=None, days_left=30):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.name = "aicm"
    t.created_by = uuid.uuid4()
    t.expires_at = datetime.now(timezone.utc) + timedelta(days=days_left)
    t.revoked_at = revoked_at
    t.rotated_from = rotated_from
    return t


def _repo(rows):
    by_id = {r.id: r for r in rows}
    repo = MagicMock()
    repo.get_by_id = AsyncMock(side_effect=lambda tid, for_update=False: by_id.get(tid))
    repo.set_revoked_at = AsyncMock()
    repo.create = AsyncMock()
    return repo


async def _rotate(repo, token_id):
    with patch("app.services.service_token_service.ServiceTokenRepository", return_value=repo):
        return await ServiceTokenService().rotate(AsyncMock(), token_id=token_id, created_by=uuid.uuid4())


async def test_in_grace_token_rotate_rejected_with_successor_hint():
    t = _tok(revoked_at=datetime.now(timezone.utc) + timedelta(hours=10))
    with pytest.raises(ValidationError, match="successor"):
        await _rotate(_repo([t]), t.id)


async def test_lineage_rate_limit_blocks_when_ancestors_in_grace():
    assert SERVICE_TOKEN_ROTATE_MAX_ANCESTORS_IN_GRACE == 2
    now = datetime.now(timezone.utc)
    root = _tok(revoked_at=now + timedelta(hours=2))
    mid = _tok(rotated_from=root.id, revoked_at=now + timedelta(hours=20))
    leaf = _tok(rotated_from=mid.id)
    repo = _repo([root, mid, leaf])
    with pytest.raises(RateLimitedError) as ei:
        await _rotate(repo, leaf.id)
    # 가장 먼저 끝나는 grace(root, ~2h) 뒤 재시도 가능
    assert 7000 < ei.value.retry_after <= 7201
    repo.create.assert_not_awaited()
    repo.set_revoked_at.assert_not_awaited()


async def test_lineage_rate_limit_ignores_ancestors_out_of_grace():
    now = datetime.now(timezone.utc)
    root = _tok(revoked_at=now - timedelta(seconds=1))  # grace 끝남(end-grace 포함)
    mid = _tok(rotated_from=root.id, revoked_at=now + timedelta(hours=20))
    leaf = _tok(rotated_from=mid.id)
    repo = _repo([root, mid, leaf])
    resp = await _rotate(repo, leaf.id)
    assert resp.token.startswith("svc-")
    repo.set_revoked_at.assert_awaited_once()
