# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""DB pool 설정 env/엔진 배선 단위 테스트."""
from __future__ import annotations

from worker.config import Settings
from worker.db import create_db_engine


def test_db_pool_env_names(monkeypatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "5")
    monkeypatch.setenv("DB_POOL_OVERFLOW", "5")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "10")
    monkeypatch.setenv("DB_POOL_RECYCLE", "1500")

    settings = Settings()

    assert settings.db_pool_size == 5
    assert settings.db_pool_overflow == 5
    assert settings.db_pool_timeout == 10
    assert settings.db_pool_recycle == 1500


def test_create_db_engine_passes_pool_timeout_and_recycle(monkeypatch) -> None:
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("worker.db.create_async_engine", fake_create_async_engine)
    settings = Settings(db_pool_timeout=7, db_pool_recycle=1200)

    create_db_engine(settings)

    assert captured["pool_timeout"] == 7
    assert captured["pool_recycle"] == 1200
