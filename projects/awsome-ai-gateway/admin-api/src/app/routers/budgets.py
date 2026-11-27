# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader, require_human_admin, require_human_admin_or_team_leader
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole
from app.models.budget import BudgetScope
from app.services.budget_service import BudgetService
from app.services.managed_fields import (
    FIELD_BUDGET,
    FIELD_DEFAULT_CAP,
    RESOURCE_TEAM,
    RESOURCE_USER,
    managed_info,
    managed_rows,
)
from app.schemas.budgets import (
    AllocateBudgetRequest,
    AppBudgetItem,
    AppGroupBudgetItem,
    AutoDowngradeConfigRequest,
    AutoDowngradeConfigResponse,
    BudgetConfigDetailResponse,
    BudgetSummaryResponse,
    EqualSplitRequest,
    SeedSpentRequest,
    SeedSpentResponse,
    SetBudgetRequest,
    SetDefaultCapRequest,
    TeamBudgetAllocation,
    TeamUserBudgetSummaryPage,
    UsageByAppItem,
    UsageByAppResponse,
    UserAppBudgetsResponse,
    UserAppGroupBudgetsResponse,
)

router = APIRouter(prefix="/admin/budgets", tags=["Budget Management"])


@router.put("/team/{team_id}", dependencies=[Depends(require_human_admin)])
async def set_team_budget(
    request: Request,
    team_id: uuid.UUID,
    body: SetBudgetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):

    svc: BudgetService = request.app.state.budget_service
    await svc.set_team_budget(
        session,
        team_id=team_id,
        data=body,
        actor=admin,
        confirm=body.confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.delete("/team/{team_id}", status_code=204, dependencies=[Depends(require_human_admin)])
async def delete_team_budget(
    request: Request,
    team_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    confirm: bool = Query(False),
    session: AsyncSession = Depends(get_db_session),
):
    """팀 예산 해제 — 팀 전원이 team_budget_unset 으로 차단되므로 confirm 필수(§3-1)."""
    svc: BudgetService = request.app.state.budget_service
    await svc.delete_team_budget(
        session,
        team_id=team_id,
        actor=admin,
        confirm=confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.put("/team/{team_id}/default-cap", dependencies=[Depends(require_human_admin)])
async def set_team_default_cap(
    request: Request,
    team_id: uuid.UUID,
    body: SetDefaultCapRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """팀 기본 유저 cap D 설정/해제(§3-2). value=null 이면 D 해제."""
    svc: BudgetService = request.app.state.budget_service
    value = await svc.set_team_default_cap(
        session,
        team_id=team_id,
        value=body.value,
        actor=admin,
        confirm=body.confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {
        "status": "ok",
        "default_user_cap_usd": str(value) if value is not None else None,
    }


@router.post("/team/{team_id}/equal-split", dependencies=[Depends(require_human_admin)])
async def equal_split_team_budget(
    request: Request,
    team_id: uuid.UUID,
    body: EqualSplitRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """균등분배 도우미 — D = floor_cent(T/N). clear_individual=true 면 개별 cap 초기화."""
    svc: BudgetService = request.app.state.budget_service
    return await svc.equal_split_team_budget(
        session,
        team_id=team_id,
        clear_individual=body.clear_individual,
        actor=admin,
        confirm=body.confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.put("/user/{user_id}", dependencies=[Depends(require_human_admin_or_team_leader)])
async def set_user_budget(
    request: Request,
    user_id: uuid.UUID,
    body: SetBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.set_user_budget(
        session,
        user_id=user_id,
        data=body,
        actor=user,
        confirm=body.confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.put("/spent", response_model=SeedSpentResponse, dependencies=[Depends(require_human_admin)])
async def seed_budget_spent(
    request: Request,
    body: SeedSpentRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Batch-inject absolute spent (USD) per scope for migration burn-rate continuation.

    Overwrites used_usd in DB + Redis. Idempotent. Per-item partial failure reported.
    """

    svc: BudgetService = request.app.state.budget_service
    return await svc.seed_spent(
        session,
        items=body.items,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.delete("/user/{user_id}", status_code=204, dependencies=[Depends(require_human_admin_or_team_leader)])
async def delete_user_budget(
    request: Request,
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    confirm: bool = Query(False),
    session: AsyncSession = Depends(get_db_session),
):
    """개인 예산 삭제 → 팀 기본 cap D 적용으로 전환. 하위 app_c 연쇄 삭제(§4-2/I-3).

    TEAM_LEADER 도 자기 팀 멤버의 개별 cap 을 해제할 수 있다(본인 제외)."""
    svc: BudgetService = request.app.state.budget_service
    await svc.delete_user_budget(
        session,
        user_id=user_id,
        actor=user,
        confirm=confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.put("/user/{user_id}/app/{client}", dependencies=[Depends(require_human_admin_or_team_leader)])
async def set_user_client_budget(
    request: Request,
    user_id: uuid.UUID,
    client: str,
    body: SetBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Set a per-app (client) budget for a user."""
    from fastapi import HTTPException

    svc: BudgetService = request.app.state.budget_service
    try:
        await svc.set_user_client_budget(
            session,
            user_id=user_id,
            client=client,
            data=body,
            actor=user,
            confirm=body.confirm,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok"}


@router.delete("/user/{user_id}/app/{client}", status_code=204, dependencies=[Depends(require_human_admin_or_team_leader)])
async def clear_user_client_budget(
    request: Request,
    user_id: uuid.UUID,
    client: str,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Clear (deactivate) the per-app budget for a user."""
    from fastapi import HTTPException

    svc: BudgetService = request.app.state.budget_service
    try:
        await svc.clear_user_client_budget(
            session,
            user_id=user_id,
            client=client,
            actor=user,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/user/{user_id}/apps", response_model=UserAppBudgetsResponse)
async def get_user_app_budgets(
    request: Request,
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Read active per-app (client) budgets for a user, for UI prefill."""

    svc: BudgetService = request.app.state.budget_service
    rows_out = await svc.get_user_app_budgets(session, user_id=user_id, actor=user)
    apps = [AppBudgetItem(**row) for row in rows_out]
    rows = await managed_rows(
        session, resource_type=RESOURCE_USER, resource_ids=[user_id],
        facets=[f"app-budget:{a.client}" for a in apps],
    )
    by_facet = {r.facet: r for r in rows}
    for app in apps:
        row = by_facet.get(f"app-budget:{app.client}")
        if row:
            app.managed_by = managed_info(row)
    has_total = await svc.has_user_total_budget(session, user_id=user_id)
    return UserAppBudgetsResponse(
        user_id=str(user_id), apps=apps, has_total_budget=has_total
    )


@router.get("/team/{team_id}/allocation", response_model=TeamBudgetAllocation | None)
async def get_team_allocation(
    request: Request,
    team_id: uuid.UUID,
    period: str | None = Query(None, description="YYYY-MM (defaults to current month)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    from app.core.usage_filters import current_kst_period

    svc: BudgetService = request.app.state.budget_service
    # KST 월(§59) — date.today() 는 pod TZ(UTC)라 월 경계 첫 9시간에 지난달이 된다.
    effective_period = period or current_kst_period()
    resp = await svc.get_team_allocation(
        session,
        team_id=team_id,
        period=effective_period,
        actor=user,
    )
    if resp is not None:
        ids = []
        for e in resp.entries:
            if e.target_type == "USER":
                try:
                    ids.append(uuid.UUID(e.target_id))
                except ValueError:
                    pass
        rows = await managed_rows(
            session, resource_type=RESOURCE_USER, resource_ids=ids, facets=[FIELD_BUDGET]
        ) if ids else []
        by_id = {r.resource_id: r for r in rows}
        for entry in resp.entries:
            if entry.target_type != "USER":
                continue
            try:
                row = by_id.get(uuid.UUID(entry.target_id))
            except ValueError:
                row = None
            if row:
                entry.managed_by = managed_info(row)
    return resp


@router.put("/team/{team_id}/allocate", dependencies=[Depends(require_human_admin_or_team_leader)])
async def allocate_team_budget(
    request: Request,
    team_id: uuid.UUID,
    body: AllocateBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.allocate_team_budget(
        session,
        team_id=team_id,
        data=body,
        actor=user,
        confirm=body.confirm,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.put("/user/{user_id}/app-group/{group}", dependencies=[Depends(require_human_admin_or_team_leader)])
async def set_user_app_group_budget(
    request: Request,
    user_id: uuid.UUID,
    group: str,
    body: SetBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """app-group budget upsert — 해당 앱의 per-app budget 과 min() 으로 gateway 집행."""
    svc: BudgetService = request.app.state.budget_service
    await svc.set_user_app_group_budget(
        session,
        user_id=user_id,
        group=group,
        data=body,
        actor=user,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.get("/user/{user_id}/app-groups", response_model=UserAppGroupBudgetsResponse)
async def get_user_app_group_budgets(
    request: Request,
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    rows = await svc.list_user_app_group_budgets(session, user_id=user_id, actor=user)
    mrows = await managed_rows(
        session, resource_type=RESOURCE_USER, resource_ids=[user_id],
        facets=[f"app-group-budget:{r.app_group}" for r in rows],
    )
    by_facet = {r.facet: r for r in mrows}
    return UserAppGroupBudgetsResponse(
        user_id=str(user_id),
        app_groups=[
            AppGroupBudgetItem(
                group=r.app_group,
                max_budget_usd=r.max_budget_usd,
                policy=r.policy,
                alert_thresholds=list(r.alert_thresholds or []),
                **({"managed_by": managed_info(by_facet[f"app-group-budget:{r.app_group}"])}
                   if f"app-group-budget:{r.app_group}" in by_facet else {}),
            )
            for r in rows
        ],
    )


@router.delete("/user/{user_id}/app-group/{group}", status_code=204, dependencies=[Depends(require_human_admin_or_team_leader)])
async def clear_user_app_group_budget(
    request: Request,
    user_id: uuid.UUID,
    group: str,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.clear_user_app_group_budget(
        session,
        user_id=user_id,
        group=group,
        actor=user,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.get("/usage-by-app", response_model=UsageByAppResponse)
async def get_usage_by_app(
    request: Request,
    period: str = Query(description="YYYY-MM"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """앱(client)별 청구 사용량 — usage_rollup 하이브리드 소스의 client_share 와 같은 정의."""
    from app.services import usage_reads, usage_rollup as ur

    dr = ur.month_range(period)
    teams = None
    if user.role == UserRole.TEAM_LEADER:
        # budget summary 와 같은 리더 범위 — 소속 팀 + leader_user_id 로 리드하는 팀.
        # 빈 집합을 RollupScope 에 넣으면 fail-closed ValueError(500)가 되므로,
        # 좁힐 팀을 해석할 수 없는 경우만 여기서 403으로 끊는다.
        from app.services.analytics_service import _leader_allowed_team_ids

        allowed = await _leader_allowed_team_ids(user, session)
        if not allowed:
            raise ForbiddenError(
                "Team leader has no team assigned — cannot scope usage by app"
            )
        teams = tuple(sorted(allowed))
    rows = await usage_reads.client_share(
        session,
        ur.RollupScope(start=dr.start, end=dr.end, teams=teams, billable_only=True),
    )
    return UsageByAppResponse(
        period=period,
        apps=[
            UsageByAppItem(
                client=r.client,
                used_usd=r.cost_usd,
                call_count=int(r.call_count or 0),
                ws_queries=int(r.ws_queries or 0),
                ws_cost_usd=r.ws_cost,
                ws_untracked=int(r.ws_untracked or 0),
            )
            for r in rows
        ],
    )


@router.get("/summary/team/{team_id}/users", response_model=TeamUserBudgetSummaryPage)
async def get_team_user_budget_summary(
    request: Request,
    team_id: uuid.UUID,
    period: str = Query(description="YYYY-MM"),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """팀 멤버 USER summary single-page 조회 — AICM 은 limit=10000 으로 한 페이지를 요청한다."""
    svc: BudgetService = request.app.state.budget_service
    result = await svc.get_budget_summary(
        session,
        redis=request.app.state.redis,
        scope="USER",
        target_id=None,
        period=period,
        actor=user,
    )
    rows = [item for item in result.summary if item.team_id == str(team_id)]
    page = rows[offset:offset + limit]
    return TeamUserBudgetSummaryPage(
        period=period,
        team_id=str(team_id),
        items=page,
        limit=limit,
        offset=offset,
        has_more=(offset + len(page)) < len(rows),
    )


@router.get("/summary", response_model=BudgetSummaryResponse)
async def get_budget_summary(
    request: Request,
    scope: str | None = None,
    target_id: uuid.UUID | None = None,
    period: str = Query(description="YYYY-MM"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    redis = request.app.state.redis
    resp = await svc.get_budget_summary(
        session,
        redis=redis,
        scope=scope,
        target_id=target_id,
        period=period,
        actor=user,
    )
    # managed_by 는 플래그 ON 에서만 채워진다 — OFF 이면 조회 없이 빈 목록.
    ids = list({uuid.UUID(i.target_id) for i in resp.summary})
    rows = await managed_rows(session, resource_ids=ids, facets=[FIELD_BUDGET, FIELD_DEFAULT_CAP]) if ids else []
    by_key = {(r.resource_type, r.resource_id, r.facet): r for r in rows}
    for item in resp.summary:
        rtype = RESOURCE_USER if item.target_type == "USER" else RESOURCE_TEAM
        rid = uuid.UUID(item.target_id)
        row = by_key.get((rtype, rid, FIELD_BUDGET))
        if row:
            item.managed_by = managed_info(row)
        row = by_key.get((rtype, rid, FIELD_DEFAULT_CAP))
        if row:
            item.default_cap_managed_by = managed_info(row)
    return resp


def _parse_scope(value: str) -> BudgetScope:
    """scope 경로 파라미터를 대소문자 무관하게 enum 으로 — 무효 값은 400.

    ``scope_id: uuid.UUID`` 시그니처 선언은 FastAPI 가 422 를 자동 반환하지만,
    scope 는 경로값이 소문자(team/user)고 enum 값이 대문자라 수동 파싱이 필요하다.
    """
    from fastapi import HTTPException

    try:
        return BudgetScope(value.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid scope: {value}")


@router.get("/{scope}/{scope_id}", response_model=BudgetConfigDetailResponse)
async def get_budget_config(
    request: Request,
    scope: str,
    scope_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """예산 설정 다이얼로그 prefill — 현재 policy/thresholds/D 를 돌려준다.

    summary 응답에는 enforcement 설정이 없어 다이얼로그가 기본값으로만 열렸고,
    금액만 고쳐 저장하면 정책이 조용히 리셋됐다(버그).
    """
    svc: BudgetService = request.app.state.budget_service
    scope_enum = _parse_scope(scope)
    resp = await svc.get_budget_config(
        session,
        scope=scope_enum,
        scope_id=scope_id,
        actor=user,
    )
    rtype = RESOURCE_USER if scope_enum == BudgetScope.USER else RESOURCE_TEAM
    rows = await managed_rows(
        session, resource_type=rtype, resource_ids=[scope_id],
        facets=[FIELD_BUDGET, FIELD_DEFAULT_CAP],
    )
    by_facet = {r.facet: r for r in rows}
    if FIELD_BUDGET in by_facet:
        resp.managed_by = managed_info(by_facet[FIELD_BUDGET])
    if FIELD_DEFAULT_CAP in by_facet:
        resp.default_cap_managed_by = managed_info(by_facet[FIELD_DEFAULT_CAP])
    return resp


@router.get("/{scope}/{scope_id}/downgrade", response_model=AutoDowngradeConfigResponse)
async def get_downgrade_config(
    request: Request,
    scope: str,
    scope_id: uuid.UUID,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    return await svc.get_downgrade_config(
        session,
        scope=_parse_scope(scope),
        scope_id=scope_id,
        actor=user,
    )


@router.put("/{scope}/{scope_id}/downgrade", response_model=AutoDowngradeConfigResponse, dependencies=[Depends(require_human_admin)])
async def set_downgrade_config(
    request: Request,
    scope: str,
    scope_id: uuid.UUID,
    body: AutoDowngradeConfigRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    return await svc.set_downgrade_config(
        session,
        scope=_parse_scope(scope),
        scope_id=scope_id,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )


@router.delete("/{scope}/{scope_id}/downgrade", status_code=204, dependencies=[Depends(require_human_admin)])
async def delete_downgrade_config(
    request: Request,
    scope: str,
    scope_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.delete_downgrade_config(
        session,
        scope=_parse_scope(scope),
        scope_id=scope_id,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
