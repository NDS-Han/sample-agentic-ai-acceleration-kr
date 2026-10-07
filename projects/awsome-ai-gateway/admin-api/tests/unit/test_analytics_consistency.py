# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""분석 위젯 간 표현 일관성 회귀 — 같은 데이터가 경로마다 다르게 보이는 결함군.

배경 — 2차 UX 리뷰(review-ux-all-pages-2)에서 발견된, 페이지 단독으로는 정상이라
기존 페이지별 검토를 빠져나간 결함들:

  H1  by_team 에 dept_name 이 없어 같은 화면에서 추이 범례는 'NDS_Developers',
      막대 차트는 'Developers' — 동명 팀을 구분 못 함. → Department outerjoin.
  H2  repo 가 ORDER BY 없는 dict 를 주므로 by_model 순서가 DB 반환 순서 —
      같은 페이지의 상세 표(cost DESC)와 다른 순서 + 팔레트 index 가 어긋남.
      → 서비스에서 cost DESC 정렬.
  M1  by_model/모델 상세가 raw alias 만 내려 대시보드(modelDisplay)와 표기 불일치
      → ModelAlias 카탈로그 조인으로 display_name 전달.
  M4  export CSV 가 group_by/scope 를 무시해 화면과 다른 파일이 나감
      → group_by 별 컬럼 분기 + scope/client 전달.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.schemas.analytics import (
    AnalyticsResponse,
    CostSummary,
    ModelBreakdown,
    TeamBreakdown,
    UserBreakdown,
)
from app.services.analytics_service import AnalyticsService

TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture(autouse=True)
def _empty_results(mock_session):
    result = MagicMock()
    result.all.return_value = []
    # `for row in result` 경로(by_team)도 빈 이터레이터.
    result.__iter__.return_value = iter([])
    mock_session.execute.return_value = result


def _executed_sqls(mock_session) -> list[str]:
    return [
        str(c.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))
        for c in mock_session.execute.call_args_list
    ]


def _empty_repo(cost_by_model: dict | None = None):
    """get_analytics 가 호출하는 repo 메서드를 빈값으로 스텁한다."""
    repo = MagicMock()
    repo.sum_usage_by_model = AsyncMock(return_value=cost_by_model or {})
    repo.count_requests_by_model = AsyncMock(return_value={})
    repo.count_active_users = AsyncMock(return_value=0)
    repo.total_requests = AsyncMock(return_value=0)
    repo.token_bucket_totals = AsyncMock(return_value={
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    return repo


# ── H1: by_team 에 dept_name ─────────────────────────────────────────────

@pytest.mark.unit
async def test_by_team_joins_department_for_canonical_name(mock_session, admin_user):
    """by_team 질의는 trends_by_team 과 같은 Department OUTER JOIN 을 해야
    프론트의 teamDisplayName({dept}_{team}) 재료가 내려온다."""
    svc = AnalyticsService()
    with patch(
        "app.services.analytics_service.AnalyticsRepository", return_value=_empty_repo()
    ):
        await svc.get_analytics(mock_session, period="2026-09", actor=admin_user)

    sqls = _executed_sqls(mock_session)
    team_sqls = [s for s in sqls if "JOIN teams" in s or "JOIN auth.teams" in s or "teams" in s]
    assert team_sqls, "by_team 질의가 실행되지 않음"
    by_team_sql = next(s for s in team_sqls if "departments" in s)
    assert "dept_name" in by_team_sql
    # 부서 미지정 팀의 행이 사라지면 안 된다 — INNER 가 아니라 OUTER JOIN.
    assert "OUTER JOIN" in by_team_sql.upper()


@pytest.mark.unit
async def test_by_team_row_carries_dept_name(mock_session, admin_user):
    """질의 결과 행의 dept_name 이 TeamBreakdown 까지 전달된다."""
    row = MagicMock()
    row.team_id = TEAM_A
    row.team_name = "Developers"
    row.dept_name = "NDS"
    row.cost = Decimal("12.5")
    row.users = 3

    result = MagicMock()
    result.all.return_value = []
    result.__iter__.return_value = iter([row])
    mock_session.execute.return_value = result

    svc = AnalyticsService()
    with patch(
        "app.services.analytics_service.AnalyticsRepository", return_value=_empty_repo()
    ):
        out = await svc.get_analytics(mock_session, period="2026-09", actor=admin_user)

    assert len(out.by_team) == 1
    assert out.by_team[0].team == "Developers"
    assert out.by_team[0].dept_name == "NDS"


# ── H2: by_model 비용 내림차순 결정론 ─────────────────────────────────────

@pytest.mark.unit
async def test_by_model_sorted_cost_descending(mock_session, admin_user):
    """repo dict 의 DB 반환 순서를 그대로 쓰면 상세 표(cost DESC)·팔레트 index 와
    어긋난다 — 서비스가 내림차순으로 못 박는다."""
    repo = _empty_repo(cost_by_model={
        "cheap": Decimal("1"),
        "expensive": Decimal("100"),
        "mid": Decimal("50"),
    })
    svc = AnalyticsService()
    with patch(
        "app.services.analytics_service.AnalyticsRepository", return_value=repo
    ):
        out = await svc.get_analytics(mock_session, period="2026-09", actor=admin_user)

    assert [b.model for b in out.by_model] == ["expensive", "mid", "cheap"]


# ── M1: display_name 카탈로그 조인 ────────────────────────────────────────

@pytest.mark.unit
async def test_by_model_maps_catalog_display_name(mock_session, admin_user):
    """카탈로그에 표시명이 있는 모델은 display_name 으로, 없는 alias 는 None —
    프론트의 modelDisplay fallback 규칙과 대응."""
    repo = _empty_repo(cost_by_model={
        "cataloged": Decimal("10"),
        "uncataloged": Decimal("5"),
    })

    lookup_row = MagicMock()
    lookup_row.alias = "cataloged"
    lookup_row.display_name = "Claude Opus 4.5"

    # session.execute 는 by_team(for row in result)·trends(.all())·룩업 등 여러
    # 질의에 쓰인다 — 룩업(model_aliases select)에만 행을 주고 나머지는 비운다.
    def _execute(stmt):
        sql = str(stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
        ))
        r = MagicMock()
        r.__iter__.return_value = iter([])
        r.all.return_value = [lookup_row] if "model_aliases" in sql else []
        return r

    mock_session.execute.side_effect = _execute

    svc = AnalyticsService()
    with patch(
        "app.services.analytics_service.AnalyticsRepository", return_value=repo
    ):
        out = await svc.get_analytics(mock_session, period="2026-09", actor=admin_user)

    names = {b.model: b.display_name for b in out.by_model}
    assert names["cataloged"] == "Claude Opus 4.5"
    assert names["uncataloged"] is None


@pytest.mark.unit
async def test_model_detail_query_joins_model_aliases(mock_session, admin_user):
    svc = AnalyticsService()
    await svc.get_model_cost_detail(mock_session, period="2026-09", actor=admin_user)
    model_sql = _executed_sqls(mock_session)[0]
    assert "model_aliases" in model_sql
    assert "OUTER JOIN" in model_sql.upper()
    assert "display_name" in model_sql


# ── M4: export 가 group_by/scope 를 존중 ─────────────────────────────────

def _sample_response() -> AnalyticsResponse:
    return AnalyticsResponse(
        period="2026-09",
        cost_summary=CostSummary(
            total_requests=10, total_tokens=1000,
            total_cost_usd=Decimal("42"), active_users=2,
            avg_cost_per_user_usd=Decimal("21"),
        ),
        by_model=[ModelBreakdown(model="m1", requests=7, cost_usd=Decimal("30"),
                                 display_name="Model One")],
        by_team=[TeamBreakdown(team="Developers", team_id=str(TEAM_A),
                               cost_usd=Decimal("30"), active_users=2,
                               dept_name="NDS")],
        by_user=[UserBreakdown(user="Alice", email="a@t.com",
                               cost_usd=Decimal("12"), requests=5)],
    )


@pytest.mark.unit
def test_export_csv_uses_selected_grouping():
    svc = AnalyticsService()
    resp = _sample_response()

    csv_model = svc._to_csv(resp, "model")
    assert csv_model.splitlines()[0] == "period,model,display_name,requests,cost_usd"
    assert "m1" in csv_model and "Model One" in csv_model and "Developers" not in csv_model

    csv_team = svc._to_csv(resp, "team")
    assert csv_team.splitlines()[0] == "period,team,dept_name,team_id,active_users,cost_usd"
    assert "Developers" in csv_team and "NDS" in csv_team and "m1" not in csv_team

    csv_user = svc._to_csv(resp, "user")
    assert csv_user.splitlines()[0] == "period,user,email,requests,cost_usd"
    assert "Alice" in csv_user and "a@t.com" in csv_user and "m1" not in csv_user


@pytest.mark.unit
async def test_export_forwards_scope_and_client(mock_session, admin_user):
    """화면의 scope 가 export 에도 적용돼야 한다 — 예전엔 'all' 고정이라
    팀 스코프 화면에서 전사 CSV 가 나갔다."""
    svc = AnalyticsService()
    svc.get_analytics = AsyncMock(return_value=_sample_response())
    await svc.export_analytics(
        mock_session, format="csv", period="2026-09", group_by="team",
        scope=f"team:{TEAM_A}", client="claude-code", actor=admin_user,
    )
    kwargs = svc.get_analytics.call_args.kwargs
    assert kwargs["scope"] == f"team:{TEAM_A}"
    assert kwargs["client"] == "claude-code"
    assert kwargs["group_by"] == "team"


@pytest.mark.unit
async def test_export_team_leader_keeps_isolation(mock_session):
    """export 경로도 _resolve_scope 를 탄다 — TEAM_LEADER 가 scope='all' 로
   내도 본인 팀으로 강제된다(서비스 위임이므로 get_analytics 에 위임 확인)."""
    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=TEAM_A,
    )
    svc = AnalyticsService()
    repo = _empty_repo()
    with patch(
        "app.services.analytics_service.AnalyticsRepository", return_value=repo
    ):
        await svc.export_analytics(
            mock_session, format="json", period="2026-09", group_by="model",
            scope="all", actor=leader,
        )
    # TEAM_LEADER + 'all' → repo 호출에 scope_ids(본인 팀)가 실려야 한다.
    kw = repo.sum_usage_by_model.call_args.kwargs
    assert kw["scope_ids"] == [TEAM_A]
