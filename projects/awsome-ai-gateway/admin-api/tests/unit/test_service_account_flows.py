# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""서비스 계정 흐름 — dedup kind 일치, repair 재확인, 표지 시 키 폐기, 발급 잠금."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.models.auth import KeyStatus, User, UserRole, VirtualKey
from app.routers.service_accounts import mark_service_account
from app.schemas.keys import ServiceAccountCreateRequest
from app.services.key_service import (
    VK_KIND_EXCHANGE,
    VK_KIND_SERVICE,
    KeyService,
)


@pytest.fixture
def key_service(encryption, cache_mgr) -> KeyService:
    return KeyService(encryption=encryption, cache_mgr=cache_mgr)


def _user(*, service: bool, role: UserRole = UserRole.DEVELOPER) -> MagicMock:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.email = "svc@test"
    u.team_id = None
    u.sso_subject = None
    u.is_service_account = service
    u.is_active = True
    u.role = role
    return u


def _actor() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        email="admin@test",
        role=UserRole.ADMIN,
        team_id=None,
    )


def _existing_key(encryption, *, kind: str, user_id: uuid.UUID, raw: str) -> MagicMock:
    now = datetime.now(timezone.utc)
    vk = MagicMock(spec=VirtualKey)
    vk.id = uuid.uuid4()
    vk.user_id = user_id
    vk.kind = kind
    vk.status = KeyStatus.ACTIVE
    vk.issued_at = now - timedelta(seconds=1)  # VK_DEDUP_SECONDS(5) 이내
    vk.expires_at = now + timedelta(hours=1)  # 요청 expires_at(24h) 이하
    vk.key_prefix = raw[:11]
    vk.key_value_encrypted = encryption.encrypt(raw)
    return vk


def _patch_issue_repos():
    """issue_key 신규 발급 경로에 필요한 repo 패치 묶음."""
    return (
        patch("app.services.key_service.KeyRepository"),
        patch("app.services.key_service.UserAllowedModelRepository"),
        patch("app.services.key_service.UserAllowedClientRepository"),
        patch("app.services.key_service.audit_logger"),
    )


# ── 1. dedup 은 kind 가 다르면 재사용하지 않는다 ──────────────────────────


class TestDedupKindMatch:
    async def _issue(self, key_service, mock_session, mock_redis, encryption, *, existing_kind):
        user = _user(service=True)
        old_raw = "vk-" + "ab" * 32
        existing = _existing_key(
            encryption, kind=existing_kind, user_id=user.id, raw=old_raw
        )
        mock_redis.exists = AsyncMock(return_value=1)  # Redis 엔트리 생존

        p_repo, p_uam, p_uac, p_audit = _patch_issue_repos()
        with p_repo as MockRepo, p_uam as MockUam, p_uac as MockUac, p_audit as mock_audit:
            repo = MockRepo.return_value
            repo.list_active_for_user = AsyncMock(return_value=[existing])
            repo.expire_and_create = AsyncMock(return_value=(1, uuid.uuid4(), []))
            MockUam.return_value.list_by_user = AsyncMock(return_value=[])
            MockUac.return_value.list_by_user = AsyncMock(return_value=[])
            mock_audit.log = AsyncMock()
            result = await key_service.issue_key(
                mock_session,
                user_id=user.id,
                actor=_actor(),
                user=user,
                kind=VK_KIND_SERVICE,
            )
        return result, old_raw, repo

    async def test_exchange_key_not_reused_for_service_request(
        self, key_service, mock_session, mock_redis, encryption
    ):
        result, old_raw, repo = await self._issue(
            key_service, mock_session, mock_redis, encryption,
            existing_kind=VK_KIND_EXCHANGE,
        )
        assert result.virtual_key != old_raw
        assert result.virtual_key.startswith("vk-")
        repo.expire_and_create.assert_awaited_once()

    async def test_same_kind_is_reused_control(
        self, key_service, mock_session, mock_redis, encryption
    ):
        """대조군: kind 가 같으면 같은 셋업에서 기존 키를 재반환한다."""
        result, old_raw, repo = await self._issue(
            key_service, mock_session, mock_redis, encryption,
            existing_kind=VK_KIND_SERVICE,
        )
        assert result.virtual_key == old_raw
        repo.expire_and_create.assert_not_awaited()


# ── 2. repair 는 FOR UPDATE 재확인에서 REVOKED 면 건너뛴다 ────────────────


class TestRepairSkipsRevoked:
    async def test_revoked_on_reread_is_skipped(
        self, key_service, mock_redis, encryption
    ):
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        raw = "vk-" + "cd" * 32
        vk = _existing_key(encryption, kind=VK_KIND_SERVICE, user_id=user_id, raw=raw)

        locked = MagicMock(spec=VirtualKey)
        locked.id = vk.id
        locked.status = KeyStatus.REVOKED
        locked.expires_at = vk.expires_at
        locked.key_value_encrypted = vk.key_value_encrypted

        session = MagicMock()
        result = MagicMock()
        result.all.return_value = [(vk, team_id)]
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.get = AsyncMock(return_value=locked)
        session.refresh = AsyncMock()

        @asynccontextmanager
        async def _cm():
            yield session

        mock_redis.exists = AsyncMock(return_value=0)  # 유실 → 정상이면 재기록 대상

        with patch(
            "app.services.key_service.AsyncSessionLocal", MagicMock(side_effect=_cm)
        ), patch("app.services.key_service.KeyRepository") as MockRepo:
            MockRepo.return_value.get_by_id = AsyncMock(return_value=locked)
            repaired = await key_service.repair_service_key_redis(user_id=user_id)

        assert repaired == 0
        MockRepo.return_value.get_by_id.assert_awaited_once_with(vk.id, for_update=True)
        mock_redis.setex.assert_not_called()
        mock_redis.sadd.assert_not_called()
        session.commit.assert_awaited()  # 잠금 해제


# ── 3. 서비스 계정 표지 시 비-service 키만 폐기 ─────────────────────────────


class TestMarkServiceAccountRevokes:
    async def test_revokes_only_non_service_keys(self, mock_session, admin_user):
        target = _user(service=False)
        exchange_vk = MagicMock(spec=VirtualKey)
        exchange_vk.id = uuid.uuid4()
        exchange_vk.kind = VK_KIND_EXCHANGE
        exchange_vk.status = KeyStatus.ACTIVE
        service_vk = MagicMock(spec=VirtualKey)
        service_vk.id = uuid.uuid4()
        service_vk.kind = VK_KIND_SERVICE
        service_vk.status = KeyStatus.ACTIVE

        svc = MagicMock(spec=KeyService)
        svc.revoke_key = AsyncMock()
        request = MagicMock()
        request.app.state.key_service = svc
        request.client.host = "10.0.0.1"
        request.state.request_id = "req-1"

        with patch("app.routers.service_accounts.UserRepository") as MockUser, \
             patch("app.repositories.key_repository.KeyRepository") as MockKey, \
             patch("app.routers.service_accounts.audit_logger") as mock_audit:
            MockUser.return_value.get_by_email = AsyncMock(return_value=target)
            MockKey.return_value.list_revocable_for_users = AsyncMock(
                return_value=[exchange_vk, service_vk]
            )
            mock_audit.log = AsyncMock()
            resp = await mark_service_account(
                body=ServiceAccountCreateRequest(email=" svc@test "),
                request=request,
                admin=admin_user,
                session=mock_session,
            )

        MockUser.return_value.get_by_email.assert_awaited_once_with(
            "svc@test", for_update=True
        )
        MockKey.return_value.list_revocable_for_users.assert_awaited_once_with(
            [target.id]
        )
        assert svc.revoke_key.await_count == 1
        kwargs = svc.revoke_key.await_args.kwargs
        assert kwargs["key_id"] == exchange_vk.id
        assert kwargs["bulk"] is True
        assert kwargs["notify"] is True
        assert kwargs["ip_address"] == "10.0.0.1"
        assert kwargs["request_id"] == "req-1"
        assert target.is_service_account is True
        assert resp["is_service_account"] is True


# ── 4. issue_key 는 users 행을 FOR UPDATE 로 잠근다 ─────────────────────────


class TestIssueKeyLocksUser:
    async def test_get_user_for_update_when_user_not_passed(
        self, key_service, mock_session
    ):
        user = _user(service=False)
        p_repo, p_uam, p_uac, p_audit = _patch_issue_repos()
        with p_repo as MockRepo, p_uam as MockUam, p_uac as MockUac, \
             p_audit as mock_audit, \
             patch("app.services.key_service.UserRepository") as MockUser:
            MockRepo.return_value.list_active_for_user = AsyncMock(return_value=[])
            MockRepo.return_value.expire_and_create = AsyncMock(
                return_value=(0, uuid.uuid4(), [])
            )
            MockUam.return_value.list_by_user = AsyncMock(return_value=[])
            MockUac.return_value.list_by_user = AsyncMock(return_value=[])
            mock_audit.log = AsyncMock()
            MockUser.return_value.get_user = AsyncMock(return_value=user)
            await key_service.issue_key(
                mock_session, user_id=user.id, actor=_actor(), kind=VK_KIND_EXCHANGE
            )
        MockUser.return_value.get_user.assert_awaited_once_with(
            user.id, for_update=True
        )
        mock_session.refresh.assert_not_awaited()

    async def test_refresh_with_for_update_when_user_passed(
        self, key_service, mock_session
    ):
        user = _user(service=True)
        p_repo, p_uam, p_uac, p_audit = _patch_issue_repos()
        with p_repo as MockRepo, p_uam as MockUam, p_uac as MockUac, \
             p_audit as mock_audit, \
             patch("app.services.key_service.UserRepository") as MockUser:
            MockRepo.return_value.list_active_for_user = AsyncMock(return_value=[])
            MockRepo.return_value.expire_and_create = AsyncMock(
                return_value=(0, uuid.uuid4(), [])
            )
            MockUam.return_value.list_by_user = AsyncMock(return_value=[])
            MockUac.return_value.list_by_user = AsyncMock(return_value=[])
            mock_audit.log = AsyncMock()
            await key_service.issue_key(
                mock_session,
                user_id=user.id,
                actor=_actor(),
                user=user,
                kind=VK_KIND_SERVICE,
            )
        mock_session.refresh.assert_awaited_once_with(user, with_for_update=True)
        MockUser.return_value.get_user.assert_not_called()
