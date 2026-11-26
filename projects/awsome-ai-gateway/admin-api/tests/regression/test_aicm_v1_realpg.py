# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 — 실 PostgreSQL + botocore Stubber(Cognito) 로 전 경로를 탄다.

PROOF_DSN 이 있을 때만 돈다. 자기 소유 DB(<dbname>_aicmproof)를 만들고 db/init SQL 을 적용한다.
HTTP 는 실제 /aicm/v1 라우터 + 통합 토큰 인증(실 DB 해시 조회) + 멱등 저장소 + CommittingRoute,
그리고 실제 서비스 계층(KeyService, CognitoSyncService)을 탄다.
Redis 는 호출을 기록하는 인메모리 대역, Cognito 는 Stubber(호출 순서·파라미터까지 고정)다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_aicmproof"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"
HUMAN_ADMIN = uuid.UUID("00000000-0000-4000-a000-000000000010")
POOL = "ap-northeast-2_TESTPOOL"

_TEST_KEY = "0123456789abcdef" * 4


def _enc():
    from app.core.encryption import AESEncryptionService

    return AESEncryptionService(_TEST_KEY)


class _LazyEnc:
    def encrypt(self, raw):
        return _enc().encrypt(raw)


_ENC = _LazyEnc()

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _admin_exec(sql: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _, _ = PROOF_DSN.rpartition("/")
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(sql))
    finally:
        await admin.dispose()


class FakeRedis:
    """Redis 대역 — 이 경로가 쓰는 명령만. 호출을 기록한다."""

    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.published: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    async def incrby(self, k, n):
        self.kv[k] = int(self.kv.get(k, 0)) + n
        return self.kv[k]

    async def expire(self, k, s):
        return True

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None, **kw):
        self.kv[k] = v
        return True

    async def delete(self, *ks):
        for k in ks:
            self.deleted.append(k)
            self.kv.pop(k, None)
        return len(ks)

    async def srem(self, k, *vs):
        return 0

    async def sadd(self, k, *vs):
        return 0

    async def smembers(self, k):
        return set()

    async def publish(self, ch, msg):
        self.published.append((ch, msg))
        return 0

    async def eval(self, *a, **kw):
        return None

    async def scan_iter(self, match=None, count=None):
        for k in list(self.kv):
            yield k

    def pipeline(self, *a, **kw):
        raise NotImplementedError


@pytest.fixture(scope="module")
async def owned_dsn():
    import asyncpg

    prefix, _, name = PROOF_DSN.rpartition("/")
    owned = f"{name}{_OWNED_SUFFIX}"
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')
    await _admin_exec(f'CREATE DATABASE "{owned}"')
    dsn = f"{prefix}/{owned}"
    conn = await asyncpg.connect(_asyncpg(dsn))
    try:
        for f in sorted(_INIT_DIR.glob("*.sql")):
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"  [init] {f.name}: {type(e).__name__}: {e}")
        assert await conn.fetchval("SELECT to_regclass('auth.integration_tokens') IS NOT NULL")
        assert await conn.fetchval("SELECT to_regclass('auth.integration_idempotency_keys') IS NOT NULL")
    finally:
        await conn.close()
    yield dsn
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')


@pytest.fixture
def cognito():
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "cognito-idp", region_name="ap-northeast-2",
        aws_access_key_id="x", aws_secret_access_key="x",
    )
    stub = Stubber(client)
    stub.activate()
    yield client, stub
    stub.deactivate()


@pytest.fixture
async def env(owned_dsn, monkeypatch, cognito):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.auth import JWTVerifier
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.core.config import get_settings
    from app.core.db import install_commit_before_response
    from app.core.encryption import AESEncryptionService
    from app.main import register_exception_handlers
    from app.routers import aicm, aicm_admin, budgets, models, service_tokens, users
    from app.services.budget_service import BudgetService
    from app.services.key_service import KeyService
    from app.services.model_service import ModelService
    from app.services.service_token_service import ServiceTokenService
    from app.services.user_team_service import UserTeamService

    s = get_settings()
    for k, v in {
        "COGNITO_USER_POOL_ID": POOL, "OIDC_PROVIDER_NAME": "oidc", "OIDC_GROUP_PREFIX": "Claude_",
        "ADMIN_GROUPS": ["ClaudeAdmin"], "ADMIN_EMAILS": [], "OIDC_GROUPS_CLAIM": "custom:groups",
        "COGNITO_FEDERATED_PROVIDER_NAME": "ADFS", "AICM_PROVISION_GROUPS_ATTRIBUTE": "custom:groups",
        "AICM_API_ENABLED": True,
    }.items():
        monkeypatch.setattr(s, k, v)

    engine = create_async_engine(owned_dsn, pool_size=10)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.core.auth.AsyncSessionLocal", sm)

    class _JWT(JWTVerifier):
        def __init__(self) -> None:
            self._public_keys = {}

        def verify(self, token: str) -> dict:
            from jose import JWTError

            if token == "human-admin":
                return {"sub": str(HUMAN_ADMIN), "email": "admin@dev.local", "role": "ADMIN"}
            raise JWTError("bad")

    redis = FakeRedis()
    cache_mgr = CacheInvalidationManager(redis)
    key_service = KeyService(
        encryption=AESEncryptionService(_TEST_KEY), cache_mgr=cache_mgr,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(aicm.router)
    app.include_router(service_tokens.router)
    app.include_router(users.router)
    app.include_router(budgets.router)
    app.include_router(models.router)
    app.include_router(aicm_admin.router)
    install_commit_before_response(app)
    app.state.jwt_verifier = _JWT()
    app.state.service_token_service = ServiceTokenService()
    app.state.redis = redis
    app.state.cache_mgr = cache_mgr
    app.state.key_service = key_service
    app.state.budget_service = BudgetService(cache_mgr=cache_mgr)
    app.state.model_service = ModelService(cache_mgr=cache_mgr)
    app.state.user_team_service = UserTeamService(cache_mgr=cache_mgr, key_service=key_service)
    app.state.aicm_cognito_client = cognito[0]
    app.state.oidc_verifier = None

    async with sm() as ses:
        for sql in (
            "DELETE FROM auth.integration_idempotency_keys",
            "DELETE FROM auth.integration_tokens",
            "DELETE FROM audit.audit_logs",
            "UPDATE auth.service_tokens SET rotated_from = NULL",
            "DELETE FROM auth.service_tokens",
        ):
            await ses.execute(text(sql))
        await ses.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield {"client": client, "sm": sm, "stub": cognito[1], "redis": redis,
               "key_service": key_service, "cache_mgr": cache_mgr, "cognito_client": cognito[0]}
    await engine.dispose()


HUMAN = {"Authorization": "Bearer human-admin"}
ALL_SCOPES = ["tokens:read", "tokens:rotate", "users:provision", "users:lifecycle"]


def _h(tok: str, key: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {tok}"}
    if key:
        h["Idempotency-Key"] = key
    return h


async def _issue(client, scopes=None, **kw) -> dict:
    body = {"name": "aicm-ops", "scopes": scopes or ALL_SCOPES, "legacy_lifecycle_token": True, **kw}
    r = await client.post("/aicm/v1/service-tokens", json=body, headers=HUMAN)
    assert r.status_code == 200, r.text
    return r.json()


async def _q(sm, sql: str, **params):
    from sqlalchemy import text

    async with sm() as s:
        return (await s.execute(text(sql), params)).all()


async def _mk_team(sm, name: str) -> uuid.UUID:
    from sqlalchemy import text

    tid = uuid.uuid4()
    async with sm() as s:
        dept = (await s.execute(text("SELECT id FROM auth.departments LIMIT 1"))).scalar()
        await s.execute(text("INSERT INTO auth.teams (id, name, dept_id) VALUES (:i, :n, :d)"),
                        {"i": tid, "n": name, "d": dept})
        await s.commit()
    return tid


async def _mk_user(sm, *, team_id=None, role="DEVELOPER", provider="oidc", sub=None, email=None,
                   keys: int = 0, service_account: bool = False) -> tuple[uuid.UUID, str]:
    from sqlalchemy import text

    uid = uuid.uuid4()
    sub = sub or str(uuid.uuid4())
    email = email or f"u{uid.hex[:8]}@corp.example"
    async with sm() as s:
        await s.execute(text(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject, provider, is_active, "
            "is_service_account) "
            "VALUES (:i, :t, :e, 'U', CAST(:r AS auth.user_role), :s, :p, true, :sa)"),
            {"i": uid, "t": team_id, "e": email, "r": role, "s": sub, "p": provider, "sa": service_account})
        for _ in range(keys):
            raw = f"vk-{uuid.uuid4().hex}"
            await s.execute(text(
                "INSERT INTO auth.virtual_keys (user_id, key_value_encrypted, key_prefix, status, expires_at, key_hash) "
                "VALUES (:u, :enc, 'vk-test', 'ACTIVE', now() + interval '1 day', :h)"),
                {"u": uid, "enc": _ENC.encrypt(raw), "h": hashlib.sha256(raw.encode()).hexdigest()})
        await s.commit()
    return uid, sub


def _cu(username, sub, email, *, enabled=True, status="CONFIRMED", groups=None, identities=None):
    attrs = [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email},
             {"Name": "email_verified", "Value": "true"}, {"Name": "name", "Value": "Kim"}]
    if groups is not None:
        attrs.append({"Name": "custom:groups", "Value": groups})
    if identities is not None:
        attrs.append({"Name": "identities", "Value": identities})
    return {"Username": username, "Attributes": attrs, "Enabled": enabled, "UserStatus": status}


def _stub_sync(stub, username, sub, email, *, enabled=True, groups="Claude_Platform"):
    stub.add_response("admin_get_user", {
        "Username": username, "Enabled": enabled, "UserStatus": "CONFIRMED",
        "UserAttributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email},
                           {"Name": "email_verified", "Value": "true"}, {"Name": "name", "Value": "Kim"},
                           {"Name": "custom:groups", "Value": groups}],
    }, {"UserPoolId": POOL, "Username": username})
    stub.add_response("admin_list_groups_for_user", {"Groups": []},
                      {"UserPoolId": POOL, "Username": username, "Limit": 60})


_COGNITO_WRITE_OPS = (
    "admin_create_user",
    "admin_link_provider_for_user",
    "admin_update_user_attributes",
    "admin_disable_user",
    "admin_user_global_sign_out",
)


def _spy_cognito_writes(client, stack: ExitStack) -> dict:
    return {
        name: stack.enter_context(patch.object(client, name, wraps=getattr(client, name)))
        for name in _COGNITO_WRITE_OPS
    }


def _assert_no_cognito_writes(writes: dict) -> None:
    for name, spy in writes.items():
        spy.assert_not_called(), name


def _stub_find_by_email(stub, email: str, users: list[dict]) -> None:
    stub.add_response("list_users", {"Users": users},
                      {"UserPoolId": POOL, "Filter": f'email = "{email}"', "Limit": 10})


def _stub_user_pool_groups(stub, username: str, groups: list[str] | None = None) -> None:
    stub.add_response(
        "admin_list_groups_for_user",
        {"Groups": [{"GroupName": g} for g in (groups or [])]},
        {"UserPoolId": POOL, "Username": username, "Limit": 60},
    )


# ── auth / tokens ────────────────────────────────────────────────────────────


async def test_token_issue_rotate_and_principal_isolation(env):
    c, sm = env["client"], env["sm"]
    t = await _issue(c, scopes=["tokens:read", "tokens:rotate"], allowed_cidrs=["10.0.0.0/8"])
    assert t["token"].startswith("itg-") and t["scopes"] == ["tokens:read", "tokens:rotate"]

    # 원천 IP 밖 → 403, 안 → 200 (XFF 가장 오른쪽 홉)
    r = await c.get("/aicm/v1/service-tokens", headers={**_h(t["token"]), "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "source_ip_not_allowed"
    inside = {"X-Forwarded-For": "9.9.9.9, 10.1.2.3"}
    r = await c.get("/aicm/v1/service-tokens", headers={**_h(t["token"]), **inside})
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [t["id"]]

    # itg- 는 /admin 에서 401, svc- 는 /aicm 에서 401, 사람 JWT 는 /aicm 통합 경로에서 401
    assert (await c.get("/admin/service-tokens", headers=_h(t["token"]))).status_code == 401
    svc = (await c.post("/admin/service-tokens", json={"name": "legacy"}, headers=HUMAN)).json()["token"]
    assert (await c.get("/aicm/v1/service-tokens", headers=_h(svc))).status_code == 401
    assert (await c.get("/aicm/v1/service-tokens", headers=HUMAN)).status_code == 401
    # svc- 토큰은 통합 토큰을 발급할 수 없다
    r = await c.post("/aicm/v1/service-tokens", json={"name": "x", "scopes": ["users:read"]}, headers=_h(svc))
    assert r.status_code == 403
    # 모르는 scope 는 발급 거부
    r = await c.post("/aicm/v1/service-tokens", json={"name": "x", "scopes": ["admin:*"]}, headers=HUMAN)
    assert r.status_code == 400

    # rotate — Idempotency-Key 필수
    r = await c.post("/aicm/v1/service-tokens/rotate", headers={**_h(t["token"]), **inside})
    assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_key_required"
    r = await c.post("/aicm/v1/service-tokens/rotate", headers={**_h(t["token"], "rot-1"), **inside})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["token"].startswith("itg-") and new["id"] != t["id"]
    # 재전송은 비밀을 다시 주지 않는다
    r = await c.post("/aicm/v1/service-tokens/rotate", headers={**_h(t["token"], "rot-1"), **inside})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_secret_not_replayable"
    stored = await _q(sm, "SELECT response::text FROM auth.integration_idempotency_keys WHERE idem_key='rot-1'")
    assert new["token"] not in stored[0][0]
    # 새 토큰: 같은 scope/CIDR, 같은 계보 목록
    r = await c.get("/aicm/v1/service-tokens", headers={**_h(new["token"]), **inside})
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["id"] for i in items} == {t["id"], new["id"]}
    assert all(i["scopes"] == ["tokens:read", "tokens:rotate"] and i["allowed_cidrs"] == ["10.0.0.0/8"]
               for i in items)
    # grace 중 구 토큰은 인증되지만 재-rotate 는 400(successor)
    r = await c.post("/aicm/v1/service-tokens/rotate", headers={**_h(t["token"], "rot-2"), **inside})
    assert r.status_code == 400 and "successor" in r.json()["error"]["message"]
    # 감사: rotate 1행, 통합 태그 + 멱등 키
    rows = await _q(sm, "SELECT changes::text FROM audit.audit_logs WHERE action='SERVICE_TOKEN_ROTATE'")
    assert len(rows) == 1
    ch = json.loads(rows[0][0])
    assert ch["_integration"] == "aicm" and ch["_idempotency_key"] == "rot-1"
    assert ch["_service_token_id"] == t["id"]

    # 비상 정지: 사람 admin 의 /admin DELETE 가 itg 계보 전체를 끊는다
    assert (await c.delete(f"/admin/service-tokens/{t['id']}", headers=HUMAN)).status_code == 204
    assert (await c.get("/aicm/v1/service-tokens", headers={**_h(new["token"]), **inside})).status_code == 401


async def test_scope_enforced_and_rate_limited(env, monkeypatch):
    from app.core.config import get_settings

    c = env["client"]
    t = await _issue(c, scopes=["tokens:read"])
    uid, _ = await _mk_user(env["sm"])
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"},
                     headers=_h(t["token"], "k1"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"

    monkeypatch.setattr(get_settings(), "AICM_RL_READ_PER_MIN", 2)
    env["redis"].kv.clear()
    codes = [(await c.get("/aicm/v1/service-tokens", headers=_h(t["token"]))).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await c.get("/aicm/v1/service-tokens", headers=_h(t["token"]))
    assert r.json()["error"]["code"] == "integration_rate_limited" and int(r.headers["Retry-After"]) >= 1


# ── deactivate ───────────────────────────────────────────────────────────────


async def test_deactivate_disables_cognito_revokes_keys_and_audits(env):
    c, sm, stub, redis = env["client"], env["sm"], env["stub"], env["redis"]
    t = await _issue(c, scopes=["users:lifecycle"])
    team = await _mk_team(sm, f"T{uuid.uuid4().hex[:6]}")
    uid, sub = await _mk_user(sm, team_id=team, keys=2)
    email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]

    stub.add_response("list_users", {"Users": [_cu("cog-1", sub, email)]},
                      {"UserPoolId": POOL, "Filter": f'sub = "{sub}"', "Limit": 1})
    stub.add_response("admin_disable_user", {}, {"UserPoolId": POOL, "Username": "cog-1"})
    stub.add_response("admin_user_global_sign_out", {}, {"UserPoolId": POOL, "Username": "cog-1"})
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "left company"},
                     headers=_h(t["token"], "d1"))
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": str(uid), "state": "deactivated", "cognito": "disabled", "revoked_key_count": 2}
    stub.assert_no_pending_responses()

    assert (await _q(sm, "SELECT is_active FROM auth.users WHERE id=:i", i=uid))[0][0] is False
    assert {s for (s,) in await _q(sm, "SELECT status::text FROM auth.virtual_keys WHERE user_id=:i", i=uid)} == {
        "REVOKED"}
    rows = await _q(sm, "SELECT changes::text FROM audit.audit_logs WHERE action='DEACTIVATE_USER'")
    ch = json.loads(rows[0][0])
    assert ch["revoked_key_count"] == 2 and ch["reason"] == "left company" and ch["_integration"] == "aicm"
    # 커밋 후: VK Redis 키 DEL + 게이트웨이 pub/sub(user_ids 포함)
    assert sum(1 for k in redis.deleted if k.startswith("key:vk:")) == 2
    msgs = [json.loads(m) for ch_, m in redis.published if ch_ == "gateway:auth:invalidate"]
    assert any(str(uid) in m["user_ids"] for m in msgs)
    assert any(len(m["key_hashes"]) == 2 for m in msgs)

    # 같은 키 재전송 → 저장된 응답, Cognito 호출 없음
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "left company"},
                     headers=_h(t["token"], "d1"))
    assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"

    # 새 키로 반복 → already_inactive, Cognito 는 수렴만(disable 생략)
    stub.add_response("list_users", {"Users": [_cu("cog-1", sub, email, enabled=False)]},
                      {"UserPoolId": POOL, "Filter": f'sub = "{sub}"', "Limit": 1})
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "again", "global_sign_out": False},
                     headers=_h(t["token"], "d2"))
    assert r.status_code == 200 and r.json()["state"] == "already_inactive"
    assert r.json()["cognito"] == "already_disabled"

    # 이후의 sync 는 Cognito Enabled=false 를 보고 비활성을 유지한다(되살아나지 않음).
    # standalone sync endpoint 는 phase 0a 표면에 없으므로 서비스 계층을 직접 부른다.
    from app.services.cognito_sync_service import CognitoSyncService

    _stub_sync(stub, "cog-1", sub, email, enabled=False)
    sync = CognitoSyncService(env["cognito_client"], key_service=env["key_service"], cache_mgr=env["cache_mgr"])
    async with sm() as ses:
        await sync.sync_user(ses, "cog-1")
    assert (await _q(sm, "SELECT is_active FROM auth.users WHERE id=:i", i=uid))[0][0] is False
    stub.assert_no_pending_responses()



async def test_deactivate_cognito_error_leaves_db_untouched_and_key_reusable(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:lifecycle"])
    uid, sub = await _mk_user(sm, keys=1)
    stub.add_response("list_users", {"Users": [_cu("cog-2", sub, "x@corp.example")]},
                      {"UserPoolId": POOL, "Filter": f'sub = "{sub}"', "Limit": 1})
    stub.add_client_error("admin_disable_user", service_error_code="AccessDeniedException",
                          service_message="not authorized", http_status_code=400)
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "e1"))
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["code"] == "cognito_permission_missing"
    assert err["details"]["iam_action"] == "cognito-idp:AdminDisableUser"
    assert (await _q(sm, "SELECT is_active FROM auth.users WHERE id=:i", i=uid))[0][0] is True
    assert (await _q(sm, "SELECT status::text FROM auth.virtual_keys WHERE user_id=:i", i=uid))[0][0] == "ACTIVE"
    assert not await _q(sm, "SELECT 1 FROM audit.audit_logs WHERE action='DEACTIVATE_USER'")

    # 같은 키로 재시도(선점 해제됨) — 이번엔 throttling → 503 + Retry-After
    stub.add_response("list_users", {"Users": [_cu("cog-2", sub, "x@corp.example")]},
                      {"UserPoolId": POOL, "Filter": f'sub = "{sub}"', "Limit": 1})
    stub.add_client_error("admin_disable_user", service_error_code="TooManyRequestsException",
                          http_status_code=400)
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "e1"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "cognito_throttled"
    assert r.headers["Retry-After"] == "1"
    stub.assert_no_pending_responses()


async def test_deactivate_refuses_admin_and_service_account(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:lifecycle"])
    admin_uid, _ = await _mk_user(sm, role="ADMIN")
    r = await c.post(f"/aicm/v1/users/{admin_uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "a1"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "target_is_admin"
    r = await c.post(f"/aicm/v1/users/{HUMAN_ADMIN}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "a2"))
    assert r.status_code == 403
    r = await c.post(f"/aicm/v1/users/{uuid.uuid4()}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "a3"))
    assert r.status_code == 404
    stub.assert_no_pending_responses()  # Cognito 호출 없음


async def test_deactivate_non_cognito_user_db_only(env):
    c, sm = env["client"], env["sm"]
    t = await _issue(c, scopes=["users:lifecycle"])
    uid, _ = await _mk_user(sm, provider="sts", keys=1)
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "n1"))
    assert r.status_code == 200 and r.json()["cognito"] == "not_cognito_user"
    assert r.json()["revoked_key_count"] == 1
    # reactivation 은 AICM 미결 질문에 달려 있어 phase 0a 표면에 없다.
    r = await c.post(f"/aicm/v1/users/{uid}/reactivate", headers=_h(t["token"], "n2"))
    assert r.status_code == 404


# ── provision ────────────────────────────────────────────────────────────────


def _created_user(username, email, sub, groups):
    return {"User": {"Username": username, "Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD",
                     "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email},
                                    {"Name": "custom:groups", "Value": groups}]}}


async def test_provision_refuses_admin_matched_by_email_and_replays_403(env):
    """DB admin 의 sub 와 Cognito sub 가 달라도, email fallback 으로 연결될 admin 은 건드리지 않는다."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    team = await _mk_team(sm, f"Adm{uuid.uuid4().hex[:6]}")
    email = f"admin-victim-{uuid.uuid4().hex[:6]}@corp.example"
    db_sub, cognito_sub = str(uuid.uuid4()), str(uuid.uuid4())
    uid, _ = await _mk_user(sm, team_id=team, role="ADMIN", sub=db_sub, email=email)
    username = "ADFS_admin victim".replace(" ", "_")
    desired = f"Claude_New{uuid.uuid4().hex[:6]}"
    _stub_find_by_email(stub, email, [
        _cu(username, cognito_sub, email, status="EXTERNAL_PROVIDER", groups="Claude_Old")
    ])
    _stub_user_pool_groups(stub, username)
    body = {"email": email, "groups": [desired], "link": {"value": "N"}}

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "pa-email"))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_admin"
        replay = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "pa-email"))
        assert replay.status_code == 403
        assert replay.json() == r.json()
        assert replay.headers["Idempotent-Replayed"] == "true"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()

    row = (await _q(sm, "SELECT role::text, team_id, is_active FROM auth.users WHERE id=:i", i=uid))[0]
    assert row == ("ADMIN", team, True)
    rows = await _q(
        sm,
        "SELECT actor_user_id, actor_role, result, changes::text FROM audit.audit_logs "
        "WHERE action='AICM_PROVISION_REFUSED' AND resource_id=:r",
        r=str(uid),
    )
    assert len(rows) == 1  # replay 는 새 감사 행을 만들지 않는다
    actor_id, actor_role, result, changes_raw = rows[0]
    assert actor_id == uuid.UUID("00000000-0000-4000-a000-000000000011")
    assert actor_role == "ADMIN" and result == "FAILURE"
    changes = json.loads(changes_raw)
    assert changes["reason_code"] == "gateway_admin"
    assert changes["_service_token_id"] == t["id"] and changes["_integration"] == "aicm"


async def test_provision_refuses_last_human_admin_without_demotion(env):
    """마지막 사람 admin 을 대상으로 한 provision 도 403 — role/team/is_active 는 그대로다."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    team = await _mk_team(sm, f"Last{uuid.uuid4().hex[:6]}")
    email = f"last-admin-{uuid.uuid4().hex[:6]}@corp.example"
    sub = str(uuid.uuid4())
    uid, _ = await _mk_user(sm, team_id=team, role="ADMIN", sub=sub, email=email)
    username = "ADFS_last_admin"
    desired = f"Claude_Demote{uuid.uuid4().hex[:6]}"
    _stub_find_by_email(stub, email, [
        _cu(username, sub, email, status="EXTERNAL_PROVIDER", groups="Claude_Old")
    ])
    _stub_user_pool_groups(stub, username)

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": email, "groups": [desired], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-last-admin"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_admin"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()
    row = (await _q(sm, "SELECT role::text, team_id, is_active FROM auth.users WHERE id=:i", i=uid))[0]
    assert row == ("ADMIN", team, True)


async def test_provision_refuses_cognito_pool_admin_group_even_when_db_not_admin(env):
    """DB 가 아직 DEVELOPER 여도 Cognito pool 그룹에 ADMIN_GROUPS 가 있으면 pre-write 거부."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    team = await _mk_team(sm, f"Pg{uuid.uuid4().hex[:6]}")
    uid, sub = await _mk_user(sm, team_id=team, role="DEVELOPER")
    email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]
    username = "ADFS_pool_admin"
    desired = f"Claude_Pool{uuid.uuid4().hex[:6]}"
    _stub_find_by_email(stub, email, [
        _cu(username, sub, email, status="EXTERNAL_PROVIDER", groups="Claude_Old")
    ])
    _stub_user_pool_groups(stub, username, ["ClaudeAdmin"])

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": email, "groups": [desired], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-pool-admin"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_admin"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()
    assert (await _q(sm, "SELECT role::text FROM auth.users WHERE id=:i", i=uid))[0][0] == "DEVELOPER"


async def test_provision_refuses_cognito_attribute_admin_group_even_when_db_not_admin(env):
    """ADMIN_GROUPS 가 Cognito groups 속성에만 있어도 DB role 과 무관하게 쓰기 전 거부."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    team = await _mk_team(sm, f"AttrAdm{uuid.uuid4().hex[:5]}")
    email = f"attr-admin-{uuid.uuid4().hex[:6]}@corp.example"
    sub = str(uuid.uuid4())
    uid, _ = await _mk_user(sm, team_id=team, role="DEVELOPER", sub=sub, email=email)
    username = f"attradm-{uuid.uuid4().hex[:6]}"
    desired = f"Claude_New{uuid.uuid4().hex[:6]}"
    _stub_find_by_email(stub, email, [_cu(username, sub, email, groups="Claude_Old;ClaudeAdmin")])
    _stub_user_pool_groups(stub, username)
    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [desired], "link": {"value": "N"}},
                         headers=_h(t["token"], "pa-attradm"))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["details"]["reason_code"] == "cognito_admin_group"
        _assert_no_cognito_writes(writes)
    row = (await _q(sm, "SELECT role::text, team_id, is_active FROM auth.users WHERE id=:i", i=uid))[0]
    assert row == ("DEVELOPER", team, True)
    assert await _q(sm, "SELECT 1 FROM audit.audit_logs WHERE action='AICM_PROVISION_REFUSED' AND resource_id=:r",
                    r=str(uid))


async def test_provision_refuses_system_and_service_account_targets(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])

    # 서비스 계정 — Cognito 에 같은 sub/email 이 있어도 연결/갱신하지 않는다.
    uid, sub = await _mk_user(sm, role="DEVELOPER", service_account=True)
    svc_email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]
    username = "ADFS_service_account"
    _stub_find_by_email(stub, svc_email, [
        _cu(username, sub, svc_email, status="EXTERNAL_PROVIDER", groups="Claude_Old")
    ])
    _stub_user_pool_groups(stub, username)

    # 시스템 사용자 — Cognito 에 없으면 생성부터 막는다(연결/속성 쓰기 이전).
    system_email = "service-token@system.internal"
    _stub_find_by_email(stub, system_email, [])

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": svc_email, "groups": [f"Claude_Svc{uuid.uuid4().hex[:6]}"], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-service-acct"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_service_account"

        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": system_email, "groups": [f"Claude_Sys{uuid.uuid4().hex[:6]}"], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-system-user"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_system_user"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()


async def test_provision_refuses_admin_email_before_create(env, monkeypatch):
    from app.core.config import get_settings

    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email = f"configured-admin-{uuid.uuid4().hex[:6]}@corp.example"
    monkeypatch.setattr(get_settings(), "ADMIN_EMAILS", [email])
    _stub_find_by_email(stub, email, [])

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": email, "groups": [f"Claude_Ae{uuid.uuid4().hex[:6]}"], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-admin-email"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "target_is_admin"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()
    assert not await _q(sm, "SELECT 1 FROM auth.users WHERE email=:e", e=email)


async def test_provision_existing_team_leader_same_team_keeps_role(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    team_name = f"Tl{uuid.uuid4().hex[:6]}"
    team = await _mk_team(sm, team_name)
    group = f"Claude_{team_name}"
    uid, sub = await _mk_user(sm, team_id=team, role="TEAM_LEADER")
    email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]
    username = "ADFS_team_leader"
    _stub_find_by_email(stub, email, [
        _cu(username, sub, email, status="EXTERNAL_PROVIDER", groups=group)
    ])
    _stub_user_pool_groups(stub, username)
    _stub_sync(stub, username, sub, email, groups=group)

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": email, "groups": [group], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-team-leader-same"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "TEAM_LEADER"
        _assert_no_cognito_writes(writes)  # 같은 그룹 — 속성 정정도 없다
    stub.assert_no_pending_responses()
    row = (await _q(sm, "SELECT role::text, team_id FROM auth.users WHERE id=:i", i=uid))[0]
    assert row == ("TEAM_LEADER", team)


async def test_provision_refuses_team_leader_team_move(env):
    """팀 리더를 다른 팀으로 옮기면 현재 sync 규칙상 DEVELOPER 로 강등되므로 쓰기 전에 거부."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    old_team_name = f"Old{uuid.uuid4().hex[:6]}"
    old_team = await _mk_team(sm, old_team_name)
    old_group = f"Claude_{old_team_name}"
    new_group = f"Claude_New{uuid.uuid4().hex[:6]}"
    uid, sub = await _mk_user(sm, team_id=old_team, role="TEAM_LEADER")
    email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]
    username = "ADFS_leader_move"
    _stub_find_by_email(stub, email, [
        _cu(username, sub, email, status="EXTERNAL_PROVIDER", groups=old_group)
    ])
    _stub_user_pool_groups(stub, username)

    with ExitStack() as stack:
        writes = _spy_cognito_writes(env["cognito_client"], stack)
        r = await c.post(
            "/aicm/v1/users/provision",
            json={"email": email, "groups": [new_group], "link": {"value": "N"}},
            headers=_h(t["token"], "pa-team-leader-move"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "team_leader_change_forbidden"
        _assert_no_cognito_writes(writes)
    stub.assert_no_pending_responses()
    row = (await _q(sm, "SELECT role::text, team_id FROM auth.users WHERE id=:i", i=uid))[0]
    assert row == ("TEAM_LEADER", old_team)


async def test_provision_existing_developer_team_change_still_works(env):
    """일반 DEVELOPER 의 팀 그룹 정정은 계속 동작하고, 비-팀 속성 그룹은 보존한다."""
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    old_team_name = f"DevOld{uuid.uuid4().hex[:6]}"
    new_team_name = f"DevNew{uuid.uuid4().hex[:6]}"
    old_team = await _mk_team(sm, old_team_name)
    uid, sub = await _mk_user(sm, team_id=old_team, role="DEVELOPER")
    email = (await _q(sm, "SELECT email FROM auth.users WHERE id=:i", i=uid))[0][0]
    username = "ADFS_developer"
    desired = f"Claude_{new_team_name}"
    current = f"PartnerPortal;Claude_{old_team_name}"
    merged = f"PartnerPortal;{desired}"
    _stub_find_by_email(stub, email, [
        _cu(username, sub, email, status="EXTERNAL_PROVIDER", groups=current)
    ])
    _stub_user_pool_groups(stub, username)
    stub.add_response("admin_update_user_attributes", {}, {
        "UserPoolId": POOL, "Username": username,
        "UserAttributes": [{"Name": "custom:groups", "Value": merged}],
    })
    _stub_sync(stub, username, sub, email, groups=merged)

    r = await c.post(
        "/aicm/v1/users/provision",
        json={"email": email, "groups": [desired], "link": {"value": "N"}},
        headers=_h(t["token"], "pa-dev-existing"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "already_federated"
    assert r.json()["role"] == "DEVELOPER"
    stub.assert_no_pending_responses()
    row = (await _q(
        sm,
        "SELECT u.role::text, t.name FROM auth.users u JOIN auth.teams t ON t.id=u.team_id WHERE u.id=:i",
        i=uid,
    ))[0]
    assert row == ("DEVELOPER", new_team_name)


async def test_provision_create_link_sync(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email, sub = f"new{uuid.uuid4().hex[:6]}@corp.example", str(uuid.uuid4())
    team_name = f"Prov{uuid.uuid4().hex[:6]}"
    groups = f"Claude_{team_name}"
    stub.add_response("list_users", {"Users": []}, {"UserPoolId": POOL, "Filter": f'email = "{email}"', "Limit": 10})
    stub.add_response("admin_create_user", _created_user("uuid-new", email, sub, groups), {
        "UserPoolId": POOL, "Username": email, "MessageAction": "SUPPRESS",
        "UserAttributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                           {"Name": "name", "Value": "Kim"}, {"Name": "custom:groups", "Value": groups}]})
    stub.add_response("admin_link_provider_for_user", {}, {
        "UserPoolId": POOL,
        "DestinationUser": {"ProviderName": "Cognito", "ProviderAttributeValue": "uuid-new"},
        "SourceUser": {"ProviderName": "ADFS", "ProviderAttributeName": "Cognito_Subject",
                       "ProviderAttributeValue": "NAMEID-1"}})
    _stub_sync(stub, "uuid-new", sub, email, groups=groups)
    body = {"email": email, "display_name": "Kim", "groups": [groups], "link": {"value": "NAMEID-1"}}
    r = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "p1"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "created" and out["cognito_username"] == "uuid-new" and out["is_active"] is True
    stub.assert_no_pending_responses()
    row = (await _q(sm, "SELECT u.sso_subject, t.name FROM auth.users u JOIN auth.teams t ON t.id=u.team_id "
                        "WHERE u.id=:i", i=uuid.UUID(out["user_id"])))[0]
    assert row == (sub, team_name)
    aud = await _q(sm, "SELECT changes::text FROM audit.audit_logs WHERE action='PROVISION_USER'")
    ch = json.loads(aud[0][0])
    assert ch["state"] == "created" and ch["link_attribute"] == "Cognito_Subject" and "NAMEID-1" not in aud[0][0]
    assert ch["_integration"] == "aicm"

    # 재전송(같은 키) → 저장된 응답, Cognito 호출 없음
    r = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "p1"))
    assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"
    stub.assert_no_pending_responses()


async def test_provision_partial_failure_resumes_on_retry(env):
    c, sm, stub = env["client"], env["sm"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email, sub = f"part{uuid.uuid4().hex[:6]}@corp.example", str(uuid.uuid4())
    groups = f"Claude_Part{uuid.uuid4().hex[:6]}"
    flt = {"UserPoolId": POOL, "Filter": f'email = "{email}"', "Limit": 10}
    stub.add_response("list_users", {"Users": []}, flt)
    stub.add_response("admin_create_user", _created_user("uuid-p", email, sub, groups))
    stub.add_client_error("admin_link_provider_for_user", service_error_code="InternalErrorException",
                          http_status_code=500)
    body = {"email": email, "groups": [groups], "link": {"attribute": "email", "value": email}}
    r = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "pp"))
    assert r.status_code == 502, r.text
    assert r.json()["error"]["details"]["partial_state"] == "created"
    assert not await _q(sm, "SELECT 1 FROM auth.users WHERE sso_subject=:s", s=sub)

    # 재시도: 네이티브 사용자를 찾아 연결만 하고 sync → linked
    stub.add_response("list_users", {"Users": [_cu("uuid-p", sub, email, status="FORCE_CHANGE_PASSWORD",
                                                   groups=groups)]}, flt)
    _stub_user_pool_groups(stub, "uuid-p")
    stub.add_response("admin_link_provider_for_user", {})
    _stub_sync(stub, "uuid-p", sub, email, groups=groups)
    r = await c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "pp"))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "linked"
    stub.assert_no_pending_responses()


async def test_provision_already_federated_and_guards(env):
    c, stub = env["client"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email, sub = f"fed{uuid.uuid4().hex[:6]}@corp.example", str(uuid.uuid4())
    groups = f"Claude_Fed{uuid.uuid4().hex[:6]}"
    flt = {"UserPoolId": POOL, "Filter": f'email = "{email}"', "Limit": 10}

    # 이미 ADFS 로 로그인한 사람: 생성·연결·삭제 없음, 그룹만 맞추고 sync
    stub.add_response("list_users", {"Users": [_cu("ADFS_abc", sub, email, status="EXTERNAL_PROVIDER",
                                                   groups="Claude_Old")]}, flt)
    _stub_user_pool_groups(stub, "ADFS_abc")
    stub.add_response("admin_update_user_attributes", {}, {
        "UserPoolId": POOL, "Username": "ADFS_abc", "UserAttributes": [{"Name": "custom:groups", "Value": groups}]})
    _stub_sync(stub, "ADFS_abc", sub, email, groups=groups)
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups], "link": {"value": "N"}},
                     headers=_h(t["token"], "f1"))
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "already_federated"
    stub.assert_no_pending_responses()

    # 관리자 그룹 → 403, 접두사 밖 → 422, 연결 누락(연동 설정됨) → 422 — Cognito 호출 없음
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": ["ClaudeAdmin"],
                                                      "link": {"value": "N"}}, headers=_h(t["token"], "f2"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_group_forbidden"
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": ["Other_X"],
                                                      "link": {"value": "N"}}, headers=_h(t["token"], "f3"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "group_not_allowed"
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups]},
                     headers=_h(t["token"], "f4"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "link_required"
    r = await c.post("/aicm/v1/users/provision", json={"email": 'a"b@x.example', "groups": [groups],
                                                      "link": {"value": "N"}}, headers=_h(t["token"], "f5"))
    assert r.status_code == 422
    stub.assert_no_pending_responses()

    # 두 네이티브 사용자 → 409
    stub.add_response("list_users", {"Users": [_cu("a", "s1", email), _cu("b", "s2", email)]}, flt)
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups], "link": {"value": "N"}},
                     headers=_h(t["token"], "f6"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "ambiguous_cognito_users"

    # 연결 충돌 → 409 link_conflict
    stub.add_response("list_users", {"Users": [_cu("native-1", sub, email, groups=groups)]}, flt)
    _stub_user_pool_groups(stub, "native-1")
    stub.add_client_error("admin_link_provider_for_user", service_error_code="AliasExistsException",
                          http_status_code=400)
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups], "link": {"value": "N"}},
                     headers=_h(t["token"], "f7"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "link_conflict"
    stub.assert_no_pending_responses()


async def test_provision_without_federation_creates_unlinked(env, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "COGNITO_FEDERATED_PROVIDER_NAME", "")
    c, stub = env["client"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email, sub = f"nl{uuid.uuid4().hex[:6]}@corp.example", str(uuid.uuid4())
    groups = f"Claude_Nl{uuid.uuid4().hex[:6]}"
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups], "link": {"value": "N"}},
                     headers=_h(t["token"], "u0"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "federation_not_configured"
    stub.add_response("list_users", {"Users": []})
    stub.add_response("admin_create_user", _created_user("uuid-nl", email, sub, groups))
    _stub_sync(stub, "uuid-nl", sub, email, groups=groups)
    r = await c.post("/aicm/v1/users/provision", json={"email": email, "groups": [groups]},
                     headers=_h(t["token"], "u1"))
    assert r.status_code == 200 and r.json()["state"] == "created_unlinked", r.text
    stub.assert_no_pending_responses()


async def test_concurrent_provision_same_email_creates_once(env):
    """두 연결에서 같은 이메일 동시 provision — 이메일 advisory lock 으로 AdminCreateUser 는 한 번."""
    c, stub = env["client"], env["stub"]
    t = await _issue(c, scopes=["users:provision"])
    email, sub = f"cc{uuid.uuid4().hex[:6]}@corp.example", str(uuid.uuid4())
    groups = f"Claude_Cc{uuid.uuid4().hex[:6]}"
    # 첫 요청: 없음 → 생성 → 연결 → sync. 둘째 요청(락 대기 후): 네이티브 발견·연결됨 → sync.
    stub.add_response("list_users", {"Users": []})
    stub.add_response("admin_create_user", _created_user("uuid-cc", email, sub, groups))
    stub.add_response("admin_link_provider_for_user", {})
    _stub_sync(stub, "uuid-cc", sub, email, groups=groups)
    ident = json.dumps([{"providerName": "ADFS", "userId": "N"}])
    stub.add_response("list_users", {"Users": [_cu("uuid-cc", sub, email, groups=groups, identities=ident)]})
    _stub_user_pool_groups(stub, "uuid-cc")
    _stub_sync(stub, "uuid-cc", sub, email, groups=groups)
    body = {"email": email, "groups": [groups], "link": {"value": "N"}}
    r1, r2 = await asyncio.gather(
        c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "cc-1")),
        c.post("/aicm/v1/users/provision", json=body, headers=_h(t["token"], "cc-2")),
    )
    assert {r1.status_code, r2.status_code} == {200}, (r1.text, r2.text)
    assert sorted([r1.json()["state"], r2.json()["state"]]) == ["created", "existing"]
    assert r1.json()["user_id"] == r2.json()["user_id"]
    stub.assert_no_pending_responses()


async def test_idempotency_rows_expire(env):
    c, sm = env["client"], env["sm"]
    t = await _issue(c)
    uid, _ = await _mk_user(sm, provider="sts")
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "old-1"))
    assert r.status_code == 200
    from sqlalchemy import text

    async with sm() as s:
        await s.execute(text("UPDATE auth.integration_idempotency_keys SET created_at = :t WHERE idem_key='old-1'"),
                        {"t": datetime.now(timezone.utc) - timedelta(days=8)})
        await s.commit()
    r = await c.post(f"/aicm/v1/users/{uid}/deactivate", json={"reason": "x"}, headers=_h(t["token"], "new-1"))
    assert r.status_code == 200
    assert not await _q(sm, "SELECT 1 FROM auth.integration_idempotency_keys WHERE idem_key='old-1'")



# ── option A admin mirror: scoped svc- 실 DB 검증 ────────────────────────────

MIRROR_SCOPES = [
    "users:read", "users:write", "users:sync", "budgets:read", "budgets:write",
    "models:read", "tokens:read", "tokens:rotate",
]


async def _issue_mirror(client, scopes=None, **kw) -> dict:
    body = {"name": "aicm-mirror", "scopes": scopes or MIRROR_SCOPES, **kw}
    r = await client.post("/aicm/v1/service-tokens", json=body, headers=HUMAN)
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith("svc-")
    return r.json()


async def test_mirror_svc_token_auth_matrix_and_real_admin_rejection(env):
    c = env["client"]
    t = await _issue_mirror(c)
    h = {"Authorization": f"Bearer {t['token']}", "X-Forwarded-For": "10.0.0.3"}

    r = await c.get("/aicm/v1/admin/service-tokens", headers=h)
    assert r.status_code == 200, r.text
    # 같은 scoped svc- 는 real /admin 표면에서 무효다(401).
    r = await c.get("/admin/service-tokens", headers=h)
    assert r.status_code == 401, r.text
    # 사람 JWT/무토큰은 mirror 에서 인증되지 않는다.
    assert (await c.get("/aicm/v1/admin/service-tokens", headers=HUMAN)).status_code == 401
    assert (await c.get("/aicm/v1/admin/service-tokens")).status_code == 401
    # allowlist 밖 admin 경로는 mount 자체가 없다.
    assert (await c.get("/aicm/v1/admin/rate-limits", headers=h)).status_code == 404


async def test_mirror_scoped_rotate_preserves_svc_prefix_and_integration_scope(env):
    c, sm = env["client"], env["sm"]
    t = await _issue_mirror(c, scopes=["tokens:read", "tokens:rotate"])
    old = t["token"]
    r = await c.post("/aicm/v1/admin/service-tokens/rotate", headers={
        "Authorization": f"Bearer {old}", "X-Forwarded-For": "10.0.0.3",
    })
    assert r.status_code == 200, r.text
    new = r.json()["token"]
    assert new.startswith("svc-")
    rows = await _q(sm, "SELECT token_id, scopes FROM auth.integration_tokens ORDER BY created_at DESC LIMIT 1")
    assert rows and set(rows[0].scopes) == {"tokens:read", "tokens:rotate"}
    r = await c.get("/aicm/v1/admin/service-tokens", headers={
        "Authorization": f"Bearer {new}", "X-Forwarded-For": "10.0.0.3",
    })
    assert r.status_code == 200, r.text


async def test_mirror_allowed_clients_write_and_admin_target_refusal(env):
    c, sm = env["client"], env["sm"]
    t = await _issue_mirror(c)
    h = {"Authorization": f"Bearer {t['token']}", "X-Forwarded-For": "10.0.0.3"}
    uid, _ = await _mk_user(sm, provider="sts", keys=1)
    env["redis"].deleted.clear()
    r = await c.put(f"/aicm/v1/admin/users/{uid}/allowed-clients", json={"clients": ["claude-code"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["clients"] == ["claude-code"]
    # admin UI 경로와 같은 post-commit VK auth cache invalidation 이 일어난다.
    assert any(k.startswith("key:cache:vk:") for k in env["redis"].deleted), env["redis"].deleted
    # real /admin 과 같은 스키마/본문이다.
    r2 = await c.get(f"/aicm/v1/admin/users/{uid}/allowed-clients", headers=h)
    assert r2.status_code == 200 and r2.json() == {"user_id": str(uid), "clients": ["claude-code"]}

    # ADMIN/system/service-account 대상 mutation 은 integration 에게 403 + audit.
    admin_id = HUMAN_ADMIN
    r = await c.put(f"/aicm/v1/admin/users/{admin_id}/allowed-clients", json={"clients": ["claude-code"]}, headers=h)
    assert r.status_code == 403 and ("target_is_admin" in r.text or "target_is_system_user" in r.text)
    # refusal 은 rollback 에 같이 버려지면 안 되므로 queue_only audit 다. 이 테스트 앱은
    # lifespan 밖이라 consumer 를 띄우지 않고 큐 내용을 직접 본다(운영 main 은 consumer 가 flush).
    from app.core.audit import audit_logger
    entries = []
    while not audit_logger._queue.empty():
        entries.append(audit_logger._queue.get_nowait())
    assert any(e["action"] == "AICM_ADMIN_TARGET_REFUSED" and e["resource_id"] == str(admin_id)
               and e["result"] == "FAILURE" for e in entries)


async def test_mirror_app_group_budget_crud_and_has_total_budget(env):
    c, sm = env["client"], env["sm"]
    t = await _issue_mirror(c)
    h = {"Authorization": f"Bearer {t['token']}", "X-Forwarded-For": "10.0.0.3"}
    uid, tid = await _mk_user(sm, provider="sts")
    # app-group 은 advisory 제어plane — 부모 총액 없이도 저장된다.
    r = await c.put(
        f"/aicm/v1/admin/budgets/user/{uid}/app-group/ide",
        json={"max_budget_usd": "12.3456"}, headers=h,
    )
    assert r.status_code == 200, r.text
    r = await c.get(f"/aicm/v1/admin/budgets/user/{uid}/app-groups", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["app_groups"][0]["group"] == "ide"
    assert r.json()["app_groups"][0]["max_budget_usd"] in ("12.3456", 12.3456)
    r = await c.get(f"/aicm/v1/admin/budgets/user/{uid}/apps", headers=h)
    assert r.status_code == 200 and r.json()["has_total_budget"] is False
    r = await c.delete(f"/aicm/v1/admin/budgets/user/{uid}/app-group/ide", headers=h)
    assert r.status_code == 204, r.text


async def test_mirror_team_unlimited_and_four_decimal_budget(env):
    c, sm = env["client"], env["sm"]
    t = await _issue_mirror(c)
    h = {"Authorization": f"Bearer {t['token']}", "X-Forwarded-For": "10.0.0.3"}
    tid = await _mk_team(sm, f"Mir{uuid.uuid4().hex[:6]}")
    r = await c.put(
        f"/aicm/v1/admin/budgets/team/{tid}",
        json={"max_budget_usd": "unlimited", "confirm": True}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert any(k == f"budget:config:team:{{{tid}}}" for k in env["redis"].deleted), env["redis"].deleted
    rows = await _q(sm, "SELECT max_budget_usd FROM budget.budget_configs WHERE scope='TEAM' AND scope_id=:i", i=tid)
    assert str(rows[0].max_budget_usd) == "99999999.9999"

    uid, _ = await _mk_user(sm, provider="sts", team_id=tid)
    r = await c.put(
        f"/aicm/v1/admin/budgets/user/{uid}",
        json={"max_budget_usd": "10.0001", "confirm": True}, headers=h,
    )
    assert r.status_code == 200, r.text



async def test_mirror_read_endpoints_summary_team_users_usage_and_models(env):
    c, sm = env["client"], env["sm"]
    t = await _issue_mirror(c)
    h = {"Authorization": f"Bearer {t['token']}", "X-Forwarded-For": "10.0.0.3"}
    tid = await _mk_team(sm, f"Read{uuid.uuid4().hex[:6]}")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    for path in (
        f"/aicm/v1/admin/budgets/summary?period={period}&scope=TEAM",
        f"/aicm/v1/admin/budgets/summary/team/{tid}/users?period={period}&limit=10000",
        f"/aicm/v1/admin/budgets/usage-by-app?period={period}",
        "/aicm/v1/admin/models",
    ):
        r = await c.get(path, headers=h)
        assert r.status_code == 200, (path, r.text)
