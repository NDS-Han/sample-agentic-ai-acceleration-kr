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
# 형식 검증은 모델 라우팅(schemas/models.py)의 검증기와 같은 규칙을 공유한다 —
# 같은 ARN/리전/ExternalId 를 두 군데에서 다르게 검증하면 편집 경로끼리 어긋난다.
from app.schemas.models import (
    _validate_account_role_arn,
    _validate_routing_external_id,
    _validate_routing_region,
)

router = APIRouter(prefix="/admin/routing-profiles", tags=["Routing Profiles"])


class WebSearchToggleBody(BaseModel):
    enabled: bool


class RoutingProfileItem(BaseModel):
    client: str
    web_search_enabled: bool
    backend: str
    enabled: bool
    account_role_arn: str | None = None
    external_id: str | None = None
    region: str


class ClientRoutingBody(BaseModel):
    # region 은 routing_profiles 가 NOT NULL 이라 항상 필요(ARN 없으면 in-account
    # 호출의 기본 서명 리전). arn 을 비우면 크로스계정 라우팅 해제.
    account_role_arn: str | None = None
    external_id: str | None = None
    region: str


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


@router.put("/{client}/routing")
async def set_client_routing(
    request: Request,
    client: str,
    body: ClientRoutingBody,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """클라이언트 프로필의 크로스계정 라우팅(arn/external_id/region)을 설정·해제.

    region 은 항상 필수 — 이 컬럼은 ARN 이 없을 때 in-account 호출의 기본
    서명 리전이라 비울 수 없다. account_role_arn 을 비우면 external_id 도
    함께 지워져 in-account 라우팅으로 복귀한다.
    """
    try:
        account_role_arn = _validate_account_role_arn(body.account_role_arn)
        region = _validate_routing_region(body.region)
        external_id = _validate_routing_external_id(body.external_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not account_role_arn and body.external_id:
        raise HTTPException(
            status_code=400,
            detail="external_id는 account_role_arn과 함께 설정해야 합니다",
        )

    svc = RoutingProfileService(session, cache_mgr=request.app.state.cache_mgr)
    try:
        result = await svc.set_routing(
            client, account_role_arn, external_id, region, admin.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await session.commit()
    # 커밋 후 무효화 — web-search 엔드포인트와 같은 DEL→commit 창 규칙.
    await svc.invalidate_cache(client)
    # external_id 는 trust 공유 비밀 — 감사 로그에는 길이만 남긴다
    # (model_service.py 의 마스킹과 동일 규칙).
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SET_CLIENT_ROUTING",
        resource_type="RoutingProfile",
        resource_id=client,
        changes={"after": {
            "account_role_arn": account_role_arn,
            "region": region,
            "external_id": f"***({len(external_id)}자)" if external_id else None,
        }},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return result
