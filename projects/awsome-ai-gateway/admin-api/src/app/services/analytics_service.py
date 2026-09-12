# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import csv
import io
import re
import uuid
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.models.usage import ROIScope
from app.repositories.analytics_repository import AnalyticsRepository
from app.schemas.analytics import (
    AnalyticsResponse,
    CostSummary,
    ModelBreakdown,
    TeamBreakdown,
    TrendItem,
    UsageByUserItem,
    UsageByUserModelItem,
    UsageByUserModelResponse,
    UsageByUserResponse,
    UserBreakdown,
)

logger = structlog.get_logger()

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_period_date(period: str, date: str) -> None:
    """period=YYYY-MM, date=YYYY-MM-DD, and date must be within period's month."""
    if not _PERIOD_RE.match(period):
        raise ValidationError(f"Invalid period format: {period}. Expected YYYY-MM")
    if not _DATE_RE.match(date):
        raise ValidationError(f"Invalid date format: {date}. Expected YYYY-MM-DD")
    # Calendar validity (Codex review): the regex accepts 2026-06-31 etc., which
    # then fails at the PostgreSQL date cast with an opaque 500. Reject up front.
    import datetime as _dt
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise ValidationError(f"Invalid calendar date: {date}")
    if date[:7] != period:
        raise ValidationError(f"date {date} must fall within period {period}")


class AnalyticsService:
    async def get_analytics(
        self,
        session: AsyncSession,
        *,
        period: str,
        group_by: str = "model",
        scope: str = "all",
        actor: CurrentUser,
    ) -> AnalyticsResponse:
        repo = AnalyticsRepository(session)

        # Determine scope filter
        roi_scope: ROIScope | None = None
        scope_id: uuid.UUID | None = None

        if scope.startswith("team:"):
            team_id = uuid.UUID(scope.split(":")[1])
            # TEAM_LEADER can only see own team
            if actor.role == UserRole.TEAM_LEADER and actor.team_id != team_id:
                raise ForbiddenError("Team leaders can only view analytics for their own team")
            roi_scope = ROIScope.TEAM
            scope_id = team_id
        elif scope == "all":
            # TEAM_LEADER restricted to own team
            if actor.role == UserRole.TEAM_LEADER:
                # ⚠️ 팀이 없는 TEAM_LEADER 를 통과시키면 안 된다. 예전엔 scope_id 가
                #    None 이 되고, 아래 모든 WHERE 가 `if scope_id`/`is not None` 가드
                #    뒤에 있어서 **전부 사라졌다** → 전사 분석(비용·사용자·모델·추이)과
                #    /admin/analytics/export CSV 가 그대로 나갔다. 도달 경로: JWT 에
                #    team_id 클레임이 없으면(core/auth.py:157) 또는 auth.users.team_id
                #    가 NULL 이면(nullable) team_id 는 None 이다. dev 토큰은 role 을
                #    본문에서 읽고 team_id 를 항상 None 으로 만들기 때문에
                #    `dev.{"role":"TEAM_LEADER"}` 하나로 재현된다.
                if actor.team_id is None:
                    raise ForbiddenError(
                        "Team leader has no team assigned — cannot scope analytics. "
                        "Ask an administrator to assign a team."
                    )
                roi_scope = ROIScope.TEAM
                scope_id = actor.team_id
        # ADMIN: no restriction

        # 불변식: 비-GLOBAL scope 라면 scope_id 가 반드시 있다. 아래의 by_user/trends 는
        # `scope_id is not None` 로 격리를 걸기 때문에, 이 둘이 어긋나면 그 두 질의만
        # 조용히 전사로 넓어진다(repo 쪽은 이제 터진다). 한곳에서 못 박는다.
        # assert 를 쓰지 않는다 — python -O 로 사라지는 검사에 데이터 격리를 맡길 수 없다.
        if roi_scope not in (None, ROIScope.GLOBAL) and scope_id is None:
            raise ForbiddenError(
                f"Analytics scope isolation could not be applied (scope={roi_scope}) — "
                "refusing to return organization-wide data."
            )

        # Real-time aggregation from usage_logs (not pre-aggregated roi_aggregations)
        query_scope = roi_scope or ROIScope.GLOBAL

        cost_by_model = await repo.sum_usage_by_model(period, query_scope, scope_id)
        # ⚠️ 같은 scope 필터(_apply_scope_filter)를 재사용하는 repo 메서드로 뽑는다 —
        #    WHERE 를 손으로 다시 쓰면 TEAM_LEADER 격리가 갈라진다.
        requests_by_model = await repo.count_requests_by_model(period, query_scope, scope_id)
        total_cost = sum(cost_by_model.values(), Decimal("0"))
        active_users_count = await repo.count_active_users(period, query_scope, scope_id)
        total_requests_count = await repo.total_requests(period, query_scope, scope_id)
        total_tokens_count = await repo.total_tokens(period, query_scope, scope_id)

        avg_cost = total_cost / active_users_count if active_users_count > 0 else Decimal("0")

        cost_summary = CostSummary(
            total_requests=total_requests_count,
            total_tokens=total_tokens_count,
            total_cost_usd=total_cost,
            active_users=active_users_count,
            avg_cost_per_user_usd=avg_cost,
        )

        # requests 를 채운다 — 예전엔 기본값 0 이 그대로 나가서, Analytics 화면에서
        # 내려받는 JSON export 가 모든 모델에 대해 "요청 0건" 을 보고했다.
        by_model = [
            ModelBreakdown(
                model=model,
                cost_usd=cost,
                requests=requests_by_model.get(model, 0),
            )
            for model, cost in cost_by_model.items()
        ]

        # Team breakdown — aggregate per team from usage_logs
        by_team: list[TeamBreakdown] = []
        if not roi_scope or roi_scope == ROIScope.GLOBAL:
            # (예전엔 여기서 sum_usage_by_model 을 team_costs 로 받아놓고 한 번도 읽지
            #  않았다 — group_by=team 요청마다 전체 테이블 집계를 낭비했으므로 제거.)
            from sqlalchemy import distinct, func, select
            from app.models.auth import Team
            from app.models.usage import UsageLog
            from app.core.usage_filters import cost_period_filter
            # ⚠️ team 라벨에 UUID 를 넣지 말 것 — 차트 x축에 그대로 노출된다.
            #    INNER JOIN 이 안전한 근거: usage_logs.team_id 는 NOT NULL + auth.teams.id
            #    FK (app/models/usage.py) 이므로 조인으로 사라지는 행이 없다(합계 불변).
            stmt = select(
                UsageLog.team_id,
                Team.name.label("team_name"),
                func.sum(UsageLog.cost_usd).label("cost"),
                func.count(distinct(UsageLog.user_id)).label("users"),
            ).join(
                Team, Team.id == UsageLog.team_id
            ).where(
                cost_period_filter(period),  # §59 SUCCESS + KST (team 귀속은 usage_logs.team_id 직접)
            ).group_by(UsageLog.team_id, Team.name)
            result = await session.execute(stmt)
            for row in result:
                if row.team_id:
                    by_team.append(TeamBreakdown(
                        team=row.team_name or str(row.team_id),
                        team_id=str(row.team_id),
                        cost_usd=row.cost or Decimal("0"),
                        active_users=row.users or 0,
                    ))

        # User breakdown — group_by='user' 요청 시만 집계(불필요 조인 회피). §60.9:
        # 그간 UI 에 '사용자별' 옵션은 있었으나 백엔드가 group_by 무시 → by_model 표시되던
        # 버그 수정. usage_logs SUCCESS+KST(cost_period_filter) + User 조인, PII(sso_subject)
        # 미노출(display_name·email 만). 상위 50명(차트 가독).
        by_user: list[UserBreakdown] = []
        if group_by == "user":
            from sqlalchemy import func, select
            from app.models.usage import UsageLog
            from app.models.auth import User
            from app.core.usage_filters import cost_period_filter

            user_where = [cost_period_filter(period)]
            if scope_id is not None:  # TEAM_LEADER/team scope 격리
                user_where.append(UsageLog.team_id == scope_id)
            ustmt = (
                select(
                    User.display_name.label("name"),
                    User.email.label("email"),
                    func.sum(UsageLog.cost_usd).label("cost"),
                    func.count().label("requests"),
                )
                .join(User, User.id == UsageLog.user_id)
                .where(*user_where)
                .group_by(User.id, User.display_name, User.email)
                .order_by(func.sum(UsageLog.cost_usd).desc())
                .limit(50)
            )
            for row in (await session.execute(ustmt)).all():
                by_user.append(UserBreakdown(
                    user=row.name,
                    email=row.email,
                    cost_usd=row.cost or Decimal("0"),
                    requests=row.requests or 0,
                ))

        # 비용 추이 — KST 일 버킷(§59). 예전엔 trends 를 아예 대입하지 않아서 기본값 []
        # 이 나갔고, 대시보드/Analytics 의 두 추이 차트가 옆 KPI 는 실제 금액을 보여주는
        # 동안 영구히 "데이터 없음" 을 렌더했다.
        # ⚠️ scope_id 격리는 by_user 와 **동일 규칙**으로. 이걸 빼면 TEAM_LEADER 가
        #    전사 일별 비용을 받아 가는 권한 누출이 된다.
        from sqlalchemy import func, select

        from app.core.usage_filters import cost_period_filter
        from app.models.usage import UsageLog

        _kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))
        trend_where = [cost_period_filter(period)]
        if scope_id is not None:
            trend_where.append(UsageLog.team_id == scope_id)
        trend_stmt = (
            select(
                _kst_day.label("day"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("requests"),
            )
            .where(*trend_where)
            .group_by(_kst_day)
            .order_by(_kst_day)
        )
        trends = [
            TrendItem(
                date=str(r.day),  # 'YYYY-MM-DD' — CostTrendCard 가 slice(5) 로 잘라 쓴다
                cost_usd=r.cost_usd or Decimal("0"),
                requests=r.requests or 0,
            )
            for r in (await session.execute(trend_stmt)).all()
        ]

        return AnalyticsResponse(
            period=period,
            cost_summary=cost_summary,
            by_model=by_model,
            by_team=by_team,
            by_user=by_user,
            trends=trends,
        )

    async def export_analytics(
        self,
        session: AsyncSession,
        *,
        format: str,
        period: str,
        group_by: str,
        actor: CurrentUser,
    ) -> tuple[str, str]:
        """Returns (content, content_type)."""
        response = await self.get_analytics(
            session, period=period, group_by=group_by, scope="all", actor=actor
        )

        if format == "csv":
            return self._to_csv(response), "text/csv"
        else:
            return response.model_dump_json(indent=2), "application/json"

    async def get_usage_by_user_model(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserModelResponse:
        """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only)."""
        from sqlalchemy import func, select

        from app.core.usage_filters import kst_day_range_filter
        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                UsageLog.model_alias.label("model_alias"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("calls"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
                func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label(
                    "cache_creation_tokens"
                ),
                func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
                Team.id.label("team_id"),
                Team.name.label("team_name"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
            )
            .select_from(UsageLog)
            .outerjoin(User, User.id == UsageLog.user_id)
            .outerjoin(Team, Team.id == User.team_id)
            .outerjoin(Department, Department.id == Team.dept_id)
            .where(
                UsageLog.status == UsageStatus.SUCCESS,
                # ⚠️ `date(timezone('Asia/Seoul', requested_at)) >= date(:start)` 형태는
                #    좌변이 컬럼에 함수를 씌운 표현식이라 requested_at 인덱스를 못 타고
                #    usage_logs 를 전부 훑는다(월 필터를 cost_period_filter 로 고친 것과
                #    같은 이유). 경계를 파라미터 쪽에서 UTC 반개구간으로 환산하면 컬럼이
                #    그대로 남아 인덱스를 탄다. 집합은 동일하므로 숫자는 움직이지 않는다:
                #    KST 일자 ∈ [start, date] ⟺ requested_at ∈ [KST start, KST date+1).
                kst_day_range_filter(period_start, date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                UsageLog.model_alias,
                Team.id,
                Team.name,
                Department.id,
                Department.name,
            )
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        rows = (await session.execute(stmt)).all()

        items = [
            UsageByUserModelItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                model_alias=r.model_alias,
                cost_usd=r.cost_usd,
                calls=r.calls,
                input_tokens=r.input_tokens or 0,
                output_tokens=r.output_tokens or 0,
                cache_read_tokens=r.cache_read_tokens or 0,
                cache_write_tokens=r.cache_creation_tokens or 0,
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
                avg_latency_ms=round(float(r.avg_latency_ms or 0)),
            )
            for r in rows
        ]
        return UsageByUserModelResponse(period=period, date=date, items=items)

    async def get_usage_by_user(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserResponse:
        """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
        from sqlalchemy import func, select

        from app.core.usage_filters import kst_day_range_filter
        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("calls"),
                Team.id.label("team_id"),
                Team.name.label("team_name"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
            )
            .select_from(UsageLog)
            .outerjoin(User, User.id == UsageLog.user_id)
            .outerjoin(Team, Team.id == User.team_id)
            .outerjoin(Department, Department.id == Team.dept_id)
            .where(
                UsageLog.status == UsageStatus.SUCCESS,
                # ⚠️ `date(timezone('Asia/Seoul', requested_at)) >= date(:start)` 형태는
                #    좌변이 컬럼에 함수를 씌운 표현식이라 requested_at 인덱스를 못 타고
                #    usage_logs 를 전부 훑는다(월 필터를 cost_period_filter 로 고친 것과
                #    같은 이유). 경계를 파라미터 쪽에서 UTC 반개구간으로 환산하면 컬럼이
                #    그대로 남아 인덱스를 탄다. 집합은 동일하므로 숫자는 움직이지 않는다:
                #    KST 일자 ∈ [start, date] ⟺ requested_at ∈ [KST start, KST date+1).
                kst_day_range_filter(period_start, date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                Team.id,
                Team.name,
                Department.id,
                Department.name,
            )
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        rows = (await session.execute(stmt)).all()

        items = [
            UsageByUserItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                cost_usd=r.cost_usd,
                calls=r.calls,
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
            )
            for r in rows
        ]
        return UsageByUserResponse(period=period, date=date, items=items)

    @staticmethod
    def _to_csv(data: AnalyticsResponse) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["period", "model", "cost_usd"])
        for item in data.by_model:
            writer.writerow([data.period, item.model, str(item.cost_usd)])
        return output.getvalue()
