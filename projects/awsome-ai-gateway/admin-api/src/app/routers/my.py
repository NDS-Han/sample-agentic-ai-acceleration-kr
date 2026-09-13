# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db_session
from app.core.usage_filters import cost_period_filter, current_kst_period
from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.models.usage import UsageLog

router = APIRouter(prefix="/admin/my", tags=["My Usage"])


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

    limit_usd = float(config.max_budget_usd) if config else 0
    policy = config.policy.value if config else "HARD_BLOCK"

    usage_stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.user_id == user.user_id,
        cost_period_filter(period),  # §59 SUCCESS + KST
    )
    used_result = await session.execute(usage_stmt)
    used_usd = float(used_result.scalar_one())
    remaining = limit_usd - used_usd
    usage_pct = (used_usd / limit_usd * 100) if limit_usd > 0 else 0

    return {
        "user_id": str(user.user_id),
        "period": period,
        "budget": {
            "limit_usd": round(limit_usd, 2),
            "used_usd": round(used_usd, 4),
            "remaining_usd": round(remaining, 4),
            "usage_pct": round(usage_pct, 1),
            "policy": policy,
        },
    }


@router.get("/usage")
async def get_my_usage(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM"),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = current_kst_period()  # KST 월 — 아래 _kst_day 집계와 경계 통일

    _kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))
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

    model_stmt = (
        select(
            UsageLog.model_alias,
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
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    model_result = await session.execute(model_stmt)
    by_model = [
        {
            "model_alias": row.model_alias,
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
