# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""issue_key 의 kind ↔ is_service_account 가드(migration 0050)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.core.exceptions import ValidationError
from app.models.auth import User, UserRole
from app.services.key_service import KeyService


@pytest.fixture
def key_service(encryption, cache_mgr) -> KeyService:
    return KeyService(encryption=encryption, cache_mgr=cache_mgr)


def _user(*, service: bool) -> MagicMock:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.team_id = None
    u.sso_subject = None
    u.is_service_account = service
    return u


def _actor() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        email="admin@test",
        role=UserRole.ADMIN,
        team_id=None,
    )


class TestKindGuards:
    async def test_service_kind_rejected_for_normal_user(self, key_service, mock_session):
        """kind='service' 는 is_service_account 계정에만 — 일반 유저는 422."""
        with patch("app.services.key_service.KeyRepository") as MockRepo:
            MockRepo.return_value.list_active_for_user = AsyncMock(return_value=[])
            with pytest.raises(ValidationError, match="service-kind"):
                await key_service.issue_key(
                    mock_session,
                    user_id=uuid.uuid4(),
                    actor=_actor(),
                    user=_user(service=False),
                    kind="service",
                )

    async def test_exchange_kind_rejected_for_service_account(
        self, key_service, mock_session
    ):
        """서비스 계정은 exchange/sts 경로의 단기키를 받을 수 없다."""
        with patch("app.services.key_service.KeyRepository") as MockRepo:
            MockRepo.return_value.list_active_for_user = AsyncMock(return_value=[])
            with pytest.raises(ValidationError, match="service accounts"):
                await key_service.issue_key(
                    mock_session,
                    user_id=uuid.uuid4(),
                    actor=_actor(),
                    user=_user(service=True),
                    kind="exchange",
                )

    async def test_service_kind_accepted_for_service_account(
        self, key_service, mock_session
    ):
        """정상 경로: service 계정 + kind='service' → 발급 진행(dedup/insert 모킹)."""
        user = _user(service=True)
        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserAllowedModelRepository") as MockUam, \
             patch("app.services.key_service.UserAllowedClientRepository") as MockUac:
            repo = MockRepo.return_value
            repo.list_active_for_user = AsyncMock(return_value=[])
            repo.expire_and_create = AsyncMock(return_value=(0, uuid.uuid4(), []))
            MockUam.return_value.list_by_user = AsyncMock(return_value=[])
            MockUac.return_value.list_by_user = AsyncMock(return_value=[])
            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(
                    mock_session,
                    user_id=user.id,
                    actor=_actor(),
                    user=user,
                    kind="service",
                )
        assert result.virtual_key is not None
        assert result.virtual_key.startswith("vk-")
