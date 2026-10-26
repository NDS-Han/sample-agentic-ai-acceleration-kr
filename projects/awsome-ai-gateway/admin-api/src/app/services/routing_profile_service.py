# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""RoutingProfileService — read/update per-client web_search_enabled toggle.

Writes model.routing_profiles.web_search_enabled, then DELs the gateway's Redis
cache key `routing_profile:{client}` (invalidate-only, like ModelService) so the
gateway repopulates from DB on the next request. Same ElastiCache as gateway-proxy.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from app.core.clients import VALID_CLIENTS
from app.models.routing import RoutingProfile

logger = structlog.get_logger(__name__)

# Must match gateway-proxy client_identifier tokens + user_allowed_client_service.
#: 단일 출처는 core/clients.py.
_VALID_CLIENTS = VALID_CLIENTS
# Cache key written by gateway-proxy RoutingProfileLoader (routing_profile_loader.py).
_CACHE_KEY = "routing_profile:{client}"


class RoutingProfileService:
    def __init__(self, session, cache_mgr=None) -> None:
        self._session = session
        self._cache_mgr = cache_mgr

    async def list_profiles(self) -> list[dict]:
        """All routing profiles with their web_search flag (for the admin toggle UI)."""
        rows = (await self._session.execute(select(RoutingProfile))).scalars().all()
        return [
            {
                "client": r.client,
                "web_search_enabled": bool(r.web_search_enabled),
                "backend": r.backend,
                "enabled": bool(r.enabled),
                "account_role_arn": r.account_role_arn,
                "external_id": r.external_id,
                "region": r.region,
            }
            for r in sorted(rows, key=lambda x: x.client)
        ]

    async def set_routing(
        self,
        client: str,
        account_role_arn: str | None,
        external_id: str | None,
        region: str,
        admin_id,
    ) -> dict:
        """크로스계정 라우팅 트리플을 쓴다. ARN 을 비우면 in-account 로 되돌린다.

        ``region`` 은 ``routing_profiles.region`` 이 NOT NULL 컬럼(ARN 없을 때
        in-account 호출의 기본 서명 리전)이라 항상 필요하다 — 모델 라우팅
        (model_aliases)의 "ARN 지우면 트리플 전체 해제"와 다른 규칙.
        """
        if client not in _VALID_CLIENTS:
            raise ValueError(f"invalid client: {client}; allowed={sorted(_VALID_CLIENTS)}")
        row = (
            await self._session.execute(
                select(RoutingProfile).where(RoutingProfile.client == client)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"routing profile not found for client: {client}")
        row.account_role_arn = account_role_arn
        # ARN 이 없으면 external_id 는 게이트웨이가 읽지 않는 dead data — 같이 지운다.
        row.external_id = external_id if account_role_arn else None
        row.region = region
        await self._session.flush()
        # set_web_search 와 같은 규칙 — 캐시 무효화는 라우터가 commit 뒤에 한다.
        logger.info(
            "admin.set_client_routing",
            client=client,
            account_role_arn=account_role_arn,
            region=region,
            has_external_id=bool(external_id),
            admin_id=str(admin_id),
        )
        return {
            "client": client,
            "account_role_arn": account_role_arn,
            "external_id": external_id if account_role_arn else None,
            "region": region,
        }

    async def set_web_search(self, client: str, enabled: bool, admin_id) -> dict:
        if client not in _VALID_CLIENTS:
            raise ValueError(f"invalid client: {client}; allowed={sorted(_VALID_CLIENTS)}")
        row = (
            await self._session.execute(
                select(RoutingProfile).where(RoutingProfile.client == client)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"routing profile not found for client: {client}")
        row.web_search_enabled = bool(enabled)
        await self._session.flush()
        # ⚠️ 캐시를 여기서 지우지 **않는다** — flush 는 커밋이 아니다. DEL→commit 창에
        #    들어온 게이트웨이 요청이 `routing_profile:{client}` 를 **토글 전** 값으로
        #    다시 캐시하면, 관리자는 성공을 봤는데 그 앱은 300s 동안 옛 설정으로 돈다.
        #    무효화는 라우터가 커밋 뒤에 `invalidate_cache` 로 한다.
        logger.info(
            "admin.set_web_search_enabled",
            client=client, web_search_enabled=bool(enabled), admin_id=str(admin_id),
        )
        return {"client": client, "web_search_enabled": bool(enabled)}

    async def invalidate_cache(self, client: str) -> None:
        """``routing_profile:{client}`` 무효화. **``session.commit()`` 뒤에** 호출한다.

        ``set_web_search`` 안에서 부르면 안 된다 — 그 시점은 커밋 전이다.
        """
        await self._invalidate(client)

    async def _invalidate(self, client: str) -> None:
        """DEL routing_profile:{client} so gateway-proxy repopulates from DB (self-heal)."""
        if self._cache_mgr is None:
            return  # no-op → gateway's 300s TTL expires naturally
        try:
            # {key}:stale 도 같이 지운다 — 게이트웨이의 DB-열화 백업 키.
            await self._cache_mgr.invalidate(
                [_CACHE_KEY.format(client=client), f"{_CACHE_KEY.format(client=client)}:stale"],
                session=self._session,
            )
        except Exception:
            logger.warning(
                "routing_profile_cache_invalidate_failed", client=client, exc_info=True
            )
