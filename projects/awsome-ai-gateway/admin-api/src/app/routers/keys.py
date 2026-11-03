# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import KeyStatus, UserRole
from app.repositories._cursor import encode_cursor
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.models.budget import BudgetScope
from app.schemas.common import (
    PAGE_LIMIT_DEFAULT,
    PAGE_LIMIT_MAX,
    PAGE_LIMIT_MIN,
    PaginationMeta,
)
from app.schemas.keys import (
    IssueServiceKeyRequest,
    KeyCountResponse,
    KeyListResponse,
)
from app.services.key_service import VK_KIND_SERVICE

router = APIRouter(prefix="/admin/keys", tags=["Key Management"])


@router.post("", response_model=dict, status_code=201)
async def issue_service_key(
    request: Request,
    body: IssueServiceKeyRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정에 장기 VK 발급 — `kind='service'`.

    대상은 ``is_service_account`` 인 계정뿐이다. 사람 계정에 열면 "관리자가
    임의 유저에게 장기 키"로 SSO 캡 의도를 우회하므로 계정 표지로 구조적
    차단한다. 재발급은 유저당 ACTIVE 1개 규칙상 기존 키를 즉시 만료시킨다 —
    UI 는 이를 발급 전 경고로 띄워야 한다.
    """
    from app.services.key_service import KeyService

    settings = get_settings()
    max_days = settings.APP_KEY_MAX_TTL_DAYS
    ttl_days = body.ttl_days if body.ttl_days is not None else max_days
    if not 1 <= ttl_days <= max_days:
        raise ValidationError(
            f"ttl_days must be between 1 and {max_days} (APP_KEY_MAX_TTL_DAYS)"
        )

    user = await UserRepository(session).get_user(body.user_id)
    if user is None:
        raise NotFoundError("User", str(body.user_id))
    if not user.is_service_account:
        raise ValidationError(
            "target user is not a service account — long-lived keys are only "
            "issuable to accounts flagged via /admin/service-accounts"
        )
    if user.role == UserRole.ADMIN:
        raise ValidationError("cannot issue keys for ADMIN users")
    if not user.is_active:
        raise ValidationError("service account is deactivated")

    # 팀+TEAM 예산 없으면 발급해도 매 호출이 429(no_team_assigned /
    # team_budget_unset) — 죽은 키를 건네지 말고 발급 시점에 차단한다.
    if user.team_id is None:
        raise ValidationError(
            "service account has no team — assign a team with a TEAM budget first"
        )
    team_budget = await BudgetRepository(session).get_active_config(
        BudgetScope.TEAM, user.team_id
    )
    if team_budget is None or team_budget.max_budget_usd is None:
        raise ValidationError(
            "service account's team has no active TEAM budget "
            "(gateway would return 429 team_budget_unset)"
        )

    key_service: KeyService = request.app.state.key_service
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    key_result = await key_service.issue_key(
        session,
        user_id=user.id,
        actor=admin,
        expires_at=expires_at,
        kind=VK_KIND_SERVICE,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
        user=user,
    )
    return {
        "key_id": key_result.key_id,
        "key_prefix": key_result.key_prefix,
        "user_id": key_result.user_id,
        "status": key_result.status,
        "expires_at": key_result.expires_at.isoformat() if key_result.expires_at else None,
        "virtual_key": key_result.virtual_key,
    }


@router.get("", response_model=KeyListResponse)
async def list_keys(
    request: Request,
    user_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    status: KeyStatus | None = None,
    email: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = Query(
        PAGE_LIMIT_DEFAULT,
        ge=PAGE_LIMIT_MIN,
        le=PAGE_LIMIT_MAX,
        description="페이지 크기. 경계를 벗어나면 422 — 예전엔 무제한이라 ?limit=1000000 이 테이블 전체를 끌어왔고 ?limit=0/-1 은 PostgreSQL 이 거부해 500 이 됐다.",
    ),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.key_service import KeyService

    svc: KeyService = request.app.state.key_service
    email_q = email.strip() if email else None
    items, has_more = await svc.list_keys(
        session,
        user_id=user_id if user_id else None,
        team_id=team_id if team_id else None,
        status=status,
        email=email_q or None,
        kind=kind,
        cursor=cursor,
        limit=limit,
    )
    next_cursor = (
        encode_cursor(items[-1].created_at, uuid.UUID(items[-1].key_id))
        if (items and has_more)
        else None
    )
    return KeyListResponse(
        items=items,
        pagination=PaginationMeta(cursor=next_cursor, limit=limit, has_more=has_more),
    )


@router.get("/count", response_model=KeyCountResponse)
async def count_keys(
    request: Request,
    user_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    status: KeyStatus | None = None,
    email: str | None = None,
    actor: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """대시보드 '활성 API Key' KPI 등이 사용. TEAM_LEADER 는 team_id 쿼리 파라미터를
    무시하고 본인 팀으로 고정(다른 팀 키 개수를 조회하는 것을 막는다)."""
    from app.services.key_service import KeyService

    svc: KeyService = request.app.state.key_service
    email_q = email.strip() if email else None

    scoped_team_id: uuid.UUID | None = None
    scoped_team_ids: list[uuid.UUID] | None = None
    if actor.role == UserRole.TEAM_LEADER:
        # 리더가 볼 수 있는 팀 = 소속 팀 + leader_user_id 로 리드하는 팀들.
        # actor.team_id 만 쓰면 리드하는 다른 팀의 키가 count 에서 빠진다
        # (analytics 의 _leader_allowed_team_ids 와 같은 규칙, R25-N7).
        from app.services.analytics_service import _leader_allowed_team_ids

        allowed = await _leader_allowed_team_ids(actor, session)
        if not allowed:
            return KeyCountResponse(count=0)
        scoped_team_ids = list(allowed)
    else:
        scoped_team_id = team_id if team_id else None

    count = await svc.count_keys(
        session,
        user_id=user_id if user_id else None,
        team_id=scoped_team_id,
        team_ids=scoped_team_ids,
        status=status,
        email=email_q or None,
    )
    return KeyCountResponse(count=count)


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    request: Request,
    key_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.key_service import KeyService

    svc: KeyService = request.app.state.key_service
    await svc.revoke_key(
        session,
        key_id=key_id,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
