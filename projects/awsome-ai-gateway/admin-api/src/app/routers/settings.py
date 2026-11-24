# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""전역 런타임 설정 API.

이 레포의 다른 전역 스위치는 모두 env 전용이라 파드 롤링이 필요하다. 여기 있는 것들은
DB 에 저장되고 즉시 적용된다(``public.system_settings``, migration 0036).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import (
    CurrentUser,
    forbid_service_token_reader,
    require_admin,
    require_human_admin,
)
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError
from app.repositories.system_settings_repository import SystemSettingsRepository
from app.schemas.settings import (
    BodyLoggingSetting,
    BodyLoggingUpdateRequest,
    WebSearchPricingSetting,
    WebSearchPricingUpdateRequest,
)
from app.services.web_search_cost import (
    DEFAULT_PRICE_PER_1K,
    PRICING_KEY,
    get_price,
    get_tracking_since,
    web_search_deployed,
)

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
    return BodyLoggingSetting(
        enabled=enabled,
        provisioned=bool(get_settings().BODY_LOG_S3_BUCKET),
    )


@router.put(
    "/body-logging",
    response_model=BodyLoggingSetting,
    # svc 토큰은 못 켠다 — 탈취된 장수명 토큰이 전사 프롬프트/응답 원문 수집을
    # 여는 것 자체가 권한 확장이라 사람 admin 만 허용(읽기 가드와 같은 경계).
    dependencies=[Depends(forbid_service_token_reader)],
)
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
    # 인프라 미프로비저닝(features.body_logging=false 또는 수동 env 부재)이면
    # 켜기를 거부한다 — 런타임 플래그만 켜도 Firehose/버킷이 없어 수집 경로가
    # 없다. 끄기는 항상 허용한다(안전한 방향 + 꼬인 ON 상태 정리 경로).
    if body.enabled and not get_settings().BODY_LOG_S3_BUCKET:
        raise ForbiddenError(
            "body logging infrastructure is not provisioned for this "
            "deployment (BODY_LOG_S3_BUCKET unset — features.body_logging)"
        )

    svc = request.app.state.system_settings_service
    enabled = await svc.set_body_logging_enabled(
        session, enabled=body.enabled, actor_id=admin.user_id
    )

    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SET_BODY_LOGGING",
        resource_type="SystemSetting",
        resource_id="body_logging_enabled",
        changes={"after": {"enabled": enabled}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )

    await session.commit()
    await svc.mirror_body_logging_to_cache(enabled)
    return BodyLoggingSetting(
        enabled=enabled,
        provisioned=bool(get_settings().BODY_LOG_S3_BUCKET),
    )


async def _web_search_pricing(session: AsyncSession) -> WebSearchPricingSetting:
    price, is_default, row = await get_price(session)
    return WebSearchPricingSetting(
        price_per_1k_queries=price,
        default_price_per_1k_queries=DEFAULT_PRICE_PER_1K,
        is_default=is_default,
        updated_at=row.updated_at if row is not None else None,
        tracking_since=await get_tracking_since(session),
        deployed=web_search_deployed(),
    )


@router.get("/web-search-pricing", response_model=WebSearchPricingSetting)
async def get_web_search_pricing(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """AgentCore Web Search 단가(USD/1,000 queries). 행이 없으면 AWS 정가 $7.00.

    **모니터링 전용** — 이 단가로 계산한 비용은 예산·HARD_BLOCK·CPM/CPH 어디에도
    들어가지 않는다(usage_logs.web_search_cost_usd, cost_usd 와 별개).
    """
    return await _web_search_pricing(session)


@router.put(
    "/web-search-pricing",
    response_model=WebSearchPricingSetting,
    dependencies=[Depends(require_human_admin)],
)
async def set_web_search_pricing(
    request: Request,
    body: WebSearchPricingUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """단가 변경 — 이후 **기록되는** 사용분부터 적용된다(워커 캐시 최대 30초).

    이미 기록된 행의 비용은 바뀌지 않는다(행마다 기록 시점 단가로 고정). 그래서 이력은
    행 자체와 ``audit.audit_logs`` 의 before/after 가 남긴다 — system_settings 한 셀은
    다음 변경이 덮어쓴다.
    """
    before, before_default, _ = await get_price(session)
    new_price = body.price_per_1k_queries
    await SystemSettingsRepository(session).upsert(
        PRICING_KEY,
        # 문자열로 저장 — JSON number 는 float 왕복에서 6.1 → 6.0999… 가 될 수 있다.
        {"price_per_1k_queries": str(new_price)},
        updated_by=admin.user_id,
    )
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SET_WEB_SEARCH_PRICING",
        resource_type="SystemSetting",
        resource_id=PRICING_KEY,
        changes={
            "before": {"price_per_1k_queries": str(before), "is_default": before_default},
            "after": {"price_per_1k_queries": str(new_price)},
        },
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    await session.commit()
    return await _web_search_pricing(session)
