# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Routing profiles admin router — per-client web search toggle.

GET  /admin/routing-profiles                → list all clients + web_search_enabled
PUT  /admin/routing-profiles/{client}/web-search  body {enabled: bool}

Mirrors the users.py allowed-clients pattern (require_admin, per-request service,
cache invalidation, session.commit). The web_search_enabled column already exists
(migration 0021) — no migration needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.services.routing_profile_service import RoutingProfileService

router = APIRouter(prefix="/admin/routing-profiles", tags=["Routing Profiles"])


class WebSearchToggleBody(BaseModel):
    enabled: bool


class RoutingProfileItem(BaseModel):
    client: str
    web_search_enabled: bool
    backend: str
    enabled: bool


class RoutingProfileListResponse(BaseModel):
    items: list[RoutingProfileItem]


@router.get("", response_model=RoutingProfileListResponse)
async def list_routing_profiles(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = RoutingProfileService(session)
    items = await svc.list_profiles()
    return RoutingProfileListResponse(items=[RoutingProfileItem(**i) for i in items])


@router.put("/{client}/web-search")
async def set_web_search(
    request: Request,
    client: str,
    body: WebSearchToggleBody,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = RoutingProfileService(session, cache_mgr=request.app.state.cache_mgr)
    try:
        result = await svc.set_web_search(client, body.enabled, admin.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await session.commit()
    # 커밋 **후** 무효화 — flush 는 커밋이 아니다. 커밋 전에 지우면 그 창에 들어온
    # 게이트웨이 요청이 토글 전 값을 다시 캐시해, 관리자는 성공을 봤는데 그 앱은
    # 300s 동안 옛 설정으로 돈다.
    await svc.invalidate_cache(client)
    # R16: 앱별 web-search 토글도 감사 대상 — 과금/도구 동작을 바꾸는 변경인데
    # audit_logs 행이 없었다.
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SET_WEB_SEARCH",
        resource_type="RoutingProfile",
        resource_id=client,
        changes={"after": {"web_search_enabled": body.enabled}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return result
