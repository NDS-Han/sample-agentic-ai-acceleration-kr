# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the /health/ready readiness probe (견고성 6축검증 축⑤).

wave 1 (A1): readiness 는 **프로세스 로컬** 신호(기동 완료·종료 중 아님)만 본다. DB/Redis
열화와 풀 포화는 플릿 공통 상태라 판정에 쓰면 전 파드가 동시에 빠진다 — 본문으로 보고만
한다. /health(liveness)는 여전히 항상 200 이다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import health as health_router
from app.schemas.domain import DegradationLevel


def _build_app(level: DegradationLevel, *, checked_out=0, pool_size=20, max_overflow=10, engine=True):
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=level)
    app.state.started = True
    app.state.shutting_down = False
    if engine:
        pool = MagicMock()
        pool.checkedout.return_value = checked_out
        pool.size.return_value = pool_size
        pool.overflow.return_value = max_overflow
        pool._max_overflow = max_overflow
        app.state.db_engine = SimpleNamespace(pool=pool)
    return app


async def _get(app, path="/health/ready"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


@pytest.mark.asyncio
async def test_ready_when_healthy_and_pool_free():
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=3))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["pool_saturated"] is False


@pytest.mark.asyncio
async def test_ready_when_db_degraded():
    # wave 1 (A1): DB 열화는 플릿 공통 상태라 readiness 판정에 쓰지 않는다(보고만).
    resp = await _get(_build_app(DegradationLevel.DB_DEGRADED, checked_out=0))
    assert resp.status_code == 200
    assert resp.json()["db_degraded"] is True


@pytest.mark.asyncio
async def test_pool_saturation_is_reported_not_routed():
    # 풀 포화는 보고만 한다 — 메인 풀 포화가 인증 경로를 막지 않는다(인증은 전용 hot-path 풀).
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=30, pool_size=20, max_overflow=10))
    assert resp.status_code == 200
    assert resp.json()["pool_saturated"] is True


@pytest.mark.asyncio
async def test_ready_under_transient_load_below_hard_cap():
    # HPA min=1 안전: 상시 풀 초과(25/20+10)여도 hard_cap 미만이면 파드를 빼지 않는다
    # (바쁨≠고장). 진짜 지속 고갈은 DegradationLevel 게이트(HealthChecker)가 잡는다.
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=25, pool_size=20, max_overflow=10))
    assert resp.status_code == 200
    assert resp.json()["pool_saturated"] is False


@pytest.mark.asyncio
async def test_ready_when_pool_has_room():
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=19, pool_size=20, max_overflow=10))
    assert resp.status_code == 200
    assert resp.json()["pool_saturated"] is False


@pytest.mark.asyncio
async def test_both_degraded_still_ready():
    resp = await _get(_build_app(DegradationLevel.BOTH_DEGRADED))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_not_ready_while_starting_or_shutting_down():
    app = _build_app(DegradationLevel.HEALTHY)
    app.state.started = False
    assert (await _get(app)).status_code == 503
    app.state.started = True
    app.state.shutting_down = True
    resp = await _get(app)
    assert resp.status_code == 503 and resp.json()["status"] == "shutting_down"


@pytest.mark.asyncio
async def test_pool_introspection_failure_is_lenient():
    # pool 조회가 실패해도 readiness 를 떨어뜨리지 않는다(HEALTHY 이면 200).
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=DegradationLevel.HEALTHY)
    app.state.started = True
    bad_pool = MagicMock()
    bad_pool.checkedout.side_effect = RuntimeError("boom")
    app.state.db_engine = SimpleNamespace(pool=bad_pool)
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.json()["pool"]["error"] == "pool_introspection_failed"


@pytest.mark.asyncio
async def test_health_liveness_still_200_on_db_degraded():
    # /health(liveness)는 DB_DEGRADED 에 여전히 200(관대) — 재시작 cascade 회피 계약 유지.
    resp = await _get(_build_app(DegradationLevel.DB_DEGRADED), path="/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_liveness_still_200_on_both_degraded():
    # /health(liveness)는 BOTH_DEGRADED 에도 200 — 양중 장애는 플릿 전체 상태라
    # 503 을 내면 kubelet 이 전 파드를 재시작한다(스풀 유실 + HEALTHY 재탐지
    # 지연으로 crashloop). 열화 보고는 본문으로, 트래픽 배제는 readiness 가 담당.
    resp = await _get(_build_app(DegradationLevel.BOTH_DEGRADED), path="/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unhealthy"
