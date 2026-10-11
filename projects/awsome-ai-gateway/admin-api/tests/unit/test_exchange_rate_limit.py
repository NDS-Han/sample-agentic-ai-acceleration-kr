# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""POST /v1/auth/exchange — per-IP 고정창 레이트리밋.

배경: 이 엔드포인트는 인증 없이 열려 있는데 호출당 비용이 크다(JWT 검증 +
JWKS 조회 가능 + 사용자/팀 upsert + VK 발급). IP 당 분당 횟수 상한을 두어
비용 증폭을 막는다. 계약:
  * _EXCHANGE_RPM_PER_IP 초과 시 429 {"error": {"type": "rate_limited"}}
  * XFF 가 있으면 가장 오른쪽 홉을 IP 로 쓴다(위조 불가 홉)
  * redis 가 None 이면 리밋을 건너뛴다(VK 매핑 저장 자체가 불가라 무의미)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db_session
from app.routers import auth_oidc


def _build_app(redis):
    from datetime import datetime, timezone

    app = FastAPI()
    app.include_router(auth_oidc.router)

    svc = MagicMock()
    svc.exchange_jwt_for_vk = AsyncMock(
        return_value={
            "virtual_key": "vk-x",
            "expires_at": datetime.now(timezone.utc),
            "gateway_endpoint": "https://gw",
            "otel_endpoint": "https://otel",
            "user_id": "u1",
        }
    )
    app.state.oidc_service = svc
    app.state.redis = redis

    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db_session] = _db
    return app


@pytest.mark.asyncio
async def test_exchange_rate_limited_after_threshold():
    """분당 한도 초과 시 JWT 검증 없이 429."""
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=11)  # 한도(10) 초과
    app = _build_app(redis)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/auth/exchange",
            json={},
            headers={"Authorization": "Bearer x.y.z"},
        )

    assert resp.status_code == 429
    assert resp.json()["detail"]["error"]["type"] == "rate_limited"
    app.state.oidc_service.exchange_jwt_for_vk.assert_not_awaited()


@pytest.mark.asyncio
async def test_exchange_under_limit_proceeds():
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    app = _build_app(redis)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/auth/exchange",
            json={},
            headers={"Authorization": "Bearer x.y.z"},
        )

    assert resp.status_code == 200
    redis.expire.assert_awaited()  # 창 첫 요청에 TTL 부여


@pytest.mark.asyncio
async def test_exchange_uses_rightmost_xff_for_limit_key():
    """XFF 오른쪽 홉이 리밋 키 — 왼쪽(위조 가능)으로 우회 불가."""
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    app = _build_app(redis)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        await client.post(
            "/v1/auth/exchange",
            json={},
            headers={
                "Authorization": "Bearer x.y.z",
                "x-forwarded-for": "1.2.3.4, 10.0.0.9",
            },
        )

    assert redis.incr.await_args.args[0] == "rl:auth_exchange:10.0.0.9"


@pytest.mark.asyncio
async def test_exchange_redis_none_skips_limit():
    app = _build_app(redis=None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/auth/exchange",
            json={},
            headers={"Authorization": "Bearer x.y.z"},
        )

    assert resp.status_code == 200
