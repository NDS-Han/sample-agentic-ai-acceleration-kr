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
from app.services import usage_reads as reads
from app.services import usage_rollup as ur
from app.services.web_search_cost import web_search_visible, ws_from_totals, ws_settings
from app.schemas.analytics import (
    AnalyticsResponse,
    CostSummary,
    ModelBreakdown,
    TeamBreakdown,
    TeamTrend,
    TokenBreakdown,
    TrendItem,
    UsageByUserItem,
    UsageByUserResponse,
    UserBreakdown,
)

logger = structlog.get_logger()

# fullmatch + [0-9]: `match`+`$` 는 끝 개행('2026-08\n')을 통과시키고,
# `\d` 는 아라비아 숫자('٢٠٢٦-٠٨', int() 가 파싱해버림)를 통과시킨다(R25-N2).
_PERIOD_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _validate_period_date(period: str, date: str) -> None:
    """period=YYYY-MM, date=YYYY-MM-DD, and date must be within period's month."""
    if not _PERIOD_RE.fullmatch(period):
        raise ValidationError(f"Invalid period format: {period}. Expected YYYY-MM")
    if not _DATE_RE.fullmatch(date):
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


async def _leader_allowed_team_ids(
    actor: CurrentUser, session: AsyncSession
) -> set[uuid.UUID]:
    """TEAM_LEADER 가 볼 수 있는 팀 집합 — 소속 팀 + leader_user_id 로 리드하는 팀들.

    auth.teams.leader_user_id 는 여러 팀이 같은 사용자를 가리킬 수 있다 —
    소속 팀 하나만 허용하면 리더가 자기가 리드하는 다른 팀을 영영 못 연다.
    """
    from sqlalchemy import select

    from app.models.auth import Team

    allowed: set[uuid.UUID] = set()
    if actor.team_id:
        allowed.add(actor.team_id)
    rows = await session.execute(
        select(Team.id).where(Team.leader_user_id == actor.user_id)
    )
    allowed.update(r[0] for r in rows.all())
    return allowed


async def _resolve_scope(
    actor: CurrentUser, scope: str, session: AsyncSession
) -> tuple[ROIScope | None, list[uuid.UUID] | None]:
    """scope 문자열 → (roi_scope, scope_ids) 해석 — get_analytics 와
    /admin/analytics/models 상세 질의가 **같은** 격리 규칙을 쓰게 단일화.

    Returns:
        roi_scope: TEAM/GLOBAL/None(None=GLOBAL 취급 — 호출부에서 or GLOBAL)
        scope_ids: None 이면 전사(제한 없음), list 면 team_id 필터 집합.

    ⚠️ **기본-거부**: `all`·`team:<uuid>` 외의 값은 400. 예전엔 매칭 안 되는
       임의 문자열이 (None, None) 으로 떨어져 호출부가 GLOBAL 로 처리했고,
       TEAM_LEADER 가 `scope=x` 로 전사 분석을 받아 가는 권한 누출이었다.
    """
    if not isinstance(scope, str) or not scope:
        raise ValidationError("scope must be 'all' or 'team:<uuid>'")

    if scope == "all":
        if actor.role == UserRole.TEAM_LEADER:
            # ⚠️ 팀이 없는 TEAM_LEADER 를 통과시키면 안 된다. scope_ids 가 비면
            #    모든 WHERE 가 가드 뒤에 있어서 전부 사라진다 → 전사 분석 누출.
            scope_ids = sorted(await _leader_allowed_team_ids(actor, session))
            if not scope_ids:
                raise ForbiddenError(
                    "Team leader has no team assigned — cannot scope analytics. "
                    "Ask an administrator to assign a team."
                )
            return ROIScope.TEAM, scope_ids
        return None, None  # ADMIN — 전사

    if scope.startswith("team:"):
        try:
            team_id = uuid.UUID(scope.split(":", 1)[1])
        except (ValueError, IndexError):
            # 잘못된 UUID 는 500 이 아니라 400 — 형식 오류는 입력 오류다.
            raise ValidationError(
                f"scope team id must be a UUID (got {scope!r})"
            ) from None
        if actor.role == UserRole.TEAM_LEADER:
            if team_id not in await _leader_allowed_team_ids(actor, session):
                raise ForbiddenError(
                    "Team leaders can only view analytics for their own teams"
                )
        return ROIScope.TEAM, [team_id]

    raise ValidationError(f"scope must be 'all' or 'team:<uuid>' (got {scope!r})")


class AnalyticsService:
    async def get_analytics(
        self,
        session: AsyncSession,
        *,
        period: str,
        group_by: str = "model",
        scope: str = "all",
        client: str | None = None,
        actor: CurrentUser,
        start_date: str | None = None,
        end_date: str | None = None,
        scope_ids: list[uuid.UUID] | None | object = ...,
    ) -> AnalyticsResponse:
        """Analytics 개요 — 하이브리드 롤업 읽기 2문장(합계·분해 + 일별 추이) + seed 잔차.

        숫자의 정의: 청구 대상(billable — 상태 무관, wave 3)·리포팅 tz·앱·요청 시점 팀. scope_ids 를
        넘기면(라우터가 캐시 키를 만들려고 이미 해석한 경우) 다시 해석하지 않는다.
        """
        # custom 날짜 구간 — 둘 다 있어야 적용. 한쪽만 오면 400 이다(조용한 월 fallback
        # 은 "날짜를 바꿔도 숫자가 안 변하는" 무증상 오답의 재탕). 형식·달력·순서·연 범위
        # 검증은 day_range_to_utc 가 한다(ValidationError → 400, 366일 초과 422).
        rng = ur.resolve_range(period, start_date, end_date)

        # Determine scope filter — 해석 규칙은 _resolve_scope 가 단일 소스
        # (/admin/analytics/models 도 같은 규칙을 공유한다).
        if scope_ids is ...:
            _roi_scope, scope_ids = await _resolve_scope(actor, scope, session)

        base = ur.RollupScope.of(rng, teams=scope_ids, client=client)
        t = await reads.totals(session, base)
        by_model_raw, by_team_raw, by_user_raw = await reads.analytics_overview(
            session, base, want_users=(group_by == "user"))
        day_scope = ur.RollupScope.of(rng, teams=scope_ids, client=client, by_day=True)
        trends_raw, team_day_raw = await reads.analytics_trends(session, day_scope)
        meta = ur.merge_meta(await ur.fetch_meta(session, base),
                             await ur.fetch_meta(session, day_scope))

        total_cost = t.total_cost
        active_users_count = t.active_users
        token_breakdown = TokenBreakdown(
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cache_read_tokens=t.cache_read_tokens,
            cache_write_tokens=t.cache_creation_tokens,
            total_tokens=t.total_tokens,
        )
        avg_cost = total_cost / active_users_count if active_users_count > 0 else Decimal("0")
        cost_summary = CostSummary(
            total_requests=t.total_requests,
            error_requests=t.error_requests,
            cancelled_requests=t.cancelled_requests,
            billable_requests=t.billable_requests,
            total_tokens=t.total_tokens,
            total_cost_usd=total_cost,
            active_users=active_users_count,
            avg_cost_per_user_usd=avg_cost,
            charged=t.charged(),
        )

        # 비용 내림차순(+ alias) — 같은 페이지의 상세 표(cost DESC)와 같은 순서·같은 팔레트 index.
        # requests 를 채운다(JSON export 가 "요청 0건" 을 보고하던 결함). display_name 은 카탈로그.
        by_model = [
            ModelBreakdown(model=m, cost_usd=Decimal(str(c)), requests=int(n), display_name=dn)
            for m, c, n, dn in by_model_raw
        ]

        # Team breakdown — 요청 시점 team_id 귀속. TEAM_LEADER 도 본인 팀(들)의 행은 본다.
        # ⚠️ team 라벨에 UUID 를 넣지 말 것 — 차트 x축에 그대로 노출된다.
        by_team = [
            TeamBreakdown(team=name or str(tid), team_id=str(tid), cost_usd=Decimal(str(c)),
                          active_users=int(u), dept_name=dept)
            for tid, name, dept, c, u in by_team_raw
        ]

        # User breakdown — group_by='user' 요청 시만(§60.9). PII(sso_subject) 미노출. 상위 50명.
        by_user: list[UserBreakdown] = []
        if group_by == "user":
            # ── 마이그레이션 주입분(seed) 반영 ──
            # `/usage/by-user` 와 **같은 공식**: seed = max(budget_usages 월 누적 − 월 실사용(전 status), 0).
            # 이 화면에 넣어야 예산 페이지(96% 소진)와 분석 화면이 같은 사실을 말한다. 순위도
            # seed 를 포함해야 "비용 상위 50명" 축이 맞는다.
            # ⚠️ 팀 격리: budget_usages 에는 team_id 가 없으므로 auth.users 로 조인해 거른다.
            # seed 잔차는 월 버킷 개념이라 custom 구간·client 필터에서는 실사용만 표시한다.
            from sqlalchemy import func, select

            from app.models.auth import User
            from app.models.budget import BudgetScope, BudgetUsage

            seeded: dict[uuid.UUID, tuple[str | None, str | None, Decimal]] = {}
            if not rng.is_custom and ur.norm_client(client) is None:
                # 월 실사용(전 status) — 팀 스코프를 **넣지 않는다**(budget_usages(USER) 는 사용자
                # 전체 카운터 — 월중 팀 이동 사용자의 A팀 시절 사용분이 잔차에 남지 않게).
                month_actual = await reads.cost_by_user_all_status(
                    session, ur.RollupScope.of(ur.month_range(period)))
                recorded_stmt = (
                    select(
                        User.id.label("user_id"),
                        User.display_name.label("name"),
                        User.email.label("email"),
                        func.coalesce(func.sum(BudgetUsage.used_usd), 0).label("used"),
                    )
                    .select_from(BudgetUsage)
                    .join(User, User.id == BudgetUsage.scope_id)
                    .where(
                        BudgetUsage.scope == BudgetScope.USER,
                        BudgetUsage.period == period,
                        # ⚠️ client IS NULL = 총합 행만(per-app 행은 부분집합).
                        BudgetUsage.client.is_(None),
                    )
                    .group_by(User.id, User.display_name, User.email)
                )
                if scope_ids:
                    recorded_stmt = recorded_stmt.where(User.team_id.in_(scope_ids))
                for r in (await session.execute(recorded_stmt)).all():
                    residual = Decimal(str(r.used)) - month_actual.get(r.user_id, Decimal("0"))
                    if residual > 0:
                        seeded[r.user_id] = (r.name, r.email, residual)

            for uid, name, email, c, n in by_user_raw or []:
                uid = uuid.UUID(str(uid))
                extra = seeded.pop(uid, None)
                by_user.append(UserBreakdown(
                    user=name, email=email, user_id=str(uid),
                    cost_usd=Decimal(str(c)) + (extra[2] if extra else Decimal("0")),
                    requests=int(n or 0),
                ))
            # 사용량 없이 seed 만 있는 사용자(이관 직후)도 차트에 나와야 한다.
            for seed_uid, (name, email, amount) in seeded.items():
                by_user.append(UserBreakdown(
                    user=name, email=email, user_id=str(seed_uid), cost_usd=amount, requests=0
                ))
            # seed 를 더한 뒤 다시 상위 50명 — SQL 순서는 실사용 기준이었다.
            by_user.sort(key=lambda b: b.cost_usd, reverse=True)
            del by_user[50:]

        # 비용 추이 — 리포팅 tz 일 버킷(§59). scope_ids 격리는 위와 같은 규칙.
        trends = [
            TrendItem(date=str(d), cost_usd=Decimal(str(c)), requests=int(n))
            for d, c, n in trends_raw
        ]
        # 팀별 추이 — trends 와 같은 범위·격리에 team 차원만 추가(팀 이름순 → 날짜순).
        _trend_rows: dict[str, TeamTrend] = {}
        for tid, name, dept, d, c, n in team_day_raw:
            tt = _trend_rows.get(tid)
            if tt is None:
                tt = _trend_rows[tid] = TeamTrend(team=name or str(tid), team_id=str(tid),
                                                  dept_name=dept)
            tt.points.append(TrendItem(date=str(d), cost_usd=Decimal(str(c)), requests=int(n)))
        trends_by_team = list(_trend_rows.values())

        # 웹 검색 — 같은 구간·scope·client, status 무관. 청구 총액에는 cost_summary.charged 로 들어간다.
        web_search = ws_from_totals(t.ws_queries, t.ws_cost, t.ws_untracked,
                                    await ws_settings(session))

        return AnalyticsResponse(
            # custom 구간이면 응답의 period 도 진짜 구간을 말한다(CSV/JSON export 파일명 근거).
            period=rng.label if rng.is_custom else period,
            cost_summary=cost_summary,
            by_model=by_model,
            by_team=by_team,
            by_user=by_user,
            trends=trends,
            trends_by_team=trends_by_team,
            token_breakdown=token_breakdown,
            web_search=web_search,
            data_as_of=meta.data_as_of,
            delayed_days=[d.isoformat() for d in meta.delayed_days],
        )

    async def get_model_cost_detail(
        self,
        session: AsyncSession,
        *,
        period: str,
        actor: CurrentUser,
        scope: str = "all",
        client: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """모델별 상세 비용 — /analytics 페이지의 상세 섹션 데이터.

        overview(get_analytics)와 같은 필터셋(기간·client·scope·역할 격리)을 받는다 — 한
        근원에서 나오지 않으면 같은 화면의 카드와 표가 서로 다른 집합을 보게 된다.
        """
        _roi_scope, scope_ids = await _resolve_scope(actor, scope, session)
        rng = ur.resolve_range(period, start_date, end_date)
        # 상세 표는 지연(전 호출)이 필요해 판정은 SQL 안에서 한다(usage_reads.model_detail).
        sc = ur.RollupScope.of(rng, teams=scope_ids, client=client)
        day_sc = ur.RollupScope.of(rng, teams=scope_ids, client=client, billable_only=True,
                                   by_day=True)
        model_rows = await reads.model_detail(session, sc)
        daily_rows = await reads.model_daily(session, day_sc)
        meta = ur.merge_meta(await ur.fetch_meta(session, sc), await ur.fetch_meta(session, day_sc))

        models = []
        for row in model_rows:
            # ⚠️ 분자(total_cost_usd)와 분모(total_tokens)의 버킷이 같아야 한다 — 캐시를 빼면
            #    캐시를 많이 쓰는 모델의 단가가 부풀어 순위가 뒤집혀 보였다. reasoning_tokens 는
            #    output_tokens 에 이미 포함이라 더하지 않는다.
            total_tokens = (
                (row.input_tokens or 0)
                + (row.output_tokens or 0)
                + (row.cache_creation_tokens or 0)
                + (row.cache_read_tokens or 0)
            )
            cost_per_1k = (float(row.total_cost_usd) / total_tokens * 1000) if total_tokens > 0 else 0
            models.append({
                "model_alias": row.model_alias,
                "display_name": row.display_name,
                "request_count": int(row.request_count),
                "total_cost_usd": round(float(row.total_cost_usd), 4),
                "input_tokens": int(row.input_tokens),
                "output_tokens": int(row.output_tokens),
                "cache_read_tokens": int(row.cache_read_tokens),
                "cache_creation_tokens": int(row.cache_creation_tokens),
                "avg_latency_ms": round(float(row.avg_latency_ms or 0)),
                "cost_per_1k_tokens": round(cost_per_1k, 6),
            })

        daily_trend = [
            {
                "date": str(row.day),
                "model_alias": row.model_alias,
                "cost_usd": round(float(row.cost_usd), 4),
            }
            for row in daily_rows
        ]

        grand_total = sum(m["total_cost_usd"] for m in models)
        return {
            "period": rng.label if rng.is_custom else period,
            "total_cost_usd": round(grand_total, 4),
            "models": models,
            "daily_trend": daily_trend,
            **meta.as_dict(),
        }

    async def export_analytics(
        self,
        session: AsyncSession,
        *,
        format: str,
        period: str,
        group_by: str,
        actor: CurrentUser,
        scope: str = "all",
        client: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        scope_ids: list[uuid.UUID] | None | object = ...,
    ) -> tuple[str, str]:
        """Returns (content, content_type). scope_ids 를 넘기면(라우터가 헤더 전에 이미
        해석한 경우) 다시 해석하지 않는다."""
        # ⚠️ scope/client 를 그대로 넘겨야 화면에 보이는 집합과 파일이 일치한다 —
        #    예전엔 scope="all" 고정이라 팀 스코프 화면에서 전사 CSV 가 나갔다.
        response = await self.get_analytics(
            session, period=period, group_by=group_by, scope=scope, client=client,
            actor=actor, start_date=start_date, end_date=end_date, scope_ids=scope_ids,
        )

        if format == "csv":
            return self._to_csv(response, group_by), "text/csv"
        else:
            return response.model_dump_json(indent=2), "application/json"

    async def get_usage_by_user(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserResponse:
        """User 단위 누적 (period 1일 ~ date, 리포팅 tz, 청구 대상 전 상태). Dashboard 요약 테이블용."""
        import datetime as _dt

        _validate_period_date(period, date)
        month = ur.month_range(period)
        rng = ur.DayRange(month.start, _dt.date.fromisoformat(date), period)
        sc = ur.RollupScope.of(rng, billable_only=True)
        rows = await reads.by_user_detail(session, sc)
        meta = await ur.fetch_meta(session, sc)

        # ── 마이그레이션 주입분(seed) 반영 ──
        #
        # `POST /admin/budgets/seed-spent` 는 이관 이전 사용액을 budget_usages 에 절대값으로
        # 넣는데, 분석 경로는 사용량만 읽어서 예산 화면($963)과 분석 화면($13)이 갈렸다.
        # `max(실사용, budget_usages)` 가 아닌 이유: budget_usages 에는 일자 축이 없고 전 status 를
        # 누적한다 — date 를 월 중간으로 주면 cost_usd 만 월 총액이 되고 calls/토큰은 date 범위가
        # 되어 한 응답이 두 기간을 말한다. 그래서 seed 를 **월 단위 상수**로 역산한다:
        #     seed := max(budget_usages 월 누적 − 같은 달 실사용(전 status), 0)
        # ⚠️ 전 status 로 빼는 것이 load-bearing 이다(SUCCESS 만 빼면 실패 비용이 잔차에 남는다).
        from sqlalchemy import text as sa_text

        month_actual = await reads.cost_by_user_all_status(session, ur.RollupScope.of(month))
        recorded = (
            await session.execute(
                sa_text(
                    """
                    SELECT scope_id AS user_id, COALESCE(SUM(used_usd), 0) AS used
                    FROM budget.budget_usages
                    -- ⚠️ client IS NULL = 총합 행만. per-app 행은 부분집합이라
                    --    같이 더하면 사용액이 최대 2배로 부풀려진다.
                    WHERE scope = 'USER' AND period = :period AND client IS NULL
                    GROUP BY scope_id
                    """
                ),
                {"period": period},
            )
        ).all()
        seeded_by_user: dict[str, Decimal] = {}
        for r in recorded:
            residual = Decimal(str(r.used)) - month_actual.get(r.user_id, Decimal("0"))
            if residual > 0:
                seeded_by_user[str(r.user_id)] = residual

        items = [
            UsageByUserItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                user_email=r.user_email,
                cost_usd=Decimal(str(r.cost_usd)) + seeded_by_user.get(str(r.user_id), Decimal("0")),
                seeded_usd=seeded_by_user.get(str(r.user_id), Decimal("0")),
                calls=int(r.calls),
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
                cache_read_tokens=int(r.cache_read_tokens),
                cache_write_tokens=int(r.cache_creation_tokens),
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
            )
            for r in rows
        ]

        # ⚠️ 사용량이 한 건도 없는데 seed 만 있는 사용자(이관 직후)도 목록에 나와야 한다.
        seen = {it.user_id for it in items}
        missing = [uid for uid in seeded_by_user if uid not in seen]
        if missing:
            from sqlalchemy import select

            from app.models.auth import Department as _Dept
            from app.models.auth import Team as _Team
            from app.models.auth import User as _User

            meta_rows = (
                await session.execute(
                    select(
                        _User.id,
                        _User.display_name,
                        _User.email,
                        _Team.id.label("team_id"),
                        _Team.name.label("team_name"),
                        _Dept.id.label("dept_id"),
                        _Dept.name.label("dept_name"),
                    )
                    .select_from(_User)
                    .outerjoin(_Team, _Team.id == _User.team_id)
                    .outerjoin(_Dept, _Dept.id == _Team.dept_id)
                    .where(_User.id.in_([uuid.UUID(u) for u in missing]))
                )
            ).all()
            for m in meta_rows:
                seeded = seeded_by_user[str(m.id)]
                items.append(
                    UsageByUserItem(
                        date=date,
                        user_id=str(m.id),
                        user_name=m.display_name,
                        user_email=m.email,
                        cost_usd=seeded,
                        seeded_usd=seeded,
                        calls=0,
                        department_id=str(m.dept_id) if m.dept_id else None,
                        department_name=m.dept_name,
                        team_id=str(m.team_id) if m.team_id else None,
                        team_name=m.team_name,
                    )
                )

        # seed 를 더한 뒤 다시 정렬한다 — SQL 의 ORDER BY 는 실사용 기준이었다.
        items.sort(key=lambda it: it.cost_usd, reverse=True)
        return UsageByUserResponse(
            period=period, date=date, items=items,
            data_as_of=meta.data_as_of, delayed_days=[d.isoformat() for d in meta.delayed_days],
        )

    @staticmethod
    def _iter_csv(data: AnalyticsResponse, group_by: str):
        """``_to_csv`` 와 같은 바이트열을 조각으로 만든다 — export 가 본문 전체를
        메모리에 쌓지 않고 스트리밍할 수 있게(느린 클라이언트에서도 누수 없음)."""
        output = io.StringIO()
        writer = csv.writer(output)

        def flush():
            nonlocal output, writer
            chunk = output.getvalue()
            output = io.StringIO()
            writer = csv.writer(output)
            return chunk

        if group_by == "team":
            writer.writerow(["period", "team", "dept_name", "team_id", "active_users", "cost_usd"])
            for i, item in enumerate(data.by_team):
                writer.writerow([
                    data.period, item.team, item.dept_name or "",
                    item.team_id, item.active_users, str(item.cost_usd),
                ])
                if i % 500 == 499:
                    yield flush()
        elif group_by == "user":
            writer.writerow(["period", "user", "email", "requests", "cost_usd"])
            for i, item in enumerate(data.by_user):
                writer.writerow([data.period, item.user, item.email, item.requests, str(item.cost_usd)])
                if i % 500 == 499:
                    yield flush()
        else:
            writer.writerow(["period", "model", "display_name", "requests", "cost_usd"])
            for i, item in enumerate(data.by_model):
                writer.writerow([
                    data.period, item.model, item.display_name or "",
                    item.requests, str(item.cost_usd),
                ])
                if i % 500 == 499:
                    yield flush()
        # Total (charged) 구획(wave 3) — 위 표의 cost_usd 는 모델 토큰 비용(청구 대상 전 상태)이다.
        # 청구 총액 = 성공 + 실패 + 취소 + 웹 검색. 웹 검색 공란 = 미추적(무료 아님).
        ch = data.cost_summary.charged
        writer.writerow([])
        writer.writerow(["period", "charged_success_usd", "charged_failed_usd",
                         "charged_cancelled_usd", "charged_web_search_usd", "charged_total_usd"])
        writer.writerow([
            data.period, f"{ch.success_usd:.6f}", f"{ch.failed_usd:.6f}",
            f"{ch.cancelled_usd:.6f}",
            "" if ch.web_search_usd is None else f"{ch.web_search_usd:.6f}",
            f"{ch.total_usd:.6f}",
        ])
        # 웹 검색 — 위 표의 cost_usd(모델 비용)와 **별개** 구획(행 단위 열로 섞지 않는다).
        # 화면과 같은 표시 규칙: 배포됐거나 기록 비용 > 0 일 때만 구획을 넣는다.
        ws = data.web_search
        if web_search_visible(ws):
            writer.writerow([])
            writer.writerow([
                "period", "web_search_queries", "web_search_cost_usd",
                "web_search_untracked_queries",
            ])
            writer.writerow([
                data.period, ws.queries,
                "" if ws.cost_usd is None else f"{ws.cost_usd:.6f}",
                ws.untracked_queries,
            ])
        yield flush()

    @staticmethod
    def _to_csv(data: AnalyticsResponse, group_by: str) -> str:
        # 화면의 group_by 와 같은 차원을 쓴다 — 이전엔 항상 by_model 이라
        # '팀별' 보고내면 모델 CSV 가 나왔다.
        return "".join(AnalyticsService._iter_csv(data, group_by))
