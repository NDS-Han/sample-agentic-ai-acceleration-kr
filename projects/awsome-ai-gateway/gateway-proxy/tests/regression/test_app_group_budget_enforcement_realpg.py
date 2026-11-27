# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""실 Postgres 에서 app-group/per-app min() 정책 합성 검증.

트랜잭션 롤백으로 공용 DB 를 오염시키지 않는다. 핵심은 ORM/UNION/SQL 타입이 실제
Postgres 에서 값을 잃지 않고, group-only·0·DELETE 의 유효 한도가 기존 per-app Redis
키 형상으로 재생성되는지 보는 것이다.

실행: PROOF_DSN=postgresql+asyncpg://... pytest 이 파일
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import redis.asyncio as aioredis

PROOF_DSN = os.environ.get("PROOF_DSN")
PROOF_REDIS_URL = os.environ.get("PROOF_REDIS_URL")
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


@pytest.mark.asyncio
@pytest.mark.skipif(not PROOF_REDIS_URL, reason="PROOF_REDIS_URL 미설정 — 실 Redis 필요")
async def test_rollout_targeted_invalidation_restores_hard_cap_and_preserves_data():
    """구형 단일정책 Redis 키 → targeted invalidation → 일반 재수화.

    운영 스크립트의 dry-run/apply 를 실제 subprocess 로 돌린다. 구형 캐시
    ($5 THROTTLE 단일 행)는 $10 HARD_BLOCK 이 있는데도 used=10 을 통과시키고,
    invalidation 뒤 gateway 의 일반 ``_hydrate_client_config_cache`` 재수화는
    layers 를 복원해 hard cap 에서 차단해야 한다. spend/reservation/다른 client 키는
    값이 그대로여야 한다.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.budget_service import BudgetService

    script = (
        Path(__file__).resolve().parents[3]
        / "docs" / "us-llm-gateway" / "update-scripts"
        / "23-invalidate-app-budget-config-cache.py"
    )
    check_lua = (
        Path(__file__).resolve().parents[2]
        / "src" / "app" / "redis_scripts" / "budget_check.lua"
    ).read_text(encoding="utf-8")

    uid = str(uuid.uuid4())
    period = "2026-11"
    usage_key = f"budget:user:{{{uid}}}:claude-code:{period}"
    config_key = f"budget:config:user:{{{uid}}}:claude-code"
    cowork_config_key = f"budget:config:user:{{{uid}}}:cowork"
    codex_config_key = f"budget:config:user:{{{uid}}}:codex"
    negative_key = f"budget:config:user:{{{uid}}}:claude-code:none"
    malformed_key = "budget:config:user:{not-a-uuid}:claude-code"
    marker_key = f"budget:pending:client:{{{uid}}}:claude-code:rollout-proof"
    resvsum_key = f"budget:resvsum:user:{{{uid}}}:claude-code:{period}"
    idx_key = f"budget:pendingidx:user:{{{uid}}}"
    idx_field = "claude-code:rollout-proof"
    owned_keys = [
        usage_key, config_key, cowork_config_key, codex_config_key, negative_key,
        malformed_key, marker_key, resvsum_key, idx_key,
    ]

    redis = aioredis.from_url(PROOF_REDIS_URL, decode_responses=True)
    engine = create_async_engine(PROOF_DSN)
    service = BudgetService()

    async def run_operator_script(*args):
        env = dict(os.environ)
        env["REDIS_URL"] = PROOF_REDIS_URL
        env["REDIS_CLUSTER_MODE"] = "false"
        env["DB_URL"] = PROOF_DSN
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    try:
        # 구 writer 가 남긴 단일 min() 정책: $5 THROTTLE 이 $10 HARD_BLOCK 을 가린다.
        legacy = {"limit_usd": "5.0000", "policy": "throttle", "thresholds": [80, 90, 100]}
        await redis.set(config_key, json.dumps(legacy), ex=300)
        await redis.set(
            cowork_config_key,
            json.dumps({"limit_usd": "8.0000", "policy": "hard_block", "thresholds": [80]}),
            ex=300,
        )
        # exact-shape 검증용 non-target decoy + 보존 검증용 spend/reservation 데이터.
        await redis.set(codex_config_key, "keep-codex", ex=300)
        await redis.set(negative_key, "1", ex=300)
        await redis.set(malformed_key, "keep-malformed", ex=300)
        await redis.set(usage_key, "10")
        await redis.set(marker_key, "1.25", ex=14400)
        await redis.set(resvsum_key, "1.25")
        await redis.hset(idx_key, idx_field, "1.25")

        legacy_result = json.loads(
            await redis.eval(check_lua, 2, usage_key, config_key, "client", "")
        )
        assert legacy_result["allowed"] is True
        assert legacy_result["policy"] == "throttle"

        dry = await run_operator_script("--dry-run")
        assert dry.returncode == 0, dry.stderr + dry.stdout
        dry_events = [json.loads(line) for line in dry.stdout.splitlines() if line.startswith("{")]
        dry_summary = next(e for e in dry_events if e.get("event") == "dry_run")
        assert dry_summary["mode"] == "standalone"
        assert dry_summary["primary_count"] == 1
        assert dry_summary["selected"] == 2
        assert dry_summary["deleted"] == 0
        assert dry_summary["clients"]["claude-code"]["selected"] == 1
        assert dry_summary["clients"]["cowork"]["selected"] == 1
        assert dry_summary["clients"]["claude-code"]["invalid_shape"] == 1
        assert dry_summary["clients"]["claude-code"]["pttl_min_ms"] > 0
        assert await redis.exists(config_key) == 1  # dry-run 은 쓰지 않는다

        applied = await run_operator_script("--apply")
        assert applied.returncode == 0, applied.stderr + applied.stdout
        applied_events = [
            json.loads(line) for line in applied.stdout.splitlines() if line.startswith("{")
        ]
        applied_summary = next(e for e in applied_events if e.get("event") == "applied")
        assert applied_summary["selected"] == 2
        assert applied_summary["deleted"] == 2
        assert await redis.exists(config_key) == 0
        assert await redis.exists(cowork_config_key) == 0

        # invalidation 은 config 키만 지운다 — spend/reservation/unrelated/negative/malformed 보존.
        assert await redis.get(usage_key) == "10"
        assert await redis.get(marker_key) == "1.25"
        assert await redis.get(resvsum_key) == "1.25"
        assert await redis.hget(idx_key, idx_field) == "1.25"
        assert await redis.get(codex_config_key) == "keep-codex"
        assert await redis.get(negative_key) == "1"
        assert await redis.get(malformed_key) == "keep-malformed"

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await _insert_user(session, uid)
            await _insert_total(session, uid)
            await _insert_app(session, uid, 10)  # HARD_BLOCK $10
            await _insert_group(session, uid, 5, policy="THROTTLE")
            await session.flush()

            # cache miss 에서 production 이 부르는 일반 재수화 경로.
            await service._hydrate_client_config_cache(redis, session, uid, "claude-code")

        hydrated = json.loads(await redis.get(config_key))
        assert {layer["source"] for layer in hydrated["layers"]} == {"app", "group"}
        assert {layer["source"]: layer["policy"] for layer in hydrated["layers"]} == {
            "app": "hard_block", "group": "throttle",
        }
        ttl = await redis.ttl(config_key)
        assert 0 < ttl <= 300

        restored = json.loads(await redis.eval(check_lua, 2, usage_key, config_key, "client", ""))
        assert restored["allowed"] is False
        assert restored["reason"] == "client_budget_exceeded"

        # 재수화도 usage/pending 데이터를 건드리지 않는다.
        assert await redis.get(usage_key) == "10"
        assert await redis.get(marker_key) == "1.25"
        assert await redis.get(resvsum_key) == "1.25"
        assert await redis.hget(idx_key, idx_field) == "1.25"
    finally:
        for key in owned_keys:
            await redis.delete(key)
        await redis.aclose()
        await engine.dispose()
