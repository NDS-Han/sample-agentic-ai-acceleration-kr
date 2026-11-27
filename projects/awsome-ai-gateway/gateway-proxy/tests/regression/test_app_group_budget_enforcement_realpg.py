# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""실 Postgres 에서 app-group/per-app min() 정책 합성 검증.

트랜잭션 롤백으로 공용 DB 를 오염시키지 않는다. 핵심은 ORM/UNION/SQL 타입이 실제
Postgres 에서 값을 잃지 않고, group-only·0·DELETE 의 유효 한도가 기존 per-app Redis
키 형상으로 재생성되는지 보는 것이다.

실행: PROOF_DSN=postgresql+asyncpg://... pytest 이 파일
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 Postgres 필요")


class _Redis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


async def _insert_user(session, user_id):
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO auth.users (id, email, display_name, role, sso_subject) "
            "VALUES (:id, :email, 'app-group-proof', 'DEVELOPER', :sub)"
        ),
        {"id": user_id, "email": f"{user_id}@example.invalid", "sub": user_id},
    )


async def _insert_total(session, user_id):
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO budget.budget_configs "
            "(scope, scope_id, client, max_budget_usd, policy, allocated_by, "
            "effective_from, is_active) "
            "VALUES ('USER', :uid, NULL, 100, 'HARD_BLOCK', :uid, DATE '2020-01-01', true)"
        ),
        {"uid": user_id},
    )


async def _insert_app(session, user_id, amount, client="claude-code"):
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO budget.budget_configs "
            "(scope, scope_id, client, max_budget_usd, policy, allocated_by, "
            "effective_from, is_active) "
            "VALUES ('USER', :uid, :client, :amount, 'HARD_BLOCK', :uid, DATE '2020-01-01', true)"
        ),
        {"uid": user_id, "client": client, "amount": amount},
    )


async def _insert_group(session, user_id, amount, group="claude-code", policy="HARD_BLOCK"):
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO budget.user_app_group_budgets "
            "(user_id, app_group, max_budget_usd, policy, allocated_by) "
            "VALUES (:uid, :group, :amount, :policy, :uid)"
        ),
        {"uid": user_id, "group": group, "amount": amount, "policy": policy},
    )


async def _new_user(session, *, total=True):
    uid = str(uuid.uuid4())
    await _insert_user(session, uid)
    if total:
        await _insert_total(session, uid)
    return uid


async def _hydrated_config(service, session, uid, client="claude-code"):
    redis = _Redis()
    await service._hydrate_client_config_cache(redis, session, uid, client)
    key = f"budget:config:user:{{{uid}}}:{client}"
    assert key in redis.data, f"{client} 유효 정책 키가 재수화되지 않았다"
    return json.loads(redis.data[key])


async def _hydrated_limit(service, session, uid, client="claude-code"):
    return (await _hydrated_config(service, session, uid, client))["limit_usd"]


@pytest.mark.asyncio
async def test_real_postgres_min_resolution_orders_one_sided_zero_and_delete():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.budget_service import BudgetService

    engine = create_async_engine(PROOF_DSN)
    service = BudgetService()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            # 커밋하지 않는다 — 같은 세션이 읽고 close 시 rollback 된다.
            cases = []

            uid = await _new_user(session)
            await _insert_app(session, uid, 10)
            await _insert_group(session, uid, 5)
            cases.append((uid, "5.0000", "app 먼저 → group 더 낮음"))

            mixed_uid = await _new_user(session)
            await _insert_app(session, mixed_uid, 10)
            await _insert_group(session, mixed_uid, 5, policy="THROTTLE")

            uid = await _new_user(session)
            await _insert_group(session, uid, 5)
            await _insert_app(session, uid, 10)
            cases.append((uid, "5.0000", "group 먼저 → group 더 낮음"))

            uid = await _new_user(session)
            await _insert_app(session, uid, 3)
            await _insert_group(session, uid, 5)
            cases.append((uid, "3.0000", "per-app 이 더 낮음"))

            uid = await _new_user(session)
            await _insert_app(session, uid, 4)
            cases.append((uid, "4.0000", "per-app 단독"))

            uid = await _new_user(session)
            await _insert_group(session, uid, 2)
            cases.append((uid, "2.0000", "app-group 단독"))

            uid = await _new_user(session)
            await _insert_app(session, uid, 10)
            await _insert_group(session, uid, 0)
            cases.append((uid, "0.0000", "PUT 0 revoke"))

            await session.flush()
            for uid, expected, label in cases:
                got = await _hydrated_limit(service, session, uid)
                assert got == expected, f"{label}: got {got}, want {expected}"

            mixed = await _hydrated_config(service, session, mixed_uid)
            layers = {row["source"]: row for row in mixed["layers"]}
            assert layers["app"]["policy"] == "hard_block"
            assert layers["group"]["policy"] == "throttle"
            assert mixed["limit_usd"] == "10.0000"  # 구버전 소비자용 hard 보수 폴백

            # DELETE 는 group 제약만 제거한다 — 독립 per-app cap 이 다시 적용된다.
            uid = await _new_user(session)
            await _insert_app(session, uid, 7)
            await _insert_group(session, uid, 2)
            await session.flush()
            assert await _hydrated_limit(service, session, uid) == "2.0000"
            from sqlalchemy import text

            await session.execute(
                text(
                    "DELETE FROM budget.user_app_group_budgets "
                    "WHERE user_id = :uid AND app_group = 'claude-code'"
                ),
                {"uid": uid},
            )
            await session.flush()
            assert await _hydrated_limit(service, session, uid) == "7.0000"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parent_app_clients_gate_includes_group_only_and_excludes_legacy_names():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.budget_service import BudgetService

    engine = create_async_engine(PROOF_DSN)
    service = BudgetService()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            uid = await _new_user(session)
            await _insert_group(session, uid, 2)
            legacy_uid = await _new_user(session)
            await _insert_group(session, legacy_uid, 2, group="ide")
            await session.flush()

            redis = _Redis()
            await service.ensure_config_cached(redis, session, uid)
            data = json.loads(redis.data[f"budget:config:user:{{{uid}}}"])
            assert data["app_clients"] == ["claude-code"]

            redis = _Redis()
            await service.ensure_config_cached(redis, session, legacy_uid)
            data = json.loads(redis.data[f"budget:config:user:{{{legacy_uid}}}"])
            assert data["app_clients"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_db_fallback_preserves_soft_block_erased_by_lower_throttle_shape():
    """REDIS_DEGRADED 폴백도 mixed rows 를 각각 평가한다 — soft cap 이 hard 보다 먼저다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.budget_service import BudgetService

    engine = create_async_engine(PROOF_DSN)
    service = BudgetService()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            uid = await _new_user(session, total=False)
            team_id = str(uuid.uuid4())
            await session.execute(text(
                "INSERT INTO budget.budget_configs "
                "(scope, scope_id, client, max_budget_usd, policy, allocated_by, "
                "effective_from, is_active) "
                "VALUES ('TEAM', :tid, NULL, 100, 'HARD_BLOCK', :uid, DATE '2020-01-01', true)"
            ), {"tid": team_id, "uid": uid})
            await _insert_app(session, uid, 10)
            await _insert_group(session, uid, 5, policy="SOFT_WARNING")
            await session.execute(text(
                "INSERT INTO budget.budget_usages "
                "(id, scope, scope_id, client, period, used_usd, limit_usd) "
                "VALUES (:id, 'USER', :uid, 'claude-code', '2026-10', 5.5, 10)"
            ), {"id": str(uuid.uuid4()), "uid": uid})
            await session.flush()
            with pytest.raises(PermissionError) as excinfo:
                await service._check_budget_db(
                    session, uid, team_id, "2026-10", client="claude-code"
                )
            assert str(excinfo.value) == "client_soft_limit_exceeded"
    finally:
        await engine.dispose()
