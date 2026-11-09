# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""SERVICE_KEYS_ENABLED 배포 플래그 — 표지·장기키 발급 엔드포인트 403 게이트.

gateway.yaml 의 features.service_keys=false 가 내려간 배포에서는 서비스 계정
표지·발급을 전부 막아 "발급은 안 되는데 사람 계정만 service_account 로 잠기는"
사고를 구조 차단한다. 해지·조회·repair 는 열어둔다(기존 키 보호).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole
from app.routers.keys import issue_service_key
from app.routers.service_accounts import (
    _require_service_keys_enabled,
    mark_and_issue_service_key,
    mark_service_account,
)


def _settings(enabled: bool) -> MagicMock:
    s = MagicMock()
    s.SERVICE_KEYS_ENABLED = enabled
    return s


def _admin() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(), email="admin@test", role=UserRole.ADMIN,
        team_id=None)


class TestRequireServiceKeysEnabled:
    def test_disabled_raises_forbidden(self):
        with patch("app.routers.service_accounts.get_settings",
                   return_value=_settings(False)):
            with pytest.raises(ForbiddenError, match="SERVICE_KEYS_ENABLED"):
                _require_service_keys_enabled()

    def test_enabled_passes(self):
        with patch("app.routers.service_accounts.get_settings",
                   return_value=_settings(True)):
            _require_service_keys_enabled()  # 예외 없음


class TestIssueGates:
    """게이트는 레포지토리 접근 전에 발화 — session/레포 mock 없이 거부돼야 한다."""

    async def test_issue_service_key_disabled(self, mock_session):
        body = MagicMock(user_id=uuid.uuid4(), ttl_days=None)
        with patch("app.routers.keys.get_settings",
                   return_value=_settings(False)):
            with pytest.raises(ForbiddenError):
                await issue_service_key(
                    MagicMock(), body, _admin(), mock_session)

    async def test_mark_service_account_disabled(self, mock_session):
        """발급이 막힌 배포에서 표지만 열면 사람 계정이 잠기는 사고만 남는다 —
        표지도 같이 막는다."""
        body = MagicMock(email="svc@test")
        with patch("app.routers.service_accounts.get_settings",
                   return_value=_settings(False)):
            with pytest.raises(ForbiddenError):
                await mark_service_account(
                    body, MagicMock(), _admin(), mock_session)

    async def test_mark_and_issue_disabled(self, mock_session):
        with patch("app.routers.service_accounts.get_settings",
                   return_value=_settings(False)):
            with pytest.raises(ForbiddenError):
                await mark_and_issue_service_key(
                    uuid.uuid4(), MagicMock(), MagicMock(), _admin(),
                    mock_session)


class TestRecoveryStaysOpen:
    """발급/표지를 막아도 해지 경로는 열어둔다 — 꺼진 배포에서 기존 키·표지를
    정리할 수 있어야 한다. 두 함수 모두 _require_service_keys_enabled 를
    호출하지 않는다는 회귀 보장."""

    async def test_unmark_not_flag_gated(self, mock_session):
        """서비스 계정이 아닌 유저는 표지 해제가 no-op — flag 가 막았다면
        ForbiddenError 가 먼저 난다."""
        from app.routers.service_accounts import unmark_service_account

        user = MagicMock()
        user.id = uuid.uuid4()
        user.is_service_account = False
        with patch(
            "app.routers.service_accounts.get_settings",
            return_value=_settings(False),
        ), patch(
            "app.routers.service_accounts.UserRepository"
        ) as repo_cls:
            repo_cls.return_value.get_user = AsyncMock(return_value=user)
            req = MagicMock()
            req.client.host = "127.0.0.1"
            req.state.request_id = "r1"
            out = await unmark_service_account(
                user.id, req, _admin(), mock_session)
        assert out["is_service_account"] is False

    async def test_revoke_not_flag_gated(self, mock_session):
        """revoke 는 KeyService 에 위임 — flag 게이트가 없어 서비스까지 도달."""
        from app.routers.keys import revoke_key

        svc = MagicMock()
        svc.revoke_key = AsyncMock()
        req = MagicMock()
        req.app.state.key_service = svc
        req.client.host = "127.0.0.1"
        req.state.request_id = "r1"
        with patch("app.routers.keys.get_settings",
                   return_value=_settings(False)):
            await revoke_key(req, uuid.uuid4(), _admin(), mock_session)
        svc.revoke_key.assert_awaited_once()
