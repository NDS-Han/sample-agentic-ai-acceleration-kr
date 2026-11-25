# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""service token self-rotate 수명주기 — 실 PostgreSQL + 실 라우터/인증 경로.

PROOF_DSN 이 있을 때만 돈다. 자기 소유 DB(<dbname>_strotproof)를 만들고 db/init SQL 을 적용한다.
HTTP 는 실제 service_tokens 라우터 + get_current_user(svc 분기는 실제 DB 해시 조회) +
CommittingRoute 를 탄다. 사람 JWT 만 대역 검증기로 해석한다.

검증 축:
  * svc 토큰 self-rotate 200, 사람 JWT 403 (R32 회귀 — 게이트가 사람 전용이면 아무도 못 부름)
  * 새 토큰: 같은 이름·발급자(created_by)·계보(rotated_from), 수명 ≤ 구 토큰, 같은 권한 표면
  * 구 토큰: grace(24h) 동안 인증되고, grace 가 지나면 401
  * grace 중/폐기된 토큰은 다시 rotate 불가(계보 분기 없음)
  * 동시 rotate 두 번 → 후속 토큰 정확히 하나 (FOR UPDATE — 두 연결로 실측)
  * DELETE 계보 연쇄 폐기 / end-grace 는 문서대로
  * 감사 행 SERVICE_TOKEN_ROTATE + 행위 토큰 id
  * 계보당 rotate 빈도 상한 → 429 + Retry-After, end-grace 로 자리가 나면 다시 허용
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_strotproof"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"
HUMAN_ADMIN = uuid.UUID("00000000-0000-4000-a000-000000000010")  # 03_seed_data 의 dev admin
SVC_USER = uuid.UUID("00000000-0000-4000-a000-000000000011")

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
            except Exception as e:  # noqa: BLE001 — pgvector 없는 이미지 등
                print(f"  [init] {f.name}: {type(e).__name__}: {e}")
        # 사람 admin JWT 의 sub 가 created_by FK 를 만족해야 한다.
        assert await conn.fetchval("SELECT count(*) FROM auth.users WHERE id = $1", HUMAN_ADMIN) == 1
        assert await conn.fetchval("SELECT count(*) FROM auth.users WHERE id = $1", SVC_USER) == 1
    finally:
        await conn.close()
    yield dsn
    await _admin_exec(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)')


@pytest.fixture
async def env(owned_dsn, monkeypatch):
    """(client, sessionmaker) — 라우터·인증이 이 DB 를 쓰도록 AsyncSessionLocal 을 바꾼다."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.auth import JWTVerifier
    from app.core.db import install_commit_before_response
    from app.main import register_exception_handlers
    from app.routers import service_tokens
    from app.services.service_token_service import ServiceTokenService

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

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(service_tokens.router)
    install_commit_before_response(app)
    app.state.jwt_verifier = _JWT()
    app.state.service_token_service = ServiceTokenService()

    async with sm() as s:
        await s.execute(text("DELETE FROM audit.audit_logs WHERE resource_type = 'ServiceToken'"))
        await s.execute(text("UPDATE auth.service_tokens SET rotated_from = NULL"))
        await s.execute(text("DELETE FROM auth.service_tokens"))
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, sm
    await engine.dispose()


HUMAN = {"Authorization": "Bearer human-admin"}


def _bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _issue(client, name="aicm", expiry_days=60) -> dict:
    r = await client.post("/admin/service-tokens", json={"name": name, "expiry_days": expiry_days}, headers=HUMAN)
    assert r.status_code == 200, r.text
    return r.json()


async def _rotate(client, raw: str):
    return await client.post("/admin/service-tokens/rotate", headers=_bearer(raw))


async def _authed(client, raw: str) -> int:
    return (await client.get("/admin/service-tokens", headers=_bearer(raw))).status_code


async def _row(sm, tid: str):
    from sqlalchemy import text

    async with sm() as s:
        return (await s.execute(
            text("SELECT * FROM auth.service_tokens WHERE id = :i"), {"i": tid}
        )).mappings().one()


async def _set_revoked(sm, tid: str, at: datetime) -> None:
    from sqlalchemy import text

    async with sm() as s:
        await s.execute(text("UPDATE auth.service_tokens SET revoked_at = :r WHERE id = :i"), {"r": at, "i": tid})
        await s.commit()


async def test_self_rotate_lifecycle(env):
    client, sm = env
    old = await _issue(client)
    old_row = await _row(sm, old["id"])

    # 사람 admin 은 403 — rotate 대상이 될 svc 신원이 없다
    r = await client.post("/admin/service-tokens/rotate", headers=HUMAN)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["type"] == "forbidden"

    t0 = datetime.now(timezone.utc)
    r = await _rotate(client, old["token"])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["token"].startswith("svc-") and new["token"] != old["token"]
    assert new["name"] == old["name"]

    new_row = await _row(sm, new["id"])
    assert str(new_row["rotated_from"]) == old["id"]
    assert new_row["created_by"] == old_row["created_by"] == HUMAN_ADMIN
    assert new_row["expires_at"] <= old_row["expires_at"]
    assert new_row["revoked_at"] is None

    # 구 토큰: grace 24h 로 표시, 그동안 인증 유지
    old_row2 = await _row(sm, old["id"])
    grace = old_row2["revoked_at"]
    assert t0 + timedelta(hours=23, minutes=59) < grace <= datetime.now(timezone.utc) + timedelta(hours=24)
    assert await _authed(client, old["token"]) == 200
    assert await _authed(client, new["token"]) == 200

    # 같은 권한 표면 — 새 토큰도 svc 라 사람 전용 mutation 은 403
    r = await client.post("/admin/service-tokens", json={"name": "x"}, headers=_bearer(new["token"]))
    assert r.status_code == 403

    # list 가 계보를 보여 준다
    items = {i["id"]: i for i in (await client.get("/admin/service-tokens", headers=HUMAN)).json()["items"]}
    assert items[new["id"]]["rotated_from"] == old["id"]
    assert items[old["id"]]["revoked_at"] is not None

    # grace 중인 구 토큰은 다시 rotate 불가 → 분기 없음
    r = await _rotate(client, old["token"])
    assert r.status_code == 400
    assert "successor" in r.json()["error"]["message"]

    # grace 경과(24h 를 DB 시각으로 당김) → 구 토큰 401, 새 토큰은 계속 200
    await _set_revoked(sm, old["id"], datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await _authed(client, old["token"]) == 401
    assert (await _rotate(client, old["token"])).status_code == 401
    assert await _authed(client, new["token"]) == 200

    from sqlalchemy import text

    async with sm() as s:
        n_children = (await s.execute(
            text("SELECT count(*) FROM auth.service_tokens WHERE rotated_from = :i"), {"i": old["id"]}
        )).scalar_one()
        audit = (await s.execute(text(
            "SELECT actor_user_id, actor_role, resource_id, changes FROM audit.audit_logs "
            "WHERE action = 'SERVICE_TOKEN_ROTATE'"
        ))).mappings().all()
    assert n_children == 1
    assert len(audit) == 1
    a = audit[0]
    assert a["actor_user_id"] == SVC_USER and a["actor_role"] == "ADMIN"
    assert a["resource_id"] == new["id"]
    assert a["changes"]["_service_token_id"] == old["id"]
    assert a["changes"]["before"]["token_id"] == old["id"]
    assert a["changes"]["after"]["token_id"] == new["id"]


async def test_revoked_token_cannot_rotate(env):
    client, sm = env
    tok = await _issue(client)
    r = await client.delete(f"/admin/service-tokens/{tok['id']}", headers=HUMAN)
    assert r.status_code == 204
    assert (await _rotate(client, tok["token"])).status_code == 401  # 폐기 즉시 인증 불가


async def test_concurrent_double_rotate_http_yields_one_successor(env):
    """같은 토큰으로 동시에 rotate 10번 — 200 은 정확히 하나, 나머지 400, 후속 행 1개."""
    client, sm = env
    tok = await _issue(client)
    results = await asyncio.gather(*[_rotate(client, tok["token"]) for _ in range(10)])
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1, codes
    assert set(codes) <= {200, 400}, [r.text for r in results]

    from sqlalchemy import text

    async with sm() as s:
        n = (await s.execute(
            text("SELECT count(*) FROM auth.service_tokens WHERE rotated_from = :i"), {"i": tok["id"]}
        )).scalar_one()
        n_audit = (await s.execute(
            text("SELECT count(*) FROM audit.audit_logs WHERE action = 'SERVICE_TOKEN_ROTATE'")
        )).scalar_one()
    assert n == 1
    assert n_audit == 1


async def test_concurrent_rotate_second_blocks_on_row_lock(env):
    """서비스 계층 결정적 증명: 두 연결 — A 가 rotate 후 커밋 전이면 B 는 행 락에서 대기,
    A 커밋 뒤 B 는 최신 revoked_at 을 보고 거부된다."""
    client, sm = env
    tok = await _issue(client)
    from app.core.exceptions import ValidationError
    from app.services.service_token_service import ServiceTokenService

    svc = ServiceTokenService()
    tid = uuid.UUID(tok["id"])
    async with sm() as a, sm() as b:
        await svc.rotate(a, token_id=tid, created_by=SVC_USER)  # 락 보유, 미커밋
        task = asyncio.create_task(svc.rotate(b, token_id=tid, created_by=SVC_USER))
        await asyncio.sleep(0.5)
        assert not task.done(), "B must wait on A's FOR UPDATE lock"
        await a.commit()
        with pytest.raises(ValidationError, match="successor"):
            await task
        await b.rollback()

    from sqlalchemy import text

    async with sm() as s:
        n = (await s.execute(
            text("SELECT count(*) FROM auth.service_tokens WHERE rotated_from = :i"), {"i": tok["id"]}
        )).scalar_one()
    assert n == 1


async def test_lineage_delete_cascades_whole_chain(env):
    """DELETE 는 계보 사슬 전체(자손 + grace 조상)를 즉시 폐기한다."""
    client, sm = env
    t = await _issue(client)
    c = (await _rotate(client, t["token"])).json()
    d = (await _rotate(client, c["token"])).json()
    for raw in (t["token"], c["token"], d["token"]):
        assert await _authed(client, raw) == 200
    r = await client.delete(f"/admin/service-tokens/{c['id']}", headers=HUMAN)
    assert r.status_code == 204
    for raw in (t["token"], c["token"], d["token"]):
        assert await _authed(client, raw) == 401


async def test_end_grace_closes_old_only_and_frees_rate_limit(env):
    """end-grace: 구 토큰 grace 만 닫고 후속 토큰은 산다. 계보 rotate 상한(조상 grace 2)
    은 429 + Retry-After 이고, end-grace 로 자리가 나면 다시 허용된다."""
    client, sm = env
    t = await _issue(client)
    c = (await _rotate(client, t["token"])).json()
    d = (await _rotate(client, c["token"])).json()

    r = await _rotate(client, d["token"])  # 조상 t, c 가 grace — 상한
    assert r.status_code == 429, r.text
    assert int(r.headers["retry-after"]) > 23 * 3600
    assert r.json()["error"]["code"] == "service_token_rotate_rate_limited"
    assert await _authed(client, d["token"]) == 200  # 거부돼도 토큰은 그대로

    # 사람 전용 — svc 토큰은 end-grace 403
    assert (await client.post(f"/admin/service-tokens/{t['id']}/end-grace", headers=_bearer(d["token"]))).status_code == 403
    r = await client.post(f"/admin/service-tokens/{t['id']}/end-grace", headers=HUMAN)
    assert r.status_code == 200 and r.json() == {"grace_ended": True}
    assert await _authed(client, t["token"]) == 401
    assert await _authed(client, c["token"]) == 200  # 다른 조상·후속은 영향 없음
    assert await _authed(client, d["token"]) == 200

    r = await _rotate(client, d["token"])
    assert r.status_code == 200, r.text
    e = r.json()
    assert await _authed(client, e["token"]) == 200
    # 두 번째 end-grace 는 no-op
    r = await client.post(f"/admin/service-tokens/{t['id']}/end-grace", headers=HUMAN)
    assert r.json() == {"grace_ended": False}
