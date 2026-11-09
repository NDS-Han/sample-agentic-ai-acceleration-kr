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


@pytest.fixture(autouse=True)
def _service_keys_on():
    """이 파일은 발급·표지 흐름 자체를 검증한다 — 배포 플래그 기본은
    false(fail-closed)라 실제 settings 에 플래그만 켜서 패치한다.
    (라우터가 APP_KEY_MAX_TTL_DAYS 도 읽으므로 bare mock 은 안 됨)
    게이트 동작 자체는 test_service_keys_flag.py 가 커버."""
    from app.core.config import get_settings

    s = get_settings()
    with patch.object(s, "SERVICE_KEYS_ENABLED", True):
        yield


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
    async def _issue(self, key_service, mock_session, mock_redis, encryption, *, existing_kind, issue_kind=VK_KIND_SERVICE):
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
                kind=issue_kind,
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

    async def test_service_kind_is_not_deduped(
        self, key_service, mock_session, mock_redis, encryption
    ):
        """service kind 재발급은 관리자의 회전 의도 — dedup 창 안에서도
        같은 키를 돌려주지 않고 새 키를 발급한다."""
        result, old_raw, repo = await self._issue(
            key_service, mock_session, mock_redis, encryption,
            existing_kind=VK_KIND_SERVICE,
        )
        assert result.virtual_key != old_raw
        repo.expire_and_create.assert_awaited_once()

    async def test_exchange_kind_is_deduped_control(
        self, key_service, mock_session, mock_redis, encryption
    ):
        """대조군: 사람 단기키는 여전히 dedup 된다."""
        result, old_raw, repo = await self._issue(
            key_service, mock_session, mock_redis, encryption,
            existing_kind=VK_KIND_EXCHANGE,
            issue_kind=VK_KIND_EXCHANGE,
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


# ── 5. svc 토큰은 에이전트 생명주기를 건드리지 못한다 ─────────────────────

from app.core.exceptions import ForbiddenError, ValidationError
from app.routers.service_accounts import (
    mark_and_issue_service_key,
    unmark_service_account,
)
from app.schemas.keys import IssueServiceAccountKeyRequest


def _svc_actor() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        email="svc-token@internal",
        role=UserRole.ADMIN,
        team_id=None,
        is_service_token=True,
        service_token_id=uuid.uuid4(),
    )


def _request():
    request = MagicMock()
    request.client.host = "10.0.0.1"
    request.state.request_id = "req-x"
    return request


class TestServiceTokenGuard:
    async def test_mark_forbidden(self, mock_session):
        with pytest.raises(ForbiddenError):
            await mark_service_account(
                body=ServiceAccountCreateRequest(email="a@b"),
                request=_request(),
                admin=_svc_actor(),
                session=mock_session,
            )

    async def test_unmark_forbidden(self, mock_session):
        with pytest.raises(ForbiddenError):
            await unmark_service_account(
                user_id=uuid.uuid4(),
                request=_request(),
                admin=_svc_actor(),
                session=mock_session,
            )

    async def test_issue_forbidden(self, mock_session):
        with pytest.raises(ForbiddenError):
            await mark_and_issue_service_key(
                user_id=uuid.uuid4(),
                body=IssueServiceAccountKeyRequest(ttl_days=30),
                request=_request(),
                admin=_svc_actor(),
                session=mock_session,
            )

    async def test_keys_issue_service_key_forbidden(self, mock_session):
        """keys.py 의 개별 발급 경로에도 같은 가드 — svc 토큰은 장기 VK 를
        찍을 수 없다(자기 권한 확장 차단)."""
        from app.routers.keys import issue_service_key
        from app.schemas.keys import IssueServiceKeyRequest

        with pytest.raises(ForbiddenError):
            await issue_service_key(
                request=_request(),
                body=IssueServiceKeyRequest(ttl_days=30, user_id=uuid.uuid4()),
                admin=_svc_actor(),
                session=mock_session,
            )


# ── 6. 표지+발급 결합 엔드포인트 ─────────────────────────────────────────


class TestMarkAndIssue:
    async def _call(self, user, mock_session, admin, *, svc=None):
        request = _request()
        request.app.state.key_service = svc or MagicMock()
        with patch("app.routers.service_accounts.UserRepository") as MockUser, \
             patch("app.repositories.key_repository.KeyRepository") as MockKey, \
             patch("app.routers.service_accounts.audit_logger") as mock_audit, \
             patch("app.services.key_service.BudgetRepository") as MockBudget:
            MockUser.return_value.get_user = AsyncMock(return_value=user)
            MockKey.return_value.list_revocable_for_users = AsyncMock(
                return_value=[]
            )
            MockBudget.return_value.get_active_config = AsyncMock(
                return_value=None
            )
            mock_audit.log = AsyncMock()
            return await mark_and_issue_service_key(
                user_id=user.id,
                body=IssueServiceAccountKeyRequest(ttl_days=30),
                request=request,
                admin=admin,
                session=mock_session,
            )

    async def test_issue_failure_leaves_flag_uncommitted_path(
        self, mock_session, admin_user
    ):
        """팀 미배정 유저 → 발급 검사가 raise — 같은 트랜잭션이라
        표지도 commit 되지 않고 요청 전체가 거절된다."""
        user = _user(service=False)
        user.team_id = None
        svc = MagicMock()
        svc.revoke_key = AsyncMock()
        svc.issue_key = AsyncMock()
        with pytest.raises(ValidationError):
            await self._call(user, mock_session, admin_user, svc=svc)
        # 발급 전에 readiness 에서 실패 — issue_key 는 호출되지 않는다.
        svc.issue_key.assert_not_awaited()
        # 표지 세팅 자체는 같은 트랜잭션 안에서 일어났다(롤백 대상).
        assert user.is_service_account is True

    async def test_happy_path_marks_and_issues(
        self, mock_session, admin_user
    ):
        user = _user(service=False)
        user.team_id = uuid.uuid4()
        svc = MagicMock()
        svc.revoke_key = AsyncMock()
        svc.issue_key = AsyncMock(
            return_value=MagicMock(
                key_id=str(uuid.uuid4()),
                key_prefix="vk-test",
                user_id=str(user.id),
                status=KeyStatus.ACTIVE,
                expires_at=datetime.now(timezone.utc),
                virtual_key="vk-" + "ef" * 32,
            )
        )
        request = _request()
        request.app.state.key_service = svc
        with patch("app.routers.service_accounts.UserRepository") as MockUser, \
             patch("app.repositories.key_repository.KeyRepository") as MockKey, \
             patch("app.routers.service_accounts.audit_logger") as mock_audit, \
             patch("app.services.key_service.BudgetRepository") as MockBudget:
            MockUser.return_value.get_user = AsyncMock(return_value=user)
            MockKey.return_value.list_revocable_for_users = AsyncMock(
                return_value=[]
            )
            cfg = MagicMock()
            cfg.max_budget_usd = 100
            MockBudget.return_value.get_active_config = AsyncMock(
                return_value=cfg
            )
            mock_audit.log = AsyncMock()
            resp = await mark_and_issue_service_key(
                user_id=user.id,
                body=IssueServiceAccountKeyRequest(ttl_days=30),
                request=request,
                admin=admin_user,
                session=mock_session,
            )
        assert user.is_service_account is True
        svc.issue_key.assert_awaited_once()
        assert resp["virtual_key"].startswith("vk-")
        assert resp["key_prefix"] == "vk-test"
