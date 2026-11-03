# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import (
    Department,
    KeyStatus,
    OrgAllowedClient,
    Team,
    TeamAllowedClient,
    User,
    UserAllowedClient,
    UserRole,
    VirtualKey,
)
from app.models.budget import BudgetScope
from app.models.model import ModelAlias, ModelStatus
from app.models.usage import UsageLog
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.schemas.keys import (
    ServiceAccountCreateRequest,
    ServiceAccountItem,
    ServiceAccountListResponse,
    ServiceAccountReadiness,
)
from app.services.key_service import VK_KIND_SERVICE

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/service-accounts", tags=["Service Accounts"])


async def _readiness(session: AsyncSession, user: User) -> ServiceAccountReadiness:
    """발급 준비 상태 — POST /admin/keys 의 하드 블로커와 같은 판정을
    발급 전에 UI 에 노출한다(운영자는 문서가 아니라 화면을 본다).

    - team_budget_ok: TEAM 예산이 없으면 모든 호출이 429.
    - allowed_clients_clear: client 'other' 인 앱은 user/team/org 어느 계층의
      allowed_clients 화이트리스트에도 들어갈 수 없다 — 행이 하나라도
      있으면 전 요청이 400 이라 발급 자체가 무의미.
    - usable_models: allowed_clients IS NULL 인 ACTIVE alias 수 — 0 이면
      호출 가능한 모델이 없다.
    """
    team_budget_ok = False
    if user.team_id is not None:
        cfg = await BudgetRepository(session).get_active_config(
            BudgetScope.TEAM, user.team_id
        )
        team_budget_ok = cfg is not None and cfg.max_budget_usd is not None

    uac = (
        await session.execute(
            select(func.count())
            .select_from(UserAllowedClient)
            .where(UserAllowedClient.user_id == user.id)
        )
    ).scalar_one()
    allowed_clients_clear = uac == 0
    if allowed_clients_clear and user.team_id is not None:
        tac = (
            await session.execute(
                select(func.count())
                .select_from(TeamAllowedClient)
                .where(TeamAllowedClient.team_id == user.team_id)
            )
        ).scalar_one()
        if tac:
            allowed_clients_clear = False
        else:
            org_id = (
                await session.execute(
                    select(Department.org_id)
                    .select_from(Team)
                    .join(Department, Department.id == Team.dept_id)
                    .where(Team.id == user.team_id)
                )
            ).scalar_one_or_none()
            if org_id is not None:
                oac = (
                    await session.execute(
                        select(func.count())
                        .select_from(OrgAllowedClient)
                        .where(OrgAllowedClient.org_id == org_id)
                    )
                ).scalar_one()
                if oac:
                    allowed_clients_clear = False

    usable_models = (
        await session.execute(
            select(func.count())
            .select_from(ModelAlias)
            .where(
                ModelAlias.status == ModelStatus.ACTIVE,
                ModelAlias.allowed_clients.is_(None),
            )
        )
    ).scalar_one()

    return ServiceAccountReadiness(
        team_budget_ok=team_budget_ok,
        allowed_clients_clear=allowed_clients_clear,
        usable_models=usable_models,
    )


@router.get("", response_model=ServiceAccountListResponse)
async def list_service_accounts(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정 목록 + 발급 준비 상태 — /keys 페이지의 관리 패널."""
    rows = (
        await session.execute(
            select(User, Team.name)
            .outerjoin(Team, User.team_id == Team.id)
            .where(User.is_service_account.is_(True))
            .order_by(User.email)
        )
    ).all()

    items: list[ServiceAccountItem] = []
    for user, team_name in rows:
        vk = (
            await session.execute(
                select(VirtualKey)
                .where(
                    VirtualKey.user_id == user.id,
                    VirtualKey.status == KeyStatus.ACTIVE,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        last_req = (
            await session.execute(
                select(func.max(UsageLog.requested_at)).where(
                    UsageLog.user_id == user.id
                )
            )
        ).scalar_one()
        readiness = await _readiness(session, user)
        items.append(
            ServiceAccountItem(
                user_id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                team_id=str(user.team_id) if user.team_id else None,
                team_name=team_name,
                is_active=user.is_active,
                readiness=readiness,
                active_key_id=str(vk.id) if vk else None,
                active_key_prefix=vk.key_prefix if vk else None,
                active_key_expires_at=vk.expires_at if vk else None,
                last_request_at=last_req,
            )
        )

    return ServiceAccountListResponse(
        items=items, max_ttl_days=get_settings().APP_KEY_MAX_TTL_DAYS
    )


@router.post("", status_code=201)
async def mark_service_account(
    body: ServiceAccountCreateRequest,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """기존 사용자를 서비스 계정으로 표지 — 장기 VK 발급 대상이 된다.

    계정 자체는 Cognito/IdP 에서 생성·동기화된다(조직 규약). 이 엔드포인트는
    그 계정에 '서비스 계정' 표지만 단다. ADMIN 과 비활성 계정은 거부.
    """
    user = await UserRepository(session).get_by_email(body.email.strip())
    if user is None:
        raise NotFoundError("User", body.email)
    if user.is_service_account:
        return {
            "user_id": str(user.id),
            "email": user.email,
            "is_service_account": True,
        }
    if user.role == UserRole.ADMIN:
        raise ValidationError("cannot flag ADMIN users as service accounts")
    if not user.is_active:
        raise ValidationError("cannot flag a deactivated user")
    if user.id == admin.user_id:
        raise ValidationError("cannot flag yourself as a service account")

    user.is_service_account = True
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="MARK_SERVICE_ACCOUNT",
        resource_type="User",
        resource_id=str(user.id),
        changes={"after": {"email": user.email, "is_service_account": True}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {
        "user_id": str(user.id),
        "email": user.email,
        "is_service_account": True,
    }


@router.delete("/{user_id}")
async def unmark_service_account(
    user_id: uuid.UUID,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정 표지 해제 — 활성 service 키가 남아 있으면 거부한다."""
    user = await UserRepository(session).get_user(user_id)
    if user is None:
        raise NotFoundError("User", str(user_id))
    if not user.is_service_account:
        return {"user_id": str(user.id), "is_service_account": False}

    active = (
        await session.execute(
            select(func.count())
            .select_from(VirtualKey)
            .where(
                VirtualKey.user_id == user.id,
                VirtualKey.kind == VK_KIND_SERVICE,
                VirtualKey.status == KeyStatus.ACTIVE,
            )
        )
    ).scalar_one()
    if active:
        raise ValidationError(
            "revoke the active service key before unflagging this account"
        )

    user.is_service_account = False
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="UNMARK_SERVICE_ACCOUNT",
        resource_type="User",
        resource_id=str(user.id),
        changes={"after": {"email": user.email, "is_service_account": False}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"user_id": str(user.id), "is_service_account": False}


@router.post("/{user_id}/repair")
async def repair_service_account_keys(
    user_id: uuid.UUID,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """해당 계정의 ACTIVE service 키 ``key:vk:{hash}`` Redis 엔트리 재기록.

    gateway 의 VK→user 매핑은 Redis 가 유일한 소스 — 축출/failover 로
    유실되면 DB 에 유효한 키가 있어도 401 이다. 대시보드의 수동 복구 버튼
    (스케줄러가 주기적으로 같은 코드를 돌린다).
    """
    from app.services.key_service import KeyService

    user = await UserRepository(session).get_user(user_id)
    if user is None:
        raise NotFoundError("User", str(user_id))
    svc: KeyService = request.app.state.key_service
    repaired = await svc.repair_service_key_redis(session, user_id=user_id)
    return {"repaired": repaired}
