# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""웹 검색 통제(검색 수 기준) 읽기·쓰기 — budget.web_search_policies.

해석 규칙은 gateway-proxy ``services/web_search_quota.resolve_policy`` 와 같아야 한다:
  - 개인 월 한도 = USER ?? ORG(사용자 1인 기본). TEAM 한도는 팀 **공용 풀**이라 상속하지 않는다.
  - 팀 공용 풀 = TEAM(없으면 무제한). 게이트웨이는 개인 한도와 팀 풀을 함께 확인한다.
  - 요청당 상한 = USER ?? TEAM ?? ORG ?? 기본(WEB_SEARCH_MAX_SEARCHES_PER_REQUEST, 5).

사용량은 ``usage_logs.web_search_count`` 의 리포팅 TZ 월 합계(상태 무관 — 게이트웨이 카운터와
같은 정의)를 하이브리드 롤업(services/usage_rollup)으로 읽는다. 워커 적재 지연(수 초)만큼
게이트웨이의 Redis 카운터보다 늦을 수 있다.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.auth import Department, Team, User
from app.models.budget import WebSearchPolicy
from app.services import usage_reads as reads
from app.services import usage_rollup as ur
from app.schemas.web_search_quota import (
    WebSearchPolicyRow,
    WebSearchQuotaEffective,
    WebSearchQuotaOverview,
    WebSearchQuotaTeamItem,
    WebSearchQuotaUserItem,
)

ORG, TEAM, USER = "ORG", "TEAM", "USER"


def default_per_request_cap() -> int:
    return int(getattr(get_settings(), "WEB_SEARCH_MAX_SEARCHES_PER_REQUEST", 5) or 5)


def resolve(rows: dict[str, WebSearchPolicy], *, include_user: bool, period: str,
            default_cap: int) -> WebSearchQuotaEffective:
    org, team, user = rows.get(ORG), rows.get(TEAM), rows.get(USER)
    if include_user and user is not None and user.monthly_query_limit is not None:
        limit, limit_src = user.monthly_query_limit, "user"
    elif include_user and org is not None and org.monthly_query_limit is not None:
        limit, limit_src = org.monthly_query_limit, "organization"
    else:
        limit, limit_src = None, "none"
    cap, cap_src = default_cap, "default"
    for src, r in (("user", user if include_user else None), ("team", team),
                   ("organization", org)):
        if r is not None and r.per_request_cap is not None:
            cap, cap_src = r.per_request_cap, src
            break
    return WebSearchQuotaEffective(
        period=period,
        monthly_limit=limit,
        monthly_limit_source=limit_src,
        team_pool_limit=team.monthly_query_limit if team is not None else None,
        per_request_cap=cap,
        per_request_cap_source=cap_src,
    )


class WebSearchQuotaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rows(self, user_id: uuid.UUID | None, team_id: uuid.UUID | None
                    ) -> dict[str, WebSearchPolicy]:
        conds = [WebSearchPolicy.scope == ORG]
        if team_id is not None:
            conds.append(and_(WebSearchPolicy.scope == TEAM, WebSearchPolicy.scope_id == team_id))
        if user_id is not None:
            conds.append(and_(WebSearchPolicy.scope == USER, WebSearchPolicy.scope_id == user_id))
        rows = (await self.session.execute(select(WebSearchPolicy).where(or_(*conds)))).scalars()
        return {r.scope: r for r in rows}

    async def _sum(self, column: str, scope_id: uuid.UUID, period: str) -> int:
        rng = ur.month_range(period)
        scope = (ur.RollupScope.of(rng, users=[scope_id]) if column == "user"
                 else ur.RollupScope.of(rng, teams=[scope_id]))
        return await reads.ws_sum(self.session, scope)

    async def effective_for_user(self, user_id: uuid.UUID, team_id: uuid.UUID | None,
                                 period: str) -> WebSearchQuotaEffective:
        eff = resolve(await self._rows(user_id, team_id), include_user=True, period=period,
                      default_cap=default_per_request_cap())
        eff.used = await self._sum("user", user_id, period)
        if eff.monthly_limit is not None:
            eff.remaining = max(0, eff.monthly_limit - eff.used)
        if team_id is not None:
            eff.team_used = await self._sum("team", team_id, period)
            if eff.team_pool_limit is not None:
                eff.team_remaining = max(0, eff.team_pool_limit - eff.team_used)
        else:
            eff.team_pool_limit = None
        return eff

    async def effective_for_team(self, team_id: uuid.UUID, period: str
                                 ) -> WebSearchQuotaEffective:
        eff = resolve(await self._rows(None, team_id), include_user=False, period=period,
                      default_cap=default_per_request_cap())
        eff.team_used = await self._sum("team", team_id, period)
        if eff.team_pool_limit is not None:
            eff.team_remaining = max(0, eff.team_pool_limit - eff.team_used)
        return eff

    async def overview(self, period: str) -> WebSearchQuotaOverview:
        policies = list((await self.session.execute(select(WebSearchPolicy))).scalars())
        org = next((p for p in policies if p.scope == ORG), None)
        team_rows = {p.scope_id: p for p in policies if p.scope == TEAM}
        user_rows = {p.scope_id: p for p in policies if p.scope == USER}
        rng = ur.month_range(period)
        team_used = await reads.ws_by_team(self.session, ur.RollupScope.of(rng))
        org_used = int(sum(int(v or 0) for v in team_used.values()))
        teams = (await self.session.execute(
            select(Team.id, Team.name, Department.name)
            .outerjoin(Department, Department.id == Team.dept_id)
            .order_by(Team.name)
        )).all()
        team_items = [
            WebSearchQuotaTeamItem(
                team_id=str(tid), team_name=name, dept_name=dept,
                monthly_query_limit=team_rows[tid].monthly_query_limit if tid in team_rows else None,
                per_request_cap=team_rows[tid].per_request_cap if tid in team_rows else None,
                used=int(team_used.get(tid) or 0),
            )
            for tid, name, dept in teams
        ]

        user_items: list[WebSearchQuotaUserItem] = []
        if user_rows:
            ids = list(user_rows)
            used = await reads.ws_by_user(self.session, ur.RollupScope.of(rng, users=ids))
            users = (await self.session.execute(
                select(User.id, User.email, User.display_name, Team.name)
                .outerjoin(Team, Team.id == User.team_id)
                .where(User.id.in_(ids)).order_by(User.email)
            )).all()
            user_items = [
                WebSearchQuotaUserItem(
                    user_id=str(uid), email=email, display_name=dn, team_name=tn,
                    monthly_query_limit=user_rows[uid].monthly_query_limit,
                    per_request_cap=user_rows[uid].per_request_cap,
                    used=int(used.get(uid) or 0),
                )
                for uid, email, dn, tn in users
            ]

        return WebSearchQuotaOverview(
            period=period,
            default_per_request_cap=default_per_request_cap(),
            org=(WebSearchPolicyRow(monthly_query_limit=org.monthly_query_limit,
                                    per_request_cap=org.per_request_cap,
                                    updated_at=org.updated_at) if org else None),
            org_used=org_used,
            teams=team_items,
            users=user_items,
        )

    async def get_row(self, scope: str, scope_id: uuid.UUID | None) -> WebSearchPolicy | None:
        cond = (WebSearchPolicy.scope_id.is_(None) if scope_id is None
                else WebSearchPolicy.scope_id == scope_id)
        return (await self.session.execute(
            select(WebSearchPolicy).where(WebSearchPolicy.scope == scope, cond)
        )).scalar_one_or_none()

    async def upsert(self, scope: str, scope_id: uuid.UUID | None, *,
                     monthly_query_limit: int | None, per_request_cap: int | None,
                     actor_id: uuid.UUID | None) -> tuple[dict | None, dict]:
        """행을 통째로 바꾼다. (이전 값 또는 None, 새 값) 을 돌려준다."""
        row = await self.get_row(scope, scope_id)
        before = (None if row is None else
                  {"monthly_query_limit": row.monthly_query_limit,
                   "per_request_cap": row.per_request_cap})
        if row is None:
            row = WebSearchPolicy(scope=scope, scope_id=scope_id)
            self.session.add(row)
        row.monthly_query_limit = monthly_query_limit
        row.per_request_cap = per_request_cap
        row.updated_by = actor_id
        await self.session.flush()
        return before, {"monthly_query_limit": monthly_query_limit,
                        "per_request_cap": per_request_cap}

    async def delete(self, scope: str, scope_id: uuid.UUID | None) -> dict | None:
        row = await self.get_row(scope, scope_id)
        if row is None:
            return None
        before = {"monthly_query_limit": row.monthly_query_limit,
                  "per_request_cap": row.per_request_cap}
        await self.session.delete(row)
        await self.session.flush()
        return before
