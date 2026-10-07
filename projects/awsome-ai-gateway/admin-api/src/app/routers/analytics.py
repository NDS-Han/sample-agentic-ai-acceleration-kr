# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.db import get_db_session
from app.core.usage_filters import current_kst_period
from app.models.auth import UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["Analytics"])

#: /admin/analytics 응답 캐시 TTL(초). dashboard 와 같은 값이지만 정책이 달라 별도로 둔다
#: (여기는 ADMIN + scope='all' 에만 캐시한다 — 아래 get_analytics 주석 참조).
_ANALYTICS_CACHE_TTL = 30


def _analytics_cache_key(
    *,
    period: str,
    group_by: str,
    client: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """전사(ADMIN, scope='all') 응답 전용 캐시 키.

    ⚠️ scope 를 키에 넣지 않는다 — 이 키는 `scope='all'` 인 경우에만 쓰이므로 상수다.
    대신 **호출부가 role 을 검사**해 ADMIN 이 아니면 캐시를 아예 쓰지 않는다.
    키에 scope 만 넣고 role 을 빼면 TEAM_LEADER 가 ADMIN 의 전사 응답을 받는다.
    custom 날짜 구간은 키에 실어야 한다 — 빼면 월 질의와 구간 질의가 같은 키를 공유해
    TTL 안에 서로의 응답을 돌려준다.
    """
    normalized_client = "all" if client in (None, "", "all") else client
    range_part = f"{start_date or ''}~{end_date or ''}"
    return f"analytics:global:{period}:{range_part}:{group_by}:{normalized_client}"


async def _cache_get(request: Request, key: str):
    """캐시 조회 — 실패는 miss(fail-open). 캐시는 정합성의 근거가 아니다."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        if not raw:
            return None
        cached = json.loads(raw)
        # ⚠️ dict 가 아니면 깨진 엔트리다(예: pydantic 모델을 json.dumps(..., default=str)
        # 로 저장하면 str() repr 문자열이 들어간다). 문자열을 그대로 반환하면 UI 는
        # data.cost_summary == undefined 를 받아 페이지가 깨진다 — miss 로 처리해 재계산.
        return cached if isinstance(cached, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics cache get failed key=%s err=%s", key, exc)
        return None


async def _cache_set(request: Request, key: str, value: object) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        # jsonable_encoder 로 pydantic 모델→dict 를 먼저 펼쳐야 한다.
        # json.dumps(model, default=str) 는 모델 통째를 str() repr 로 저장해서
        # 캐시 적중 시 응답이 JSON 객체가 아니라 문자열이 된다.
        await redis.setex(key, _ANALYTICS_CACHE_TTL, json.dumps(jsonable_encoder(value)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics cache set failed key=%s err=%s", key, exc)



@router.get("")
async def get_analytics(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    # ⚠️ Literal 로 좁혀 잘못된 값이 422 로 실패하게 한다. 예전 description 은
    #    'department' 를 광고했지만 analytics_service 에는 그 분기가 없어서, 요청하면
    #    조용히 by_model 응답이 돌아왔다(쓰레기 값과 바이트 단위로 동일 = 무증상 오답).
    #    이 Literal 집합은 admin-ui/src/types/enums.ts 의 GroupByType 과 일치한다.
    group_by: Literal["model", "team", "user"] = Query("model"),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service

    # ⚠️ 캐시는 **ADMIN + scope='all'** 에만 적용한다. 그 조합만이 행위자와 무관한
    #    (전사) 응답이기 때문이다.
    #
    #    캐시를 걸면 안 되는 이유가 성능 트레이드오프가 아니라 **데이터 격리**다:
    #    이 엔드포인트는 require_admin_or_team_leader 이고, 같은 `scope='all'` 이
    #    ADMIN 에겐 GLOBAL·TEAM_LEADER 에겐 본인 팀으로 갈린다
    #    (analytics_service.get_analytics 의 role 분기). 그래서 키에 행위자를 넣지 않고
    #    `analytics:{period}:{group_by}:{scope}` 로 캐시하면, ADMIN 이 먼저 조회해
    #    채워둔 **전사 비용·사용자·모델·추이**를 TTL 안에 동일 파라미터로 요청한
    #    TEAM_LEADER 가 그대로 받는다 — ForbiddenError 격리와 위 fail-closed 불변식이
    #    통째로 무력화된다.
    #    ⇒ TEAM_LEADER 요청과 team: 범위 요청은 아예 캐시하지 않는다(cache_key=None).
    #      role 을 키에 섞는 방법도 있지만, TEAM_LEADER 는 팀마다 응답이 달라
    #      team_id 까지 넣어야 하고 그러면 적중률이 거의 0 이다 — 복잡도만 늘고 이득이 없다.
    cache_key = None
    if user.role == UserRole.ADMIN and scope == "all":
        cache_key = _analytics_cache_key(
            period=period, group_by=group_by, client=client,
            start_date=start_date, end_date=end_date,
        )
        if (cached := await _cache_get(request, cache_key)) is not None:
            return cached

    result = await svc.get_analytics(
        session,
        period=period,
        group_by=group_by,
        scope=scope,
        client=client,
        actor=user,
        start_date=start_date,
        end_date=end_date,
    )
    if cache_key is not None:
        await _cache_set(request, cache_key, result)
    return result


@router.get("/models")
async def get_model_cost_analytics(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM format"),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """모델별 상세 비용 — /analytics 페이지의 상세 섹션.

    overview(/admin/analytics)와 같은 필터셋(월 또는 custom 구간·client·scope·
    TEAM_LEADER 팀 격리)을 받는다 — 예전엔 period 만 받아서 별도 페이지였을 때는
    괜찮았지만, 같은 화면에 붙은 이상 필터가 다르면 카드와 표의 숫자가 어긋난다.
    집계·격리 로직은 AnalyticsService.get_model_cost_detail 이 단일 소스다.
    """
    from app.services.analytics_service import AnalyticsService

    if not period:
        period = current_kst_period()  # KST 월(§59) — pod TZ(UTC) 아님

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_model_cost_detail(
        session,
        period=period,
        actor=user,
        scope=scope,
        client=client,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/export")
async def export_analytics(
    request: Request,
    format: Literal["csv", "json"] = Query("csv"),
    period: str = Query(description="YYYY-MM format"),
    group_by: Literal["model", "team", "user"] = Query("model"),
    scope: str = Query("all"),
    client: str | None = Query(default=None),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: AnalyticsService = request.app.state.analytics_service
    content, content_type = await svc.export_analytics(
        session,
        format=format,
        period=period,
        group_by=group_by,
        scope=scope,
        client=client,
        actor=user,
        start_date=start_date,
        end_date=end_date,
    )

    filename = (
        f"analytics_{start_date}_{end_date}.{format}"
        if start_date and end_date
        else f"analytics_{period}.{format}"
    )
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/usage/by-user-model")
async def get_usage_by_user_model(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 메인 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user_model(session, period=period, date=date)


@router.get("/usage/by-user")
async def get_usage_by_user(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user(session, period=period, date=date)
