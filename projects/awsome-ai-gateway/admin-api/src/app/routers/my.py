# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_session
from app.core.usage_filters import cost_period_filter, current_kst_period, kst_month_expr, reporting_tz_sql
from app.models.auth import Team
from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.models.model import ModelAlias
from app.models.usage import UsageLog, UsageStatus

router = APIRouter(prefix="/admin/my", tags=["My Usage"])


@router.get("/profile")
async def get_my_profile(user: CurrentUser = Depends(get_current_user)):
    """로그인 사용자의 **유효 신원**(role 포함)을 돌려준다.

    admin-ui 는 OIDC 콜백에서 IdP id_token 을 `admin_jwt` 로 굽는데, IdP 토큰에는
    `role` 클레임이 없다 — 그래서 UI 의 권한 게이트(middleware/Sidebar)가 역할을
    모른 채 판정해야 했다. 이 엔드포인트는 get_current_user 와 **동일한 판정**
    (DB role + ADMIN_EMAILS/ADMIN_GROUPS 병합) 결과를 주므로, 콜백이 로그인 시점에
    여기를 한 번 물어 유효 역할을 보조 쿠키로 구워 둔다.

    인증만 요구한다 — 자기 자신의 신원을 돌려줄 뿐이라 역할 제한은 없다.
    """
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "role": user.role.value,
        "team_id": str(user.team_id) if user.team_id else None,
    }


@router.get("/budget")
async def get_my_budget(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    # KST 월(§59) — date.today() 는 pod TZ(UTC)라 매월 1일 첫 9시간에 지난달이 된다.
    period = current_kst_period()

    # ⚠️ `client IS NULL` 이 load-bearing 이다. per-app(앱별) 예산 config 가 **같은
    #    테이블**에 살기 때문에, 이 필터가 없으면 `created_at DESC LIMIT 1` 이 나중에
    #    만들어진 앱별 행을 집는다. 그러면 사용자 화면의 "내 개인 월예산" 칸에 그 앱의
    #    한도(예: $200)가 뜨는데 used_usd 는 **총 사용액**이라, remaining 과 usage_pct
    #    가 둘 다 틀린다 — 사용자는 아직 여유가 있는데 소진됐다고 보거나 그 반대가 된다.
    #    이 레포의 리포지토리 계층은 이미 같은 필터를 걸고 있었다
    #    (repositories/budget_repository.py) — 이 라우터만 빠져 있었다.
    stmt = (
        select(BudgetConfig)
        .where(
            BudgetConfig.scope == BudgetScope.USER,
            BudgetConfig.scope_id == user.user_id,
            BudgetConfig.is_active.is_(True),
            BudgetConfig.client.is_(None),
        )
        .order_by(BudgetConfig.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    config = result.scalar_one_or_none()

    # 개인 예산 config 없음(팀 예산/D-cap 적용)은 null 로 구분한다 — 예전엔 0/기본
    # 정책으로 내려가 UI 가 "한도 $0.00·잔액 -$x·HARD_BLOCK" 처럼 차단된 것처럼
    # 보여줬다. null 이면 프론트가 "팀 예산 적용" 표시로 전환한다.
    limit_usd = float(config.max_budget_usd) if config else None
    policy = config.policy.value if config else None

    # 사용액은 enforcement 계수(budget_usages)와 같은 행에서 읽는다 — usage_logs
    # SUCCESS 합계는 실패 호출 비용과 시드 이관분을 빼먹어, 하드블록 상태인 사용자에게
    # "잔액 있음"을 보여주는 불일치가 있었다.
    usage_stmt = select(func.coalesce(func.sum(BudgetUsage.used_usd), 0)).where(
        BudgetUsage.scope == BudgetScope.USER,
        BudgetUsage.scope_id == user.user_id,
        BudgetUsage.period == period,
        BudgetUsage.client.is_(None),
    )
    used_result = await session.execute(usage_stmt)
    used_usd = float(used_result.scalar_one())
    remaining = (limit_usd - used_usd) if limit_usd is not None else None
    usage_pct = (used_usd / limit_usd * 100) if limit_usd else None

    # 팀 공용 풀 — 개인 한도(A_u 또는 팀 기본 D)와 무관하게 T 가 소진되면
    # team_budget_unset/한도초과로 요청이 차단되므로 "내 실효 한도"의 절반이다.
    # 사용액은 개인과 같은 enforcement 계수(budget_usages, TEAM scope).
    # T 미설정(config 행 없음 또는 max NULL)은 limit=null 로 내려 프론트가
    # "차단 가능" 상태로 표시한다 — 실제로 그 상태에서 게이트웨이는 429 를 돌린다.
    team = None
    if user.team_id is not None:
        team_cfg_stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == BudgetScope.TEAM,
                BudgetConfig.scope_id == user.team_id,
                BudgetConfig.is_active.is_(True),
                BudgetConfig.client.is_(None),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        team_cfg = (await session.execute(team_cfg_stmt)).scalar_one_or_none()
        team_used_stmt = select(func.coalesce(func.sum(BudgetUsage.used_usd), 0)).where(
            BudgetUsage.scope == BudgetScope.TEAM,
            BudgetUsage.scope_id == user.team_id,
            BudgetUsage.period == period,
            BudgetUsage.client.is_(None),
        )
        team_used = float((await session.execute(team_used_stmt)).scalar_one())
        team_name = (
            await session.execute(select(Team.name).where(Team.id == user.team_id))
        ).scalar_one_or_none()

        t_limit = float(team_cfg.max_budget_usd) if team_cfg and team_cfg.max_budget_usd is not None else None
        t_cap = (
            float(team_cfg.default_user_cap_usd)
            if team_cfg and team_cfg.default_user_cap_usd is not None
            else None
        )
        t_remaining = (t_limit - team_used) if t_limit is not None else None
        t_pct = (team_used / t_limit * 100) if t_limit else None
        team = {
            "team_id": str(user.team_id),
            "team_name": team_name,
            "limit_usd": round(t_limit, 2) if t_limit is not None else None,
            "used_usd": round(team_used, 4),
            "remaining_usd": round(t_remaining, 4) if t_remaining is not None else None,
            "usage_pct": round(t_pct, 1) if t_pct is not None else None,
            # 개인 A_u 가 없을 때 본인에게 적용되는 팀 기본 상한(D). 개인 한도가
            # 있어도 내려준다 — 표시 판단은 프론트.
            "default_cap_usd": round(t_cap, 2) if t_cap is not None else None,
        }

    return {
        "user_id": str(user.user_id),
        "period": period,
        "team": team,
        "budget": {
            "limit_usd": round(limit_usd, 2) if limit_usd is not None else None,
            "used_usd": round(used_usd, 4),
            "remaining_usd": round(remaining, 4) if remaining is not None else None,
            "usage_pct": round(usage_pct, 1) if usage_pct is not None else None,
            "policy": policy,
        },
    }


@router.get("/periods")
async def get_my_periods(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """본인 사용량이 존재하는 월(YYYY-MM) 목록 — 최신순.

    /my 페이지의 기간 선택기용. /admin/dashboard/periods 는 require_admin 이라
    DEVELOPER·TEAM_LEADER 가 쓸 수 없어(§PAGE_PERMISSIONS /my 는 비-ADMIN 도
    허용) 같은 쿼리를 본인 스코프로 내린다.
    """
    period_expr = kst_month_expr()
    stmt = (
        select(distinct(period_expr).label("period"))
        .where(UsageLog.user_id == user.user_id)
        .order_by(period_expr.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {"periods": [p for p in rows if p]}


@router.get("/usage")
async def get_my_usage(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = current_kst_period()  # KST 월 — 아래 _kst_day 집계와 경계 통일

    _kst_day = func.date(func.timezone(reporting_tz_sql(), UsageLog.requested_at))
    daily_stmt = (
        select(
            _kst_day.label("day"),
            func.sum(UsageLog.cost_usd).label("cost_usd"),
            func.count().label("requests"),
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ).label("tokens"),
        )
        .where(
            UsageLog.user_id == user.user_id,
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        .group_by(_kst_day)
        .order_by(_kst_day)
    )
    daily_result = await session.execute(daily_stmt)
    daily_usage = [
        {
            "date": str(row.day),
            "cost_usd": round(float(row.cost_usd), 4),
            "requests": row.requests,
            "tokens": row.tokens or 0,
        }
        for row in daily_result.all()
    ]

    # display_name 카탈로그 조인 — 대시보드/analytics 와 같은 표기 규칙(modelDisplay).
    # 미등록 alias 는 None → 프론트가 alias 로 fallback.
    model_stmt = (
        select(
            UsageLog.model_alias,
            func.max(ModelAlias.display_name).label("display_name"),
            func.sum(UsageLog.cost_usd).label("cost_usd"),
            func.count().label("requests"),
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ).label("tokens"),
            # 모델별 토큰 버킷 — summary 와 같은 정의. 모델별 캐시 사용 패턴이
            # 비용 차이를 설명하므로 분해해서 내린다.
            func.sum(UsageLog.input_tokens).label("input_tokens"),
            func.sum(UsageLog.output_tokens).label("output_tokens"),
            func.sum(UsageLog.cache_creation_tokens).label("cache_creation_tokens"),
            func.sum(UsageLog.cache_read_tokens).label("cache_read_tokens"),
        )
        .outerjoin(ModelAlias, ModelAlias.alias == UsageLog.model_alias)
        .where(
            UsageLog.user_id == user.user_id,
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    model_result = await session.execute(model_stmt)
    by_model = [
        {
            "model_alias": row.model_alias,
            "display_name": row.display_name,
            "cost_usd": round(float(row.cost_usd), 4),
            "requests": row.requests,
            "tokens": row.tokens or 0,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cache_creation_tokens": row.cache_creation_tokens or 0,
            "cache_read_tokens": row.cache_read_tokens or 0,
        }
        for row in model_result.all()
    ]

    # 토큰 버킷 요약 — agents 상세와 동일 정의(§59 SUCCESS + KST 기간).
    # reasoning_tokens 는 output 에 이미 포함(비청구 표시 지표)이므로
    # 프론트의 total 계산에서 제외 대상이다.
    # WHERE 는 SUCCESS 필터가 빠진 구간, 성공/에러 구분은 FILTER 절 — 한 스캔.
    # 정책 차단은 usage_logs 에 행이 없어 ERROR/TIMEOUT 은 장애만 잡힌다.
    _success = UsageLog.status == UsageStatus.SUCCESS
    summary_stmt = select(
        func.count().filter(_success).label("requests"),
        func.count()
        .filter(UsageLog.status.in_([UsageStatus.ERROR, UsageStatus.TIMEOUT]))
        .label("error_requests"),
        func.sum(UsageLog.cost_usd).filter(_success).label("cost_usd"),
        func.sum(UsageLog.input_tokens).filter(_success).label("input_tokens"),
        func.sum(UsageLog.output_tokens).filter(_success).label("output_tokens"),
        func.sum(UsageLog.cache_creation_tokens).filter(_success).label("cache_creation_tokens"),
        func.sum(UsageLog.cache_read_tokens).filter(_success).label("cache_read_tokens"),
        func.sum(UsageLog.reasoning_tokens).filter(_success).label("reasoning_tokens"),
        func.sum(UsageLog.web_search_count).filter(_success).label("web_search_count"),
    ).where(
        UsageLog.user_id == user.user_id,
        cost_period_filter(period, success_only=False),
    )
    srow = (await session.execute(summary_stmt)).one()

    summary = {
        "requests": srow.requests or 0,
        "error_requests": srow.error_requests or 0,
        "cost_usd": round(float(srow.cost_usd or 0), 4),
        "input_tokens": srow.input_tokens or 0,
        "output_tokens": srow.output_tokens or 0,
        "cache_creation_tokens": srow.cache_creation_tokens or 0,
        "cache_read_tokens": srow.cache_read_tokens or 0,
        "reasoning_tokens": srow.reasoning_tokens or 0,
        "web_search_count": srow.web_search_count or 0,
    }

    return {
        "user_id": str(user.user_id),
        "period": period,
        "summary": summary,
        "daily_usage": daily_usage,
        "by_model": by_model,
    }
