# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: 웹 검색 비용은 예산·한도·모델 비용 어디에도 섞이지 않는다 (실 PG).

같은 사용량을 두 번 만든다 — (A) 웹 검색 없음, (B) 같은 요청에 검색 수·비용만 추가
(추적 행·미추적 NULL 행·실패 요청의 검색 포함). 그리고 실제 라우터/서비스 함수를 같은
DB 에서 불러:

  - 대시보드 KPI 의 ``total_cost_usd``·``budget_used_usd``·``budget_limit_usd``·
    ``budget_utilization_pct``, 앱별 점유의 ``cost_usd``·``share_pct``,
    모니터링 1시간 ``total_cost_usd``, 내 예산(개인·팀) ``used/limit/remaining/pct``,
    내 사용량 ``summary.cost_usd``, Analytics ``cost_summary`` 가 **A 와 B 에서 동일**하고
  - 웹 검색은 별도 ``web_search`` 객체로만 나오며 NULL(미추적) 이 0 으로 접히지 않음

을 확인한다. 예산 원장(budget_usages)은 워커가 ``cost_usd`` 만으로 쓴다 — 그 쪽 회귀는
cost-recorder-worker ``tests/unit/test_web_search_cost.py`` 가 잡는다. 여기서는 같은
원장 행을 두 변형에 똑같이 넣어, API 가 읽는 쪽에서 검색 비용을 더하지 않는지를 본다.

``PROOF_DSN`` (alembic head 까지 올라간 **scratch** DB) 이 있어야 돈다. 모든 쓰기는
트랜잭션 안에서 하고 롤백한다::

    PROOF_DSN=postgresql+asyncpg://postgres:pw@127.0.0.1:55439/gateway \\
        pytest tests/regression/test_high_web_search_cost_excluded_from_budgets.py
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")

#: 시드(db/init/03) 관리자·팀·부서.
_USER = uuid.UUID("00000000-0000-4000-a000-000000000010")
_TEAM = uuid.UUID("00000000-0000-4000-a000-000000000003")
_DEPT = uuid.UUID("00000000-0000-4000-a000-000000000002")

#: (request_id 접미, status, client, model cost, web_search_count, web_search_cost)
_ROWS = [
    ("a", "SUCCESS", "claude-code", "0.120000", 2, "0.014000"),
    ("b", "SUCCESS", "cowork", "0.300000", 0, "0"),
    ("c", "SUCCESS", "claude-code", "0.050000", 5, None),      # 추적 이전 행(NULL)
    ("d", "ERROR", "claude-code", "0.003000", 1, "0.007000"),  # 실패 요청도 검색은 과금
]


async def _snapshot(with_web_search: bool) -> dict:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.auth import CurrentUser
    from app.core.usage_filters import current_kst_period
    from app.models.auth import UserRole
    from app.routers.dashboard import client_share, dashboard_kpi, dashboard_web_search
    from app.routers.monitoring import monitoring_overview
    from app.routers.my import get_my_budget, get_my_usage
    from app.services.analytics_service import AnalyticsService

    period = current_kst_period()
    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            tag = uuid.uuid4().hex[:8]
            for sfx, status, client, cost, ws, ws_cost in _ROWS:
                await session.execute(
                    text(
                        "INSERT INTO usage.usage_logs (request_id, user_id, team_id, dept_id, "
                        "model_alias, provider, input_tokens, output_tokens, web_search_count, "
                        "web_search_cost_usd, cost_usd, latency_ms, status, requested_at, "
                        "completed_at, client) VALUES (:rid, :u, :t, :d, 'm-wsc', 'BEDROCK', 10, "
                        "5, :ws, CAST(:wsc AS numeric), CAST(:cost AS numeric), 100, "
                        "CAST(:st AS usage.usage_status), now() - interval '1 minute', now(), :cl)"
                    ),
                    {
                        "rid": f"wsc-{tag}-{sfx}", "u": _USER, "t": _TEAM, "d": _DEPT,
                        "ws": ws if with_web_search else 0,
                        # 검색 없는 변형은 "추적 중·검색 0" — 미추적 행만 NULL 로 둔다.
                        "wsc": (ws_cost if with_web_search else "0") if ws_cost is not None
                        else None,
                        "cost": cost, "st": status, "cl": client,
                    },
                )
            # 예산 원장·한도 — 워커가 모델 비용으로만 쓰는 행(두 변형 동일).
            await session.execute(
                text(
                    "INSERT INTO budget.budget_configs (scope, scope_id, max_budget_usd, "
                    "allocated_by, effective_from) SELECT 'TEAM', :t, 100, :u, current_date "
                    "WHERE NOT EXISTS (SELECT 1 FROM budget.budget_configs WHERE scope='TEAM' "
                    "AND scope_id=:t AND is_active AND client IS NULL)"
                ),
                {"t": _TEAM, "u": _USER},
            )
            for scope, sid in (("TEAM", _TEAM), ("USER", _USER)):
                await session.execute(
                    text(
                        "INSERT INTO budget.budget_usages (scope, scope_id, period, client, "
                        "used_usd, limit_usd, last_updated) VALUES (CAST(:s AS "
                        "budget.budget_scope), :sid, :p, NULL, 0.473, 0, now()) "
                        "ON CONFLICT (scope, scope_id, period, COALESCE(client, '')) "
                        "DO UPDATE SET used_usd = budget.budget_usages.used_usd + 0.473"
                    ),
                    {"s": scope, "sid": sid, "p": period},
                )

            admin = CurrentUser(user_id=_USER, email="a@x", role=UserRole.ADMIN, team_id=_TEAM)
            req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
            snap = {
                "kpi": await dashboard_kpi(req, period, None, admin, session),
                "share": await client_share(period, admin, session),
                # 웹 검색 섹션 — 전부 웹 검색 값이라 A/B 비교에서는 뺀다(아래 _strip).
                "web_search_section": await dashboard_web_search(req, period, None, admin, session),
                "mon": await monitoring_overview(req, admin, session),
                "my_budget": await get_my_budget(req, admin, session),
                "my_usage": await get_my_usage(req, period, admin, session),
                "analytics": (await AnalyticsService().get_analytics(
                    session, period=period, group_by="model", actor=admin,
                )).model_dump(mode="json"),
            }
            await trans.rollback()
            return snap
    finally:
        await engine.dispose()


def _strip_web_search(snap: dict) -> dict:
    """웹 검색 필드를 뺀 나머지 — 이게 A/B 에서 같아야 한다."""
    out = {}
    for k, v in snap.items():
        if k.startswith("web_search") or k == "timestamp":
            continue
        if isinstance(v, dict):
            out[k] = _strip_web_search(v)
        elif isinstance(v, list):
            out[k] = [_strip_web_search(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


@pytest.mark.asyncio
async def test_budget_limit_and_model_cost_identical_with_and_without_web_search():
    plain = await _snapshot(with_web_search=False)
    searched = await _snapshot(with_web_search=True)

    # 공허한 통과 방지 — 비교 대상이 실제 값을 갖는다.
    assert plain["kpi"]["budget_used_usd"] > 0 and plain["kpi"]["budget_limit_usd"] > 0
    assert plain["my_budget"]["team"]["used_usd"] > 0
    assert _strip_web_search(plain) == _strip_web_search(searched)

    # 웹 검색은 별도 객체로만 — 추적분 합(0.014+0+0.007), 미추적(NULL) 5건은 따로.
    for ws in (searched["kpi"]["web_search"], searched["my_usage"]["web_search"],
               searched["analytics"]["web_search"], searched["mon"]["last_1h"]["web_search"]):
        assert ws["queries"] >= 8
        assert ws["untracked_queries"] >= 5
        assert ws["cost_usd"] is not None and ws["cost_usd"] >= 0.021 - 1e-9
    cc = next(c for c in searched["share"]["clients"] if c["client"] == "claude-code")
    assert cc["web_search_count"] >= 8 and cc["web_search_untracked_count"] >= 5
    assert cc["web_search_cost_usd"] >= 0.021 - 1e-9

    # 대시보드 웹 검색 섹션 — 앱별·일별 검색 수와 요청당 검색 수.
    sec = searched["web_search_section"]
    assert sec["queries"] >= 8 and sec["successful_requests"] >= 3
    assert next(c for c in sec["by_client"] if c["client"] == "claude-code")["queries"] >= 8
    assert sum(d["queries"] for d in sec["daily"]) == sec["queries"]
    assert sec["queries_per_request"] == round(sec["queries"] / sec["successful_requests"], 8)
    assert plain["web_search_section"]["queries"] == sec["queries"] - 8
