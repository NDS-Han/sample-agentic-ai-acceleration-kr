# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""웹 검색 통제 API — 월 검색 수 한도(사용자·팀 공용 풀) + 요청당 상한.

**검색 수 기준이며 예산과 별개다.** 한도에 닿아도 모델 호출은 막히지 않는다(게이트웨이가
web_search 도구만 빼는 soft block). 변경은 gateway-proxy 정책 캐시(30초) 뒤에 반영된다.

감사 로그: SET_WEB_SEARCH_QUOTA(월 한도 변경), SET_WEB_SEARCH_REQUEST_CAP(요청당 상한 변경),
CLEAR_WEB_SEARCH_QUOTA(행 삭제 — 상속으로 복귀).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin, require_human_admin
from app.core.db import get_analytics_session, get_db_session
from app.core.exceptions import NotFoundError
from app.core.usage_filters import current_kst_period
from app.models.auth import Team, User
from app.schemas.web_search_quota import (
    WebSearchPolicyUpdate,
    WebSearchQuotaEffective,
    WebSearchQuotaOverview,
)
from app.services.web_search_quota_service import ORG, TEAM, USER, WebSearchQuotaService

router = APIRouter(prefix="/admin/web-search-quotas", tags=["Web Search Quotas"])


async def _require_target(session: AsyncSession, scope: str, scope_id: uuid.UUID | None) -> None:
    if scope == TEAM:
        if (await session.execute(select(Team.id).where(Team.id == scope_id))).first() is None:
            raise NotFoundError("Team", str(scope_id))
    elif scope == USER:
        if (await session.execute(select(User.id).where(User.id == scope_id))).first() is None:
            raise NotFoundError("User", str(scope_id))


def _resource_id(scope: str, scope_id: uuid.UUID | None) -> str:
    return f"{scope}:{scope_id}" if scope_id else scope


async def _audit(request: Request, session: AsyncSession, admin: CurrentUser, action: str,
                 scope: str, scope_id: uuid.UUID | None, changes: dict) -> None:
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action=action,
        resource_type="WebSearchPolicy",
        resource_id=_resource_id(scope, scope_id),
        changes=changes,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


async def _put(request: Request, session: AsyncSession, admin: CurrentUser, scope: str,
               scope_id: uuid.UUID | None, body: WebSearchPolicyUpdate) -> dict:
    await _require_target(session, scope, scope_id)
    svc = WebSearchQuotaService(session)
    before, after = await svc.upsert(
        scope, scope_id, monthly_query_limit=body.monthly_query_limit,
        per_request_cap=body.per_request_cap, actor_id=admin.user_id,
    )
    prev = before or {"monthly_query_limit": None, "per_request_cap": None}
    # 바뀐 축마다 감사 행 하나 — 한도와 상한은 의미가 다른 조작이다. 새 행이면 둘 다 남긴다.
    if before is None or prev["monthly_query_limit"] != after["monthly_query_limit"]:
        await _audit(request, session, admin, "SET_WEB_SEARCH_QUOTA", scope, scope_id, {
            "before": {"monthly_query_limit": prev["monthly_query_limit"]},
            "after": {"monthly_query_limit": after["monthly_query_limit"]},
        })
    if before is None or prev["per_request_cap"] != after["per_request_cap"]:
        await _audit(request, session, admin, "SET_WEB_SEARCH_REQUEST_CAP", scope, scope_id, {
            "before": {"per_request_cap": prev["per_request_cap"]},
            "after": {"per_request_cap": after["per_request_cap"]},
        })
    await session.commit()
    return {"scope": scope, "scope_id": str(scope_id) if scope_id else None, **after}


async def _delete(request: Request, session: AsyncSession, admin: CurrentUser, scope: str,
                  scope_id: uuid.UUID | None) -> dict:
    before = await WebSearchQuotaService(session).delete(scope, scope_id)
    if before is None:
        raise NotFoundError("WebSearchPolicy", _resource_id(scope, scope_id))
    await _audit(request, session, admin, "CLEAR_WEB_SEARCH_QUOTA", scope, scope_id,
                 {"before": before, "after": None})
    await session.commit()
    return {"scope": scope, "scope_id": str(scope_id) if scope_id else None, "cleared": True}


@router.get("", response_model=WebSearchQuotaOverview)
async def get_overview(
    period: str = Query(default=None, description="YYYY-MM (리포팅 TZ). 미지정 시 현재 월"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """조직 기본·팀 공용 풀(전 팀)·개인 덮어쓰기 + 이번 달 사용량."""
    return await WebSearchQuotaService(session).overview(period or current_kst_period())


@router.get("/users/{user_id}/effective", response_model=WebSearchQuotaEffective)
async def get_user_effective(
    user_id: uuid.UUID,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User", str(user_id))
    return await WebSearchQuotaService(session).effective_for_user(
        user.id, user.team_id, current_kst_period())


@router.get("/teams/{team_id}/effective", response_model=WebSearchQuotaEffective)
async def get_team_effective(
    team_id: uuid.UUID,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    await _require_target(session, TEAM, team_id)
    return await WebSearchQuotaService(session).effective_for_team(team_id, current_kst_period())


@router.put("/org", dependencies=[Depends(require_human_admin)])
async def put_org(request: Request, body: WebSearchPolicyUpdate,
                  admin: CurrentUser = Depends(require_admin),
                  session: AsyncSession = Depends(get_db_session)):
    """조직 기본 — monthly_query_limit = 사용자 1인 월 기본 한도, per_request_cap = 기본 상한."""
    return await _put(request, session, admin, ORG, None, body)


@router.put("/teams/{team_id}", dependencies=[Depends(require_human_admin)])
async def put_team(request: Request, team_id: uuid.UUID, body: WebSearchPolicyUpdate,
                   admin: CurrentUser = Depends(require_admin),
                   session: AsyncSession = Depends(get_db_session)):
    """팀 — monthly_query_limit = 팀 **공용 풀**(팀원 합계)."""
    return await _put(request, session, admin, TEAM, team_id, body)


@router.put("/users/{user_id}", dependencies=[Depends(require_human_admin)])
async def put_user(request: Request, user_id: uuid.UUID, body: WebSearchPolicyUpdate,
                   admin: CurrentUser = Depends(require_admin),
                   session: AsyncSession = Depends(get_db_session)):
    """개인 덮어쓰기 — 조직 기본 대신 이 사용자의 월 한도·요청당 상한."""
    return await _put(request, session, admin, USER, user_id, body)


@router.delete("/org", dependencies=[Depends(require_human_admin)])
async def delete_org(request: Request, admin: CurrentUser = Depends(require_admin),
                     session: AsyncSession = Depends(get_db_session)):
    return await _delete(request, session, admin, ORG, None)


@router.delete("/teams/{team_id}", dependencies=[Depends(require_human_admin)])
async def delete_team(request: Request, team_id: uuid.UUID,
                      admin: CurrentUser = Depends(require_admin),
                      session: AsyncSession = Depends(get_db_session)):
    return await _delete(request, session, admin, TEAM, team_id)


@router.delete("/users/{user_id}", dependencies=[Depends(require_human_admin)])
async def delete_user(request: Request, user_id: uuid.UUID,
                      admin: CurrentUser = Depends(require_admin),
                      session: AsyncSession = Depends(get_db_session)):
    return await _delete(request, session, admin, USER, user_id)
