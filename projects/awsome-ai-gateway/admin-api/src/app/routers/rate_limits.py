# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.schemas.rate_limits import (
    RateLimitResponse,
    RateLimitScopeStatus,
    RateLimitSetRequest,
    RateLimitTreeNode,
)

router = APIRouter(prefix="/admin/rate-limits", tags=["Rate Limit Management"])


@router.get("/tree", response_model=list[RateLimitTreeNode])
async def get_rate_limit_tree(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_rate_limit_tree(session)


@router.get("/usage/{scope}/{scope_id}")
async def get_rate_limit_usage(
    request: Request,
    scope: str,
    scope_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실시간 RPM 사용량(§60.9) — gateway-proxy 가 Redis 에 적재하는 sliding-window
    카운터를 읽어 현재 사용/모델별 분해 반환. 설정값(tree)과 별개의 라이브 상태.
    fail-soft: 조회 실패 시 {available:false}(화면 막지 않음)."""
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_live_usage(session, scope, scope_id)


@router.get("/usage-trend/{scope}/{scope_id}")
async def get_rate_limit_usage_trend(
    request: Request,
    scope: str,
    scope_id: str,
    window: str = "24h",
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """USER/TEAM 사용량 트렌드 — usage_logs 시간 버킷 집계.
    한도 설정의 근거 데이터로 rate-limits 패널이 사용. 창/버킷은 서비스의
    고정 화이트리스트(1h/6h/24h/7d)만 허용해 집계 비용을 상한 내에 둔다.
    fail-soft: 실패 시 {available:false}."""
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_usage_trend(session, scope, scope_id, window)


@router.put("/user/{user_id}", response_model=RateLimitResponse)
async def set_user_rate_limit(
    request: Request,
    user_id: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.rate_limit_service import RateLimitService

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.set_user_rate_limit(
        session,
        user_id=uuid.UUID(user_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/team/{team_id}", response_model=RateLimitResponse)
async def set_team_rate_limit(
    request: Request,
    team_id: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.set_team_rate_limit(
        session,
        team_id=uuid.UUID(team_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.get("/user/{user_id}", response_model=RateLimitScopeStatus)
async def get_user_rate_limit(
    request: Request,
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """단건 조회 — /users 패널용. own 없으면 팀 설정을 inherited 로 반환."""
    from app.models.model import RateLimitScope

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_rate_limit_status(
        session, scope=RateLimitScope.USER, scope_id=uuid.UUID(user_id)
    )


@router.get("/team/{team_id}", response_model=RateLimitScopeStatus)
async def get_team_rate_limit(
    request: Request,
    team_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.models.model import RateLimitScope

    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.get_rate_limit_status(
        session, scope=RateLimitScope.TEAM, scope_id=uuid.UUID(team_id)
    )


@router.delete("/user/{user_id}", status_code=204)
async def delete_user_rate_limit(
    request: Request,
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """개별 설정 해제 → 팀 상속 복귀. 설정이 없어도 멱등 성공(204)."""
    from app.models.model import RateLimitScope

    svc: RateLimitService = request.app.state.rate_limit_service
    await svc.delete_rate_limit(
        session,
        scope=RateLimitScope.USER,
        scope_id=uuid.UUID(user_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.delete("/team/{team_id}", status_code=204)
async def delete_team_rate_limit(
    request: Request,
    team_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """팀 설정 해제 → 제한 없음 복귀. 설정이 없어도 멱등 성공(204)."""
    from app.models.model import RateLimitScope

    svc: RateLimitService = request.app.state.rate_limit_service
    await svc.delete_rate_limit(
        session,
        scope=RateLimitScope.TEAM,
        scope_id=uuid.UUID(team_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/global/{model_alias}", response_model=RateLimitResponse)
async def set_global_rate_limit(
    request: Request,
    model_alias: str,
    body: RateLimitSetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: RateLimitService = request.app.state.rate_limit_service
    return await svc.set_global_rate_limit(
        session,
        model_alias=model_alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
