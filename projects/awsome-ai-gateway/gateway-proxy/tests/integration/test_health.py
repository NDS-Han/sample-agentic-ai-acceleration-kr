# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.degradation.manager import FAIL_THRESHOLD, DegradationManager
from app.schemas.domain import DegradationLevel


def make_app_with_dm(dm: DegradationManager):
    """테스트용 최소 앱 생성 (lifespan 없이)."""
    from fastapi import FastAPI
    from app.routers import health

    app = FastAPI()
    app.include_router(health.router)
    app.state.degradation_manager = dm
    return app


def test_health_healthy():
    dm = DegradationManager()
    app = make_app_with_dm(dm)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_health_db_degraded():
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    app = make_app_with_dm(dm)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert resp.json()["components"]["postgresql"]["status"] == "down"


def test_health_both_degraded():
    # Liveness 는 의존성 장애에도 200 — 재시작으로 고칠 수 없는 양중 장애에서
    # 503 을 내면 kubelet 이 전 파드를 재시작해 스풀 유실 + crashloop 가 된다.
    # 열화 보고는 본문 status/components 로, 트래픽 배제는 /health/ready 로.
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    app = make_app_with_dm(dm)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unhealthy"
    assert resp.json()["degradation_level"] == "BOTH_DEGRADED"
