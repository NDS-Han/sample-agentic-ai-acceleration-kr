# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Human ADMIN release endpoint for AICM-managed fields."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin, require_human_admin
from app.core.db import get_db_session
from app.core.exceptions import NotFoundError, ValidationError
from app.models.integration import ExternalManagement
from app.services.managed_fields import STATE_MANAGED, STATE_RELEASED

router = APIRouter(prefix="/admin/managed-fields", tags=["AICM Managed Fields"])


class ReleaseManagedFieldRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class ReleaseManagedFieldResponse(BaseModel):
    status: str
    resource_type: str
    resource_id: str
    field: str


@router.post(
    "/{resource_type}/{resource_id}/{facet}/release",
    response_model=ReleaseManagedFieldResponse,
    dependencies=[Depends(require_human_admin)],
)
async def release_managed_field(
    request: Request,
    resource_type: str,
    resource_id: uuid.UUID,
    facet: str,
    body: ReleaseManagedFieldRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """사람 ADMIN 이 AICM 잠금 한 필드를 사유와 함께 해제한다.

    해제는 행 삭제가 아니라 ``released`` 상태다 — 이력과 사유가 남고, 다음 AICM 쓰기가
    같은 PK 를 upsert 하면서 자동으로 다시 잠근다.
    """
    if resource_type not in {"user", "team"}:
        raise ValidationError("resource_type must be 'user' or 'team'")
    if not facet or len(facet) > 96 or "/" in facet:
        raise ValidationError("invalid managed field facet")
    row = (
        await session.execute(
            select(ExternalManagement)
            .where(
                ExternalManagement.resource_type == resource_type,
                ExternalManagement.resource_id == resource_id,
                ExternalManagement.facet == facet,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.state != STATE_MANAGED:
        raise NotFoundError("AICM managed field lock", f"{resource_type}:{resource_id}:{facet}")

    now = datetime.now(timezone.utc)
    row.state = STATE_RELEASED
    row.released_at = now
    row.released_by = admin.user_id
    row.release_reason = body.reason
    row.updated_at = now
    await session.flush()
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="RELEASE_AICM_MANAGED_FIELD",
        resource_type="ExternalManagement",
        resource_id=f"{resource_type}:{resource_id}:{facet}",
        changes={
            "before": {"state": STATE_MANAGED, "managed_by": row.managed_by},
            "after": {"state": STATE_RELEASED, "reason": body.reason},
        },
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return ReleaseManagedFieldResponse(
        status="released", resource_type=resource_type, resource_id=str(resource_id), field=facet
    )
