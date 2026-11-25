# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""DB pool 설정 env/엔진 배선 단위 테스트.

계약 (swe-fix, rv-swe P1-2/P1-3/P2-4):
- pool_size ← DB_POOL_SIZE (공통 ConfigMap 또는 차트 <worker>.db.poolSize).
- max_overflow ← DB_POOL_OVERFLOW(서비스별) 만, 미설정이면 코드 기본 3.
  공통 DB_MAX_OVERFLOW 는 읽지 않는다 (prod overflow 불변 — swe-fix round 2).
- pool_timeout/pool_recycle ← WORKER_DB_POOL_TIMEOUT/WORKER_DB_POOL_RECYCLE 일 때만.
  공통 ConfigMap 의 DB_POOL_TIMEOUT/DB_POOL_RECYCLE 은 읽지 않는다 — 미설정이면
  SQLAlchemy 기본(30s, recycle 없음)으로 이 배선 이전과 같다.
"""
from __future__ import annotations

import pytest

from worker.config import Settings
from worker.db import create_db_engine

_POOL_ENV = (
    "DB_POOL_SIZE", "DB_POOL_OVERFLOW", "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE",
    "WORKER_DB_POOL_TIMEOUT", "WORKER_DB_POOL_RECYCLE",
)
# 차트 values-eks-fargate-prod.yaml 의 공통 ConfigMap (database.external) 값
_PROD_CONFIGMAP = {
    "DB_POOL_SIZE": "20", "DB_MAX_OVERFLOW": "30",
    "DB_POOL_TIMEOUT": "10", "DB_POOL_RECYCLE": "3600",
}


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    for k in _POOL_ENV:
        monkeypatch.delenv(k, raising=False)


def _capture_engine_kwargs(monkeypatch, settings: Settings) -> dict:
    captured: dict = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("worker.db.create_async_engine", fake_create_async_engine)
    create_db_engine(settings)
    return captured


def test_defaults_without_env(monkeypatch) -> None:
    kw = _capture_engine_kwargs(monkeypatch, Settings())
    assert kw["pool_size"] == 5
    assert kw["max_overflow"] == 3
    # 미설정이면 키 자체를 넘기지 않는다 → SQLAlchemy 기본
    assert "pool_timeout" not in kw
    assert "pool_recycle" not in kw


def test_per_service_overrides_are_wired(monkeypatch) -> None:
    """차트 <worker>.db.* 가 렌더하는 env 이름 4개가 모두 엔진까지 간다."""
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    monkeypatch.setenv("DB_POOL_OVERFLOW", "6")
    monkeypatch.setenv("WORKER_DB_POOL_TIMEOUT", "7")
    monkeypatch.setenv("WORKER_DB_POOL_RECYCLE", "1200")
    # 공통 값은 서비스별 값에 진다
    monkeypatch.setenv("DB_MAX_OVERFLOW", "30")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "10")
    monkeypatch.setenv("DB_POOL_RECYCLE", "3600")

    kw = _capture_engine_kwargs(monkeypatch, Settings())

    assert kw["pool_size"] == 4
    assert kw["max_overflow"] == 6
    assert kw["pool_timeout"] == 7
    assert kw["pool_recycle"] == 1200


def test_overflow_zero_is_honoured(monkeypatch) -> None:
    """0 은 유효한 하드캡이다 — falsy 로 떨어져 공통/기본값이 되면 안 된다."""
    monkeypatch.setenv("DB_POOL_OVERFLOW", "0")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "30")
    assert _capture_engine_kwargs(monkeypatch, Settings())["max_overflow"] == 0


def test_common_max_overflow_is_ignored_without_override(monkeypatch) -> None:
    """round 2: 서비스별 override 가 없으면 공통 DB_MAX_OVERFLOW 가 있어도 코드 기본."""
    monkeypatch.setenv("DB_MAX_OVERFLOW", "12")
    assert _capture_engine_kwargs(monkeypatch, Settings())["max_overflow"] == 3


def test_prod_configmap_keeps_sqlalchemy_timeout_and_recycle(monkeypatch) -> None:
    """P1-2/round 2: prod 공통 ConfigMap 만 있는 pod (서비스별 override 없음) 에서
    새 이미지가 overflow/pool_timeout/pool_recycle 을 바꾸지 않는다."""
    for k, v in _PROD_CONFIGMAP.items():
        monkeypatch.setenv(k, v)
    kw = _capture_engine_kwargs(monkeypatch, Settings())
    assert kw["pool_size"] == 20
    assert kw["max_overflow"] == 3
    assert "pool_timeout" not in kw
    assert "pool_recycle" not in kw


def test_real_engine_pool_matches_pre_wiring_defaults(monkeypatch) -> None:
    """실제 SQLAlchemy 엔진의 풀 속성 — 연결은 만들지 않는다."""
    for k, v in _PROD_CONFIGMAP.items():
        monkeypatch.setenv(k, v)
    engine = create_db_engine(Settings())
    try:
        pool = engine.sync_engine.pool
        assert pool.size() == 20
        assert pool._max_overflow == 3
        assert pool._timeout == 30       # SQLAlchemy 기본
        assert pool._recycle == -1       # recycle 없음
    finally:
        engine.sync_engine.dispose()
