# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_session
from app.core.usage_filters import cost_period_filter, current_kst_period, kst_month_expr, reporting_tz_sql
from app.models.budget import BudgetConfig, BudgetScope
from app.models.model import ModelAlias
from app.models.usage import UsageLog

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

    usage_stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.user_id == user.user_id,
        cost_period_filter(period),  # §59 SUCCESS + KST
    )
    used_result = await session.execute(usage_stmt)
    used_usd = float(used_result.scalar_one())
    remaining = (limit_usd - used_usd) if limit_usd is not None else None
    usage_pct = (used_usd / limit_usd * 100) if limit_usd else None

    return {
        "user_id": str(user.user_id),
        "period": period,
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
        }
        for row in model_result.all()
    ]

    return {
        "user_id": str(user.user_id),
        "period": period,
        "daily_usage": daily_usage,
        "by_model": by_model,
    }
