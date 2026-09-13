# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""전역 런타임 설정 API.

이 레포의 다른 전역 스위치는 모두 env 전용이라 파드 롤링이 필요하다. 여기 있는 것들은
DB 에 저장되고 즉시 적용된다(``public.system_settings``, migration 0036).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.schemas.settings import BodyLoggingSetting, BodyLoggingUpdateRequest

router = APIRouter(prefix="/admin/settings", tags=["System Settings"])


@router.get("/body-logging", response_model=BodyLoggingSetting)
async def get_body_logging(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """현재 전역 body-logging on/off. 행이 없으면 OFF."""
    svc = request.app.state.system_settings_service
    enabled = await svc.get_body_logging_enabled(session)
    return BodyLoggingSetting(enabled=enabled)


@router.put("/body-logging", response_model=BodyLoggingSetting)
async def set_body_logging(
    request: Request,
    body: BodyLoggingUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """전역 body-logging on/off. DB 영구 저장 + 커밋 후 Redis 미러링.

    ⚠️ 이 스위치를 켜면 게이트웨이가 **요청·응답 본문을 그대로** 수집하기 시작한다.
       사용자가 프롬프트에 붙여 넣은 것이 durable 저장소로 나간다는 뜻이다. 그래서
       ``audit.audit_logs`` 에 **불변 행**을 남긴다 — ``system_settings.updated_by``
       한 셀은 다음 변경이 덮어쓰므로 "누가 언제 켰나" 의 이력이 되지 못한다.

    ⚠️ Redis 미러링은 커밋 **뒤**다. 커밋 전에 쓰면 트랜잭션이 롤백될 때 DB 는 OFF 인데
       Redis 는 ON 이 되어, 아무도 켜지 않은 본문 수집이 돌아간다.
    """
    svc = request.app.state.system_settings_service
    enabled = await svc.set_body_logging_enabled(
        session, enabled=body.enabled, actor_id=admin.user_id
    )

    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        action="SET_BODY_LOGGING",
        resource_type="SystemSetting",
        resource_id="body_logging_enabled",
        changes={"after": {"enabled": enabled}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )

    await session.commit()
    await svc.mirror_body_logging_to_cache(enabled)
    return BodyLoggingSetting(enabled=enabled)
