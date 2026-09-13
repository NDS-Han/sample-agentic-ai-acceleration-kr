# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import uuid

import structlog

from app.core.clients import VALID_CLIENTS
from app.repositories.user_allowed_client_repository import UserAllowedClientRepository
from app.repositories.user_repository import UserRepository

logger = structlog.get_logger(__name__)

#: 단일 출처는 core/clients.py (중복 3벌을 모았다).
_VALID_CLIENTS = VALID_CLIENTS


class UserAllowedClientService:
    def __init__(self, session, cache_mgr=None, key_service=None) -> None:
        self._session = session
        self._cache_mgr = cache_mgr  # optional: to invalidate VK auth cache for the user
        self._key_service = key_service  # optional: to resolve the user's active VK hashes

    async def get(self, user_id: uuid.UUID) -> list[str]:
        return await UserAllowedClientRepository(self._session).list_by_user(user_id)

    async def set(self, user_id: uuid.UUID, clients: list[str], admin_id: uuid.UUID) -> list[str]:
        invalid = [c for c in clients if c not in _VALID_CLIENTS]
        if invalid:
            raise ValueError(f"invalid clients: {invalid}; allowed={sorted(_VALID_CLIENTS)}")
        # dedup, preserve canonical set
        clients = sorted(set(clients))
        repo = UserAllowedClientRepository(self._session)
        user = await UserRepository(self._session).get_user(user_id)
        if user is None:
            raise LookupError(f"user not found: {user_id}")
        await repo.replace_for_user(user_id, clients, admin_id)
        # ⚠️ 여기서 캐시를 지우지 **않는다.** 이 메서드는 커밋 전에 끝나고, 커밋은
        #    라우터가 한다. DEL→commit 창에 들어온 게이트웨이 요청이 **변경 전** 정책을
        #    다시 캐시하면 그 스냅샷이 VK 캐시 TTL(~300s) 동안 살아 있다 — 화면은 200 을
        #    받았는데 데이터 평면은 옛 허용 목록을 계속 집행한다. 좁히는 변경이면 그건
        #    권한 누출이다.
        #    무효화는 라우터가 커밋 뒤에 `invalidate_user_vk_cache` 로 한다.
        #    (이 레포의 allowed-**models** 경로가 이미 그 순서다: routers/users.py:494)
        logger.info(
            "admin.set_user_allowed_clients",
            user_id=str(user_id), clients=clients, admin_id=str(admin_id),
        )
        return clients

    async def clear(self, user_id: uuid.UUID, admin_id: uuid.UUID) -> None:
        await UserAllowedClientRepository(self._session).clear_for_user(user_id)
        # 커밋 후 무효화 — 위 `set` 의 주석과 같은 이유.
        logger.info(
            "admin.clear_user_allowed_clients",
            user_id=str(user_id), admin_id=str(admin_id),
        )

    async def invalidate_user_vk_cache(self, user_id: uuid.UUID) -> None:
        """VK 인증 캐시 무효화. **``session.commit()`` 뒤에** 호출한다.

        예전엔 `set`/`clear` 가 스스로 이것을 호출했는데, 그 시점은 라우터의 커밋
        **전**이었다. 그래서 DEL 과 commit 사이에 들어온 요청이 옛 정책을 다시 캐시했다.
        """
        await self._invalidate(user_id)

    async def _invalidate(self, user_id: uuid.UUID) -> None:
        # 변경 즉시 반영: 해당 user 의 ACTIVE VK 들의 auth 캐시(`key:cache:vk:*`)를 DEL.
        # cache_mgr/key_service 미주입 시(read-only 경로 등) no-op → VK 캐시 TTL(~300s)로 자연 만료.
        if self._cache_mgr is None or self._key_service is None:
            return
        try:
            hashes = await self._key_service.list_active_vk_hashes_for_user(
                self._session, user_id
            )
            keys = [f"key:cache:vk:{h}" for h in hashes]
            if keys:
                await self._cache_mgr.invalidate(keys, session=self._session)
        except Exception:
            logger.warning(
                "allowed_clients_cache_invalidate_failed",
                user_id=str(user_id),
                exc_info=True,
            )
