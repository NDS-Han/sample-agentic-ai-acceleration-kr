# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sync_all 상호배제 advisory lock 회귀.

이전 버그: ``session.get_bind()`` 는 AsyncSession 에서 항상 **sync**
Engine/Connection(sync_session.get_bind 프록시)을 돌려준다. 구현은
AsyncConnection isinstance 분기 + ``bind.connect()`` 를 await 해서 sync
Connection 의 execute 는 await 불가 → 매번 ``advisory_lock_unavailable``
예외 경로로 빠져 **락 없이 sync 가 실행**됐다. 이 테스트는 엔진 바인딩
경로가 AsyncEngine 래퍼로 전용 커넥션에서 xact 락을 실제로 획득하는지,
경합 시 두 번째 sync 가 거부되는지를 본다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Engine

from app.services.cognito_sync_service import CognitoSyncService, SyncResult


def _settings(monkeypatch):
    s = MagicMock()
    s.COGNITO_USER_POOL_ID = "pool-1"
    s.OIDC_PROVIDER_NAME = "cognito"
    s.OIDC_GROUP_PREFIX = "Claude_"
    s.DEFAULT_TEAM_ID = str(uuid.uuid4())
    s.DEFAULT_DEPT_ID = str(uuid.uuid4())
    s.ADMIN_EMAILS = []
    s.ADMIN_GROUPS = []
    s.COGNITO_SYNC_DEACTIVATE_MISSING = False
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "get_settings", lambda: s)
    return s


def _async_cm(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _engine_bound_session():
    """get_bind 가 sync Engine 을 돌려주는 AsyncSession double."""
    session = AsyncMock()
    session.get_bind = MagicMock(return_value=MagicMock(spec=Engine))
    return session


def _lock_conn(acquired: bool):
    """pg_try_advisory_xact_lock 의 scalar() 결과를 주는 커넥션 double."""
    lock_conn = MagicMock()
    lock_conn.begin = MagicMock(return_value=_async_cm(MagicMock()))
    res = MagicMock()
    res.scalar = MagicMock(return_value=acquired)
    lock_conn.execute = AsyncMock(return_value=res)
    return lock_conn


@pytest.mark.asyncio
async def test_engine_bound_session_acquires_dedicated_xact_lock(monkeypatch):
    """엔진 바인딩 → AsyncEngine(sync_engine) 래퍼의 전용 커넥션에서 xact 락."""
    _settings(monkeypatch)
    session = _engine_bound_session()
    lock_conn = _lock_conn(acquired=True)

    ae = MagicMock()
    ae.connect = MagicMock(return_value=_async_cm(lock_conn))
    sync_result = SyncResult()

    svc = CognitoSyncService(MagicMock())
    svc._sync_all_locked = AsyncMock(return_value=sync_result)

    with patch(
        "sqlalchemy.ext.asyncio.AsyncEngine", return_value=ae
    ) as ae_cls:
        result = await svc.sync_all(session)

    # AsyncEngine 이 sync engine 으로 래핑됐고, 전용 커넥션+tx를 열어 락을 잡았다.
    ae_cls.assert_called_once_with(session.get_bind())
    ae.connect.assert_called_once()
    lock_conn.execute.assert_awaited_once()
    svc._sync_all_locked.assert_awaited_once()
    assert result is sync_result


@pytest.mark.asyncio
async def test_concurrent_sync_refused_when_lock_held(monkeypatch):
    """락을 못 얻으면 sync 를 실행하지 않고 에러를 남긴다."""
    _settings(monkeypatch)
    session = _engine_bound_session()
    lock_conn = _lock_conn(acquired=False)

    ae = MagicMock()
    ae.connect = MagicMock(return_value=_async_cm(lock_conn))

    svc = CognitoSyncService(MagicMock())
    svc._sync_all_locked = AsyncMock()

    with patch("sqlalchemy.ext.asyncio.AsyncEngine", return_value=ae):
        result = await svc.sync_all(session)

    svc._sync_all_locked.assert_not_called()
    assert any("already in progress" in e for e in result.errors)


@pytest.mark.asyncio
async def test_conn_bound_session_uses_session_level_lock(monkeypatch):
    """커넥션 바인딩(테스트 격리 패턴) → 세션-레벨 advisory lock."""
    _settings(monkeypatch)
    from sqlalchemy import Connection

    session = AsyncMock()
    session.get_bind = MagicMock(return_value=MagicMock(spec=Connection))
    res = MagicMock()
    res.scalar = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=res)
    # rollback/unlock 정리 경로
    session.rollback = AsyncMock()

    sync_result = SyncResult()
    svc = CognitoSyncService(MagicMock())
    svc._sync_all_locked = AsyncMock(return_value=sync_result)

    result = await svc.sync_all(session)

    # 세션-레벨 락(pg_try_advisory_lock)으로 조회했고 sync 를 실행했다.
    executed_sql = session.execute.await_args_list[0][0][0].text
    assert "pg_try_advisory_lock" in executed_sql
    svc._sync_all_locked.assert_awaited_once()
    assert result is sync_result
