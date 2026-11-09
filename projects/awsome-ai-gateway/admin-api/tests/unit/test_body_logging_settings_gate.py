# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""body-logging 런타임 토글의 인프라 게이트 — BODY_LOG_S3_BUCKET 미설정 배포는
켜기를 403 거부한다(끄기는 항상 허용). GET 은 provisioned 를 내려 UI 잠금을
돕는다."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole
from app.routers.settings import get_body_logging, set_body_logging


def _settings(bucket: str) -> MagicMock:
    s = MagicMock()
    s.BODY_LOG_S3_BUCKET = bucket
    return s


def _admin() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(), email="admin@test", role=UserRole.ADMIN,
        team_id=None)


def _request() -> MagicMock:
    req = MagicMock()
    req.app.state.system_settings_service = MagicMock()
    req.app.state.system_settings_service.get_body_logging_enabled = \
        AsyncMock(return_value=False)
    req.app.state.system_settings_service.set_body_logging_enabled = \
        AsyncMock(return_value=True)
    req.app.state.system_settings_service.mirror_body_logging_to_cache = \
        AsyncMock()
    return req


class TestProvisionGate:
    async def test_enable_rejected_without_bucket(self, mock_session):
        body = MagicMock(enabled=True)
        with patch("app.routers.settings.get_settings",
                   return_value=_settings("")):
            with pytest.raises(ForbiddenError, match="not provisioned"):
                await set_body_logging(
                    _request(), body, _admin(), mock_session)

    async def test_disable_allowed_without_bucket(self, mock_session):
        """끄기는 안전한 방향이라 인프라 없이도 허용 — 꼬인 ON 상태 정리 경로."""
        body = MagicMock(enabled=False)
        req = _request()
        req.app.state.system_settings_service.set_body_logging_enabled = \
            AsyncMock(return_value=False)
        with patch("app.routers.settings.get_settings",
                   return_value=_settings("")), \
             patch("app.routers.settings.audit_logger") as al:
            al.log = AsyncMock()
            resp = await set_body_logging(
                req, body, _admin(), mock_session)
        assert resp.enabled is False
        assert resp.provisioned is False

    async def test_get_reports_provisioned(self, mock_session):
        with patch("app.routers.settings.get_settings",
                   return_value=_settings("")):
            resp = await get_body_logging(
                _request(), _admin(), mock_session)
        assert resp.provisioned is False

        with patch("app.routers.settings.get_settings",
                   return_value=_settings("bucket-x")):
            resp = await get_body_logging(
                _request(), _admin(), mock_session)
        assert resp.provisioned is True
