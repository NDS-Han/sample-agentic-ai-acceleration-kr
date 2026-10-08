# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""KeyService.revoke_keys_for_users — Cognito offboarding VK 일괄 폐기 (R3-3).

배경: cognito_sync 가 사용자를 is_active=False 로 만들기만 하면, 게이트웨이의
per-request is_active 재확인(R2-8)에 전적으로 의존하게 되고, 재활성화 시 폐기되지
않은 옛 키가 그대로 부활한다. 여기서 DB 상태·Redis 매핑/캐시·team 역인덱스·
key_revoked 알림까지 함께 정리한다.

고정하는 것:
  1. ACTIVE VK 가 DB 상 REVOKED 로 바뀐다 (repo.revoke_many 호출).
  2. key:vk:{sha256(raw)} + key:cache:vk:{sha256(raw)} 가 Redis 에서 지워진다.
  3. 복호화 실패 키가 있어도 나머지 키는 정상 폐기된다.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.models.auth import User, VirtualKey
from app.services.key_service import KeyService


@pytest.fixture
def key_service(encryption: AESEncryptionService, cache_mgr: CacheInvalidationManager) -> KeyService:
    return KeyService(encryption=encryption, cache_mgr=cache_mgr)


def _vk(user_id: uuid.UUID, enc: str) -> MagicMock:
    vk = MagicMock(spec=VirtualKey)
    vk.id = uuid.uuid4()
    vk.user_id = user_id
    vk.key_value_encrypted = enc
    vk.key_prefix = "vk-test"
    return vk


@pytest.mark.asyncio
async def test_revokes_db_and_invalidates_cache(
    key_service: KeyService, mock_session: AsyncMock, mock_redis: AsyncMock,
    encryption,
):
    uid = uuid.uuid4()
    team_id = uuid.uuid4()
    enc1 = encryption.encrypt("sk-raw-one")
    vks = [_vk(uid, enc1)]

    # 팀 역인덱스 srem 용 user→team 조회 결과
    user_row = MagicMock()
    user_row.id = uid
    user_row.team_id = team_id
    mock_session.execute = AsyncMock(
        return_value=MagicMock(all=lambda: [user_row])
    )

    with patch("app.services.key_service.KeyRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.list_active_for_users = AsyncMock(return_value=vks)
        repo.revoke_many = AsyncMock(return_value=1)

        n = await key_service.revoke_keys_for_users(
            mock_session, user_ids=[uid], reason="cognito_deactivated"
        )

    assert n == 1
    repo.revoke_many.assert_awaited_once()
    assert repo.revoke_many.await_args[0][0] == [vks[0].id]

    h = hashlib.sha256(b"sk-raw-one").hexdigest()
    deleted = {c.args[0] for c in mock_redis.delete.await_args_list}
    assert f"key:vk:{h}" in deleted
    assert f"key:cache:vk:{h}" in deleted

    srem_keys = [c.args[0] for c in mock_redis.srem.await_args_list]
    assert f"team:vk_hashes:{team_id}" in srem_keys


@pytest.mark.asyncio
async def test_decrypt_failure_still_revokes_others(
    key_service: KeyService, mock_session: AsyncMock, mock_redis: AsyncMock,
    encryption,
):
    uid = uuid.uuid4()
    enc_ok = encryption.encrypt("sk-raw-ok")
    vks = [_vk(uid, b"corrupt-not-decryptable"), _vk(uid, enc_ok)]

    user_row = MagicMock()
    user_row.id = uid
    user_row.team_id = None
    mock_session.execute = AsyncMock(
        return_value=MagicMock(all=lambda: [user_row])
    )

    with patch("app.services.key_service.KeyRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.list_active_for_users = AsyncMock(return_value=vks)
        repo.revoke_many = AsyncMock(return_value=2)

        n = await key_service.revoke_keys_for_users(
            mock_session, user_ids=[uid]
        )

    assert n == 2  # DB 폐기는 둘 다
    h = hashlib.sha256(b"sk-raw-ok").hexdigest()
    deleted = {c.args[0] for c in mock_redis.delete.await_args_list}
    assert f"key:vk:{h}" in deleted  # 복호화 가능한 쪽만 캐시 정리
