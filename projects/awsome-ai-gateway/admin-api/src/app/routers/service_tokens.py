# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import (
    CurrentUser,
    require_admin,
    require_human_admin,
    require_service_token_self,
)
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError
from app.schemas.service_tokens import (
    ServiceTokenCreateRequest,
    ServiceTokenCreateResponse,
    ServiceTokenListResponse,
)

router = APIRouter(prefix="/admin/service-tokens", tags=["Service Token Management"])


@router.post("", response_model=ServiceTokenCreateResponse)
async def issue_service_token(
    request: Request,
    body: ServiceTokenCreateRequest,
    admin: CurrentUser = Depends(require_human_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """발급 — 사람 admin(JWT)만. 원문은 응답에서 1회만 반환."""
    if admin.is_service_token:
        raise ForbiddenError("Service tokens cannot issue new service tokens")
    if body.scopes:
        # AICM scoped integration 토큰 — legacy /aicm/v1 bootstrap 의 이전 경로.
        # scopes 없이 cidrs/rate_limit 만 오는 조합은 422 (아래에서 검증).
        from app.services.integration_token_service import IntegrationTokenService

        isvc = IntegrationTokenService(request.app.state.service_token_service)
        iresp, scopes, cidrs = await isvc.issue(
            session, name=body.name, created_by=admin.user_id, scopes=body.scopes,
            allowed_cidrs=body.allowed_cidrs, rate_limit_per_min=body.rate_limit_per_min,
            expiry_days=body.expiry_days,
        )
        result = ServiceTokenCreateResponse(
            id=iresp.id, name=iresp.name, token_prefix=iresp.token_prefix,
            created_at=iresp.created_at, expires_at=iresp.expires_at, token=iresp.token,
            integration="aicm", scopes=scopes, allowed_cidrs=cidrs,
        )
    elif body.allowed_cidrs or body.rate_limit_per_min is not None:
        from app.core.exceptions import ValidationError

        raise ValidationError("allowed_cidrs/rate_limit_per_min 은 scopes 와 함께만 지정할 수 있습니다")
    else:
        svc = request.app.state.service_token_service
        result = await svc.issue(
            session, name=body.name, created_by=admin.user_id, expiry_days=body.expiry_days
        )
    # R16: 서비스 토큰 생명주기는 키 폐기·역할 변경과 같은 감사 대상 — 발급
    # 자체가 ADMIN 동급 자격증명 생성인데 audit_logs 행이 없었다.
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SERVICE_TOKEN_ISSUE",
        resource_type="ServiceToken",
        resource_id=str(result.id),
        changes={"after": {
            "name": body.name, "expiry_days": body.expiry_days,
            **({"integration": "aicm", "scopes": body.scopes,
                "allowed_cidrs": body.allowed_cidrs,
                "rate_limit_per_min": body.rate_limit_per_min}
               if body.scopes else {}),
        }},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return result


@router.get("", response_model=ServiceTokenListResponse)
async def list_service_tokens(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """목록 — prefix만, 원문 없음."""
    svc = request.app.state.service_token_service
    return await svc.list_tokens(session)


@router.post("/rotate", response_model=ServiceTokenCreateResponse)
async def rotate_service_token(
    request: Request,
    admin: CurrentUser = Depends(require_service_token_self),
    session: AsyncSession = Depends(get_db_session),
):
    """self-rotate — service token 호출자만. 자기 자신을 교체, 구 토큰 24h grace."""
    # ⚠️ 게이트는 require_service_token_self 다 — require_human_admin 으로 바꾸면
    #    아무도 호출할 수 없다(R32 회귀; test_service_token_rotate_gate 가 잡는다).
    #    대상은 항상 호출 토큰 자신이며 요청 본문/경로에서 id 를 받지 않는다 —
    #    한 svc 토큰이 다른 토큰을 rotate 하는 경로가 없다.
    svc = request.app.state.service_token_service
    # integration grant 가 있으면 ServiceTokenService.rotate 가 같은 scopes/CIDR/lineage 를
    # 새 토큰에 복사한다 — mirror rotate 후에도 AICM scope 와 svc- prefix 가 유지된다.
    result = await svc.rotate(
        session, token_id=admin.service_token_id, created_by=admin.user_id
    )
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SERVICE_TOKEN_ROTATE",
        resource_type="ServiceToken",
        resource_id=str(result.id),
        changes={"before": {"token_id": str(admin.service_token_id)},
                 "after": {"token_id": str(result.id)}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return result


@router.post("/{token_id}/end-grace", status_code=200)
async def end_service_token_grace(
    request: Request,
    token_id: uuid.UUID,
    admin: CurrentUser = Depends(require_human_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """rotate grace 만 조기 종료 — 후속 토큰은 건드리지 않는다.

    DELETE /{id} 가 계보 사슬 전체(자손+grace 조상)를 폐기하므로, 배포 완료된
    새 토큰을 살리며 옛 토큰의 grace 만 닫고 싶을 때 여기를 쓴다(R21 r4 N1)."""
    if admin.is_service_token:
        raise ForbiddenError("Service tokens cannot manage service tokens")
    svc = request.app.state.service_token_service
    ended, prev_revoked_at = await svc.end_grace(session, token_id=token_id)
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SERVICE_TOKEN_END_GRACE",
        resource_type="ServiceToken",
        resource_id=str(token_id),
        changes={
            "before": {
                "revoked_at": prev_revoked_at.isoformat() if prev_revoked_at else None
            },
            "after": {"grace_ended": ended},
        },
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"grace_ended": ended}


@router.delete("/{token_id}", status_code=204)
async def revoke_service_token(
    request: Request,
    token_id: uuid.UUID,
    admin: CurrentUser = Depends(require_human_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """폐기 — 사람 admin(JWT)만. 즉시 무효화.

    ⚠️ rotate 계보 **사슬 전체**(자손 + grace 창의 조상)를 폐기한다 —
    옛 토큰의 grace 만 끝내려면 POST /{id}/end-grace."""
    if admin.is_service_token:
        raise ForbiddenError("Service tokens cannot revoke service tokens")
    svc = request.app.state.service_token_service
    cascaded = await svc.revoke(session, token_id=token_id)
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SERVICE_TOKEN_REVOKE",
        resource_type="ServiceToken",
        resource_id=str(token_id),
        changes={"after": {"cascade_revoked": cascaded}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
