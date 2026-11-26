# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import uuid

import structlog

from app.core.audit import audit_logger
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

    async def _audit(
        self,
        *,
        actor,
        action: str,
        user_id: uuid.UUID,
        changes: dict | None,
        ip_address: str,
        request_id: str,
    ) -> None:
        await audit_logger.log(
            self._session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action=action,
            resource_type="UserAllowedClients",
            resource_id=str(user_id),
            changes=changes,
            ip_address=ip_address,
            request_id=request_id,
        )

    async def _finish_apply(self, user_id: uuid.UUID, commit: bool):
        """커밋 후 캐시 무효화 순서를 한 곳에 둔다.

        ``commit=True`` 는 /admin 라우터의 기존 순서(commit → invalidate)를 그대로
        수행한다. ``commit=False`` 는 AICM 멱등성 트랜잭션이 커밋을 소유하도록 하고,
        호출자에게 커밋 뒤 실행할 콜백을 돌려준다.
        """
        async def invalidate() -> None:
            await self.invalidate_user_vk_cache(user_id)

        if not commit:
            return invalidate
        await self._session.commit()
        await invalidate()
        return None

    async def apply_set(
        self,
        user_id: uuid.UUID,
        clients: list[str],
        actor,
        *,
        ip_address: str,
        request_id: str,
        commit: bool = True,
        audit_in_transaction: bool = False,
    ):
        """set + commit/cache/audit 절차. 기본값은 /admin 의 기존 동작과 같다."""
        before = await self.get(user_id) if audit_in_transaction else None
        clients = await self.set(user_id, clients, actor.user_id)
        if audit_in_transaction:
            await self._audit(
                actor=actor, action="SET_USER_ALLOWED_CLIENTS", user_id=user_id,
                changes={"before": {"clients": before}, "after": {"clients": clients}},
                ip_address=ip_address, request_id=request_id,
            )
        after_commit = await self._finish_apply(user_id, commit)
        if commit and not audit_in_transaction:
            await self._audit(
                actor=actor, action="SET_USER_ALLOWED_CLIENTS", user_id=user_id,
                changes={"after": {"clients": clients}},
                ip_address=ip_address, request_id=request_id,
            )
        return clients, after_commit

    async def apply_clear(
        self,
        user_id: uuid.UUID,
        actor,
        *,
        ip_address: str,
        request_id: str,
        commit: bool = True,
        audit_in_transaction: bool = False,
    ):
        """clear + commit/cache/audit 절차. 기본값은 /admin 의 기존 동작과 같다."""
        before = await self.get(user_id) if audit_in_transaction else None
        await self.clear(user_id, actor.user_id)
        if audit_in_transaction:
            await self._audit(
                actor=actor, action="CLEAR_USER_ALLOWED_CLIENTS", user_id=user_id,
                changes={"before": {"clients": before}, "after": {"clients": []}},
                ip_address=ip_address, request_id=request_id,
            )
        after_commit = await self._finish_apply(user_id, commit)
        if commit and not audit_in_transaction:
            await self._audit(
                actor=actor, action="CLEAR_USER_ALLOWED_CLIENTS", user_id=user_id,
                changes=None,
                ip_address=ip_address, request_id=request_id,
            )
        return after_commit

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
