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
from unittest.mock import AsyncMock, MagicMock

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


async def test_request_counts_uses_single_scan_with_filter_clause() -> None:
    """성공/에러 카운트는 한 번의 스캔 — WHERE 에 status 가 없고 집계 FILTER 가
    둘을 나눈다(두 쿼리 스캔 시절의 회귀 가드)."""
    from app.models.usage import ROIScope
    from app.repositories.analytics_repository import AnalyticsRepository

    session = AsyncMock()
    row = MagicMock()
    row.success, row.errors = 3, 1
    result = MagicMock()
    result.one.return_value = row
    session.execute = AsyncMock(return_value=result)

    repo = AnalyticsRepository(session)
    ok, err = await repo.request_counts("2026-09", ROIScope.GLOBAL, None)
    assert (ok, err) == (3, 1)
    assert session.execute.await_count == 1

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # usage 스키마 한정자까지 붙은 컴파일 형태.
    assert "FILTER (WHERE usage.usage_logs.status = 'SUCCESS')" in compiled
    assert (
        "FILTER (WHERE usage.usage_logs.status IN ('ERROR', 'TIMEOUT'))"
        in compiled
    )
    # 성공 전용 조건이 바깥 WHERE 에 새면 에러는 항상 0 — 문서화만으론 부족해
    # 단언(FROM 절 뒤가 진짜 WHERE — 앞쪽 WHERE 는 FILTER 의 것).
    where_part = compiled.split("FROM usage.usage_logs", 1)[1]
    assert "status" not in where_part


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


def _hybrid_session(*, by_model=None, by_team=None, by_user=None):
    """하이브리드 롤업 읽기(services/usage_reads)용 기록 세션 — 개요 질의에 JSON 행을 준다."""
    import json

    from tests.rollup_double import EmptyAggRow, empty_result, recording_session

    def _respond(sql, params):
        if "AS by_model" in sql:
            return empty_result(EmptyAggRow(
                by_model=json.dumps(by_model or []), by_team=json.dumps(by_team or []),
                by_user=json.dumps(by_user or [])))
        return None

    return recording_session(_respond)


# ── H1: by_team 에 dept_name ─────────────────────────────────────────────

@pytest.mark.unit
def test_by_team_joins_department_for_canonical_name():
    """by_team 질의는 trends_by_team 과 같은 Department OUTER JOIN 을 해야
    프론트의 teamDisplayName({dept}_{team}) 재료가 내려온다."""
    from app.services import usage_reads as reads

    by_team_part = reads._ANALYTICS_SELECT.split("AS by_model,", 1)[1].split("AS by_team", 1)[0]
    assert "JOIN auth.teams" in by_team_part
    # 부서 미지정 팀의 행이 사라지면 안 된다 — INNER 가 아니라 OUTER JOIN.
    assert "LEFT JOIN auth.departments" in by_team_part
    assert "LEFT JOIN auth.departments" in reads._TRENDS_SELECT


@pytest.mark.unit
async def test_by_team_row_carries_dept_name(admin_user):
    """질의 결과 행의 dept_name 이 TeamBreakdown 까지 전달된다."""
    session, _ = _hybrid_session(by_team=[[str(TEAM_A), "Developers", "NDS", "12.5", 3]])
    out = await AnalyticsService().get_analytics(session, period="2026-09", actor=admin_user)
    assert len(out.by_team) == 1
    assert out.by_team[0].team == "Developers"
    assert out.by_team[0].dept_name == "NDS"
    assert out.by_team[0].cost_usd == Decimal("12.5")


# ── H2: by_model 비용 내림차순 결정론 ─────────────────────────────────────

@pytest.mark.unit
async def test_by_model_sorted_cost_descending(admin_user):
    """by_model 은 비용 DESC(+ alias) — 상세 표(cost DESC)·팔레트 index 와 같은 순서.
    순서는 SQL 이 정하고(결정적 2차 키 포함) 서비스는 그 순서를 그대로 쓴다."""
    from app.services import usage_reads as reads

    assert "ORDER BY t.c DESC, t.model_alias" in reads._ANALYTICS_SELECT
    session, _ = _hybrid_session(by_model=[["expensive", "100", 3, None], ["mid", "50", 2, None],
                                           ["cheap", "1", 1, None]])
    out = await AnalyticsService().get_analytics(session, period="2026-09", actor=admin_user)
    assert [b.model for b in out.by_model] == ["expensive", "mid", "cheap"]
    assert [b.requests for b in out.by_model] == [3, 2, 1]


# ── M1: display_name 카탈로그 조인 ────────────────────────────────────────

@pytest.mark.unit
async def test_by_model_maps_catalog_display_name(admin_user):
    """카탈로그에 표시명이 있는 모델은 display_name 으로, 없는 alias 는 None —
    프론트의 modelDisplay fallback 규칙과 대응."""
    from app.services import usage_reads as reads

    assert "LEFT JOIN model.model_aliases" in reads._ANALYTICS_SELECT
    session, _ = _hybrid_session(by_model=[["cataloged", "10", 1, "Claude Opus 4.5"],
                                           ["uncataloged", "5", 1, None]])
    out = await AnalyticsService().get_analytics(session, period="2026-09", actor=admin_user)
    names = {b.model: b.display_name for b in out.by_model}
    assert names["cataloged"] == "Claude Opus 4.5"
    assert names["uncataloged"] is None


@pytest.mark.unit
def test_model_detail_query_joins_model_aliases():
    from app.services import usage_reads as reads

    sql = reads._MODEL_DETAIL_SELECT
    assert "LEFT JOIN model.model_aliases" in sql
    assert "display_name" in sql


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
async def test_export_team_leader_keeps_isolation():
    """export 경로도 _resolve_scope 를 탄다 — TEAM_LEADER 가 scope='all' 로
    내도 본인 팀으로 강제된다(하이브리드 읽기의 바인드된 팀 집합으로 확인)."""
    from tests.rollup_double import data_branches, team_isolated

    leader = CurrentUser(
        user_id=uuid.uuid4(), email="l@t.com",
        role=UserRole.TEAM_LEADER, team_id=TEAM_A,
    )
    session, seen = _hybrid_session()
    await AnalyticsService().export_analytics(
        session, format="json", period="2026-09", group_by="model",
        scope="all", actor=leader,
    )
    data_q = [(q, p) for q, p in seen if data_branches(q)]
    assert data_q
    assert all(team_isolated(q, p, [TEAM_A]) for q, p in data_q)


# ── per-app budget_usages 이중계상 회귀 ──────────────────────────────────

@pytest.mark.unit
async def test_seed_residual_query_filters_client_rows(admin_user):
    """budget_usages 시드 잔차 집계는 반드시 client IS NULL(총합 행)만 더한다.

    워커는 (scope, scope_id, period, client) 단위로 per-app 행도 쓴다 —
    'claude-code'/'cowork'/'codex' 행은 총합의 부분집합이라 client IS NULL
    필터 없이 SUM 하면 사용액이 최대 2배로 부풀고, 잔차(seed)가 허위로 생겨
    대시보드의 사용자 비용이 과대계상된다.
    """
    session, seen = _hybrid_session()
    await AnalyticsService().get_analytics(
        session, period="2026-09", group_by="user", actor=admin_user
    )
    bu_sqls = [q for q, _ in seen if "budget_usages" in q]
    assert bu_sqls, "seed 잔차 질의가 실행되지 않음"
    for q in bu_sqls:
        assert "CLIENT IS NULL" in q.upper(), f"client IS NULL 누락: {q[:200]}"


@pytest.mark.unit
async def test_usage_by_user_seed_query_filters_client_rows():
    """`get_usage_by_user`(대시보드 top users)의 seed 질의도 총합 행만 더한다."""
    session, seen = _hybrid_session()
    await AnalyticsService().get_usage_by_user(session, period="2026-09", date="2026-09-15")
    bu_sqls = [q for q, _ in seen if "budget_usages" in q]
    assert bu_sqls, "seed 잔차 질의가 실행되지 않음"
    for q in bu_sqls:
        assert "CLIENT IS NULL" in q.upper(), f"client IS NULL 누락: {q[:200]}"
