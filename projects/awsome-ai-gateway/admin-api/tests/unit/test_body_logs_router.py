# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""body_logs 라우터 — 게이트/검증/감사 계약 단위 테스트 (S3·DB mock)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.body_logs import get_body_log
from app.services.body_log_store import (
    BodyLogFetchResult,
    BodyLogLookupError,
    BodyLogNotConfigured,
)

REQ = "023c673b-0686-4794-b8ac-8d1317118e7a"
NOW = datetime(2026, 11, 5, 8, 0, 0, tzinfo=timezone.utc)


def _request() -> MagicMock:
    req = MagicMock()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=True
    )
    req.client.host = "10.0.0.1"
    req.state.request_id = "t-1"
    req.headers = {}
    return req


def _admin() -> MagicMock:
    admin = MagicMock()
    admin.user_id = uuid.uuid4()
    admin.role.value = "ADMIN"
    return admin


def _session(row=None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.first.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


def _usage_row(requested_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        team_id=None,
        provider="BEDROCK",
        client="other",
        requested_at=requested_at or NOW - timedelta(minutes=10),
        latency_ms=1200,
        model_alias="claude-sonnet",
    )


@pytest.fixture
def settings():
    with patch("app.routers.body_logs.get_settings") as gs:
        gs.return_value.BODY_LOG_S3_BUCKET = "bucket"
        yield gs


@pytest.fixture
def audit():
    with patch("app.routers.body_logs.audit_logger") as a:
        a.log = AsyncMock()
        yield a


@pytest.fixture
def store_cls():
    with patch("app.routers.body_logs.BodyLogStore") as cls:
        cls.return_value.fetch = AsyncMock()
        yield cls


@pytest.mark.asyncio
async def test_toggle_off_403_no_audit(settings, audit):
    req = _request()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=False
    )
    with pytest.raises(HTTPException) as ei:
        await get_body_log(req, REQ, _admin(), _session(_usage_row()))
    assert ei.value.status_code == 403
    audit.log.assert_not_called()


@pytest.mark.asyncio
async def test_bad_request_id_400_no_audit(settings, audit):
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), "x/../etc", _admin(), _session(_usage_row()))
    assert ei.value.status_code == 400
    audit.log.assert_not_called()


@pytest.mark.asyncio
async def test_no_usage_row_404_audit_queue_only(settings, audit, store_cls):
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(None))
    assert ei.value.status_code == 404
    store_cls.assert_not_called()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert kwargs["action"] == "VIEW_BODY_LOG"
    assert kwargs["changes"]["found"] is False
    assert kwargs["result"] == "FAILURE"


@pytest.mark.asyncio
async def test_lookup_error_502_audit(settings, audit, store_cls):
    store_cls.return_value.fetch.side_effect = BodyLogLookupError("boom")
    with pytest.raises(HTTPException) as ei:
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    assert ei.value.status_code == 502
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert kwargs["changes"]["found"] is None
    assert "boom" in kwargs["changes"]["error"]


@pytest.mark.asyncio
async def test_not_configured_503_audit(settings, audit, store_cls):
    store_cls.return_value.fetch.side_effect = BodyLogNotConfigured("no bucket")
    with pytest.raises(HTTPException) as ei:
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    assert ei.value.status_code == 503
    assert audit.log.call_args.kwargs["queue_only"] is True


@pytest.mark.asyncio
async def test_unexpected_error_audited_then_raised(settings, audit, store_cls):
    """안전망 — 서비스가 못 잡은 예외도 열람 시도는 감사되고 예외는 올라간다."""
    store_cls.return_value.fetch.side_effect = RuntimeError("weird")
    with pytest.raises(RuntimeError):
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert "weird" in kwargs["changes"]["error"]


@pytest.mark.asyncio
async def test_success_200_audit_session_bound(settings, audit, store_cls):
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        record={"request_id": REQ, "request_body": {"messages": []}},
        source="firehose",
        key="k",
    )
    resp = await get_body_log(
        _request(), REQ, _admin(), _session(_usage_row())
    )
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is False
    assert kwargs["changes"]["found"] is True
    assert kwargs["result"] == "SUCCESS"
    assert kwargs["changes"]["subject_user_id"]


@pytest.mark.asyncio
async def test_not_found_recent_pending_message(settings, audit, store_cls):
    """수집 창(<3분) 안 404 는 '수집 대기' 메시지다."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult()
    recent = _usage_row(requested_at=NOW - timedelta(seconds=30))
    with patch("app.routers.body_logs.datetime") as dt:
        dt.now.return_value = NOW
        dt.side_effect = lambda *a, **k: datetime(*a, **k)
        with pytest.raises(HTTPException) as ei:
            await get_body_log(
                _request(), REQ, _admin(), _session(recent)
            )
    assert ei.value.status_code == 404
    assert "수집 대기" in ei.value.detail


@pytest.mark.asyncio
async def test_not_found_old_not_collected_message(settings, audit, store_cls):
    store_cls.return_value.fetch.return_value = BodyLogFetchResult()
    old = _usage_row(requested_at=NOW - timedelta(hours=2))
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(old))
    assert ei.value.status_code == 404
    assert "수집되지 않았습니다" in ei.value.detail


@pytest.mark.asyncio
async def test_not_found_incomplete_message(settings, audit, store_cls):
    """스캔 상한/파손이면 '미수집'이 아니라 '못 찾았을 수 있음' 메시지."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        incomplete=True
    )
    old = _usage_row(requested_at=NOW - timedelta(hours=2))
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(old))
    assert ei.value.status_code == 404
    assert "전부 확인하지 못했습니다" in ei.value.detail


@pytest.mark.asyncio
async def test_oversized_counts_as_found(settings, audit, store_cls):
    """oversized fallback 객체는 레코드 없이도 '존재함' — 200 + found=True."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        oversized=True, source="fallback", key="k"
    )
    resp = await get_body_log(
        _request(), REQ, _admin(), _session(_usage_row())
    )
    assert resp.status_code == 200
    assert audit.log.call_args.kwargs["changes"]["found"] is True
