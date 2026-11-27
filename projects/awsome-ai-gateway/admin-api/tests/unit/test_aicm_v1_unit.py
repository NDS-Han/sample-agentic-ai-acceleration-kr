# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 단위 — 통합 토큰 인증 순서, Cognito 래퍼(타임아웃·오류 매핑·쿼터 가드), 멱등 키 형식.

DB 없이 돈다. 실 PG + Stubber 전 경로는 tests/regression/test_aicm_v1_realpg.py.
"""
from __future__ import annotations

import ipaddress
import uuid

import pytest

from app.core.exceptions import IntegrationError

# ── integration auth ─────────────────────────────────────────────────────────


@pytest.fixture
async def client(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core.db import get_db_session
    from app.main import register_exception_handlers
    from app.routers import aicm_admin as aicm
    from app.services.integration_token_service import IntegrationTokenRecord, IntegrationTokenService

    lineage = uuid.uuid4()
    records = {
        "svc-" + "a" * 64: IntegrationTokenRecord(
            token_id=uuid.uuid4(), name="ops", integration="aicm", scopes=frozenset({"tokens:read"}),
            allowed_cidrs=(ipaddress.ip_network("10.0.0.0/8"),), lineage_id=lineage),
        "svc-" + "b" * 64: IntegrationTokenRecord(
            token_id=uuid.uuid4(), name="other", integration="other", scopes=frozenset({"tokens:read"}),
            lineage_id=lineage),
    }

    async def verify(self, session, raw):
        return records.get(raw)

    async def list_lineage(self, session, lineage_id):
        return []

    monkeypatch.setattr(IntegrationTokenService, "verify", verify)
    monkeypatch.setattr(IntegrationTokenService, "list_lineage", list_lineage)

    class _Redis:
        def __init__(self):
            self.kv = {}

        async def incr(self, k):
            self.kv[k] = self.kv.get(k, 0) + 1
            return self.kv[k]

        async def expire(self, k, s):
            return True

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(aicm.router)

    async def _no_db():
        yield None

    app.dependency_overrides[get_db_session] = _no_db
    app.state.redis = _Redis()

    class _SvcTokens:
        """mirror 가 재사용하는 real admin handler 의 state 의존 스텁 — 인증은
        IntegrationTokenService.verify 몽키패치가 담당하므로 여기선 빈 결과만 돌려준다."""

        def __getattr__(self, name):
            from types import SimpleNamespace

            async def _any(*a, **kw):
                if name.startswith("list"):
                    return SimpleNamespace(items=[])
                return None

            return _any

    app.state.service_token_service = _SvcTokens()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, app


def _hdr(tok, ip="10.1.1.1"):
    return {"Authorization": f"Bearer {tok}", "X-Forwarded-For": ip}


async def test_auth_order_and_codes(client, monkeypatch):
    c, app = client
    good = "svc-" + "a" * 64
    cases = [
        ({}, 401, "invalid_integration_token"),
        ({"Authorization": "Bearer itg-" + "a" * 64}, 401, "invalid_integration_token"),
        ({"Authorization": "Bearer svc-short"}, 401, "invalid_integration_token"),
        (_hdr("svc-" + "c" * 64), 401, "invalid_integration_token"),  # 모르는 토큰
        (_hdr("svc-" + "b" * 64), 401, "invalid_integration_token"),  # 다른 통합
        (_hdr(good, ip="8.8.8.8"), 403, "source_ip_not_allowed"),
        (_hdr(good), 200, None),
    ]
    for headers, status, code in cases:
        r = await c.get("/aicm/v1/admin/service-tokens", headers=headers)
        assert r.status_code == status, (headers, r.text)
        if code:
            assert r.json()["error"]["code"] == code
    # 쿠키(admin_jwt)는 통합 토큰 경로로 받지 않는다
    r = await c.get("/aicm/v1/admin/service-tokens", headers={"Cookie": f"admin_jwt={good}"})
    assert r.status_code == 401


async def test_scope_checked_after_ip(client):
    c, _ = client
    good = "svc-" + "a" * 64
    r = await c.post("/aicm/v1/admin/service-tokens/rotate", headers=_hdr(good, ip="8.8.8.8"))
    assert r.json()["error"]["code"] == "source_ip_not_allowed"
    r = await c.post("/aicm/v1/admin/service-tokens/rotate", headers=_hdr(good))
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    assert r.json()["error"]["details"] == {"required_scope": "tokens:rotate"}


async def test_rate_limit_per_lineage(client, monkeypatch):
    from app.core.config import get_settings

    c, _ = client
    monkeypatch.setattr(get_settings(), "AICM_RL_READ_PER_MIN", 3)
    good = "svc-" + "a" * 64
    codes = [(await c.get("/aicm/v1/admin/service-tokens", headers=_hdr(good))).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


async def test_rate_limit_falls_back_to_local_when_redis_down(client, monkeypatch):
    from app.core import integration_auth
    from app.core.config import get_settings

    c, app = client

    class _Down:
        async def incr(self, k):
            raise ConnectionError("down")

    app.state.redis = _Down()
    integration_auth._LOCAL_RL.clear()
    monkeypatch.setattr(get_settings(), "AICM_RL_READ_PER_MIN", 2)
    good = "svc-" + "a" * 64
    codes = [(await c.get("/aicm/v1/admin/service-tokens", headers=_hdr(good))).status_code for _ in range(3)]
    assert codes == [200, 429, 429]  # Redis 장애 로컬 폴백은 replica 수로 나눈 cap(2//3 → 1)


def test_audit_tags_only_inside_integration_requests():
    from app.core.audit import audit_tags

    assert audit_tags.get() is None


async def test_audit_logger_adds_tags_from_context():
    from unittest.mock import MagicMock

    from app.core.audit import AuditLogger, audit_tags

    session = MagicMock()
    tok = audit_tags.set({"_integration": "aicm", "_idempotency_key": "k1"})
    try:
        await AuditLogger().log(session, actor_user_id=uuid.uuid4(), actor_role="ADMIN", action="X",
                                resource_type="T", resource_id="1", changes={"a": 1})
    finally:
        audit_tags.reset(tok)
    row = session.add.call_args[0][0]
    assert row.changes == {"a": 1, "_integration": "aicm", "_idempotency_key": "k1"}
    session2 = MagicMock()
    await AuditLogger().log(session2, actor_user_id=uuid.uuid4(), actor_role="ADMIN", action="X",
                            resource_type="T", resource_id="1", changes={"a": 1})
    assert session2.add.call_args[0][0].changes == {"a": 1}


# ── cognito wrapper ──────────────────────────────────────────────────────────


def _client():
    import boto3

    return boto3.client("cognito-idp", region_name="ap-northeast-2", aws_access_key_id="x",
                        aws_secret_access_key="x")


def test_build_client_sets_timeouts_and_retries():
    from app.services.cognito_admin import build_client

    cfg = build_client().meta.config
    assert cfg.connect_timeout == 3.0 and cfg.read_timeout == 5.0
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}


@pytest.mark.parametrize("code,status,mapped", [
    ("TooManyRequestsException", 503, "cognito_throttled"),
    ("LimitExceededException", 503, "cognito_throttled"),
    ("AccessDeniedException", 503, "cognito_permission_missing"),
    ("ResourceNotFoundException", 503, "cognito_misconfigured"),
    ("InvalidParameterException", 400, "cognito_invalid_parameter"),
    ("UserNotFoundException", 404, "cognito_user_not_found"),
    ("InternalErrorException", 502, "cognito_error"),
])
def test_client_error_mapping(code, status, mapped):
    from botocore.exceptions import ClientError

    from app.services.cognito_admin import map_error

    e = map_error(ClientError({"Error": {"Code": code, "Message": "m"}}, "AdminDisableUser"), "admin_disable_user")
    assert (e.status_code, e.code) == (status, mapped)
    assert e.details["cognito_code"] == code
    if mapped == "cognito_throttled":
        assert e.retry_after == 1


def test_transport_error_mapping():
    from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError

    from app.services.cognito_admin import map_error

    assert map_error(ReadTimeoutError(endpoint_url="u"), "list_users").code == "cognito_timeout"
    assert map_error(ConnectTimeoutError(endpoint_url="u"), "list_users").status_code == 504
    assert map_error(EndpointConnectionError(endpoint_url="u"), "list_users").code == "cognito_timeout"
    assert map_error(NoCredentialsError(), "list_users").code == "cognito_permission_missing"


async def test_call_passthrough_and_mapping():
    from botocore.stub import Stubber

    from app.services.cognito_admin import CognitoAdmin, CognitoClientError

    cl = _client()
    with Stubber(cl) as stub:
        stub.add_client_error("admin_create_user", service_error_code="UsernameExistsException")
        stub.add_client_error("admin_create_user", service_error_code="UsernameExistsException")
        cog = CognitoAdmin(cl, user_pool_id="p")
        with pytest.raises(CognitoClientError) as ei:
            await cog.call("admin_create_user", passthrough={"UsernameExistsException"}, Username="u")
        assert ei.value.code == "UsernameExistsException"
        with pytest.raises(IntegrationError) as ei2:
            await cog.call("admin_create_user", Username="u")
        assert ei2.value.status_code == 502


async def test_call_without_pool_is_503():
    from app.services.cognito_admin import CognitoAdmin

    with pytest.raises(IntegrationError) as ei:
        await CognitoAdmin(_client(), user_pool_id="").call("list_users")
    assert ei.value.code == "cognito_not_configured"


async def test_quota_guard_busy(monkeypatch):
    from app.core.config import get_settings
    from app.services import cognito_admin

    monkeypatch.setattr(get_settings(), "AICM_COGNITO_RPS_USER_UPDATE", 1)

    class _R:
        def __init__(self):
            self.n = 0

        async def incrby(self, k, n):
            self.n += n
            return self.n

        async def expire(self, k, s):
            return True

    async def _nosleep(_):
        return None

    monkeypatch.setattr(cognito_admin.asyncio, "sleep", _nosleep)
    cog = cognito_admin.CognitoAdmin(_client(), redis=_R(), user_pool_id="p")
    await cog.acquire(cognito_admin.USER_UPDATE)
    with pytest.raises(IntegrationError) as ei:
        await cog.acquire(cognito_admin.USER_UPDATE)
    assert ei.value.code == "cognito_busy" and ei.value.status_code == 503 and ei.value.retry_after == 1


async def test_quota_guard_local_fallback_divides_cap(monkeypatch):
    from app.core.config import get_settings
    from app.services import cognito_admin

    s = get_settings()
    monkeypatch.setattr(s, "AICM_COGNITO_RPS_USER_LIST", 6)
    monkeypatch.setattr(s, "AICM_COGNITO_FALLBACK_DIVISOR", 3)
    monkeypatch.setattr(cognito_admin.time, "time", lambda: 1000.5)
    cognito_admin._LOCAL.clear()

    class _Down:
        async def incrby(self, k, n):
            raise ConnectionError

    cog = cognito_admin.CognitoAdmin(_client(), redis=_Down(), user_pool_id="p")
    assert [await cog._try_take(cognito_admin.USER_LIST, 1) for _ in range(3)] == [True, True, False]


# ── idempotency key format / fingerprint ─────────────────────────────────────


class _Req:
    def __init__(self, headers=None, body=b"", method="PUT", path="/aicm/v1/x", query=()):
        from starlette.datastructures import Headers, QueryParams

        self.headers = Headers(headers or {})
        self._body = body
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.query_params = QueryParams(list(query))

    async def body(self):
        return self._body


@pytest.mark.parametrize("value,code", [
    (None, "idempotency_key_required"), ("", "idempotency_key_required"),
    ("a" * 129, "idempotency_key_invalid"), ("has space", "idempotency_key_invalid"),
    ("tab\tkey", "idempotency_key_invalid"),
])
def test_idempotency_key_validation(value, code):
    from app.core.idempotency import idempotency_key

    headers = {} if value is None else {"Idempotency-Key": value}
    with pytest.raises(IntegrationError) as ei:
        idempotency_key(_Req(headers))
    assert ei.value.code == code


async def test_fingerprint_ignores_json_key_order_but_not_values():
    from app.core.idempotency import request_fingerprint

    a = await request_fingerprint(_Req(body=b'{"a":1,"b":[1,2]}'))
    b = await request_fingerprint(_Req(body=b'{ "b": [1, 2], "a": 1 }'))
    c = await request_fingerprint(_Req(body=b'{"a":2,"b":[1,2]}'))
    d = await request_fingerprint(_Req(body=b'{"a":1,"b":[1,2]}', path="/aicm/v1/y"))
    e = await request_fingerprint(_Req(body=b'{"a":1,"b":[1,2]}', query=[("confirm", "true")]))
    assert a == b and len({a, c, d, e}) == 4


# ── provision group validation (pure) ────────────────────────────────────────


