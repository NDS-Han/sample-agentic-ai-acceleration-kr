# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""웹 검색 통제(검색 수 기준) — 해석 규칙·감사 로그·검증 + 실 PG 왕복(PROOF_DSN).

해석 규칙은 gateway-proxy services/web_search_quota.resolve_policy 와 같아야 한다:
개인 월 한도 USER → ORG, 팀 풀 = TEAM(상속 안 함), 요청당 상한 USER → TEAM → ORG → 기본 5.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.audit_catalog import group_for_action
from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.schemas.web_search_quota import WebSearchPolicyUpdate
from app.services.web_search_quota_service import resolve

PROOF_DSN = os.environ.get("PROOF_DSN")


def _row(limit=None, cap=None):
    return SimpleNamespace(monthly_query_limit=limit, per_request_cap=cap)


class TestResolve:
    def test_no_rows(self):
        e = resolve({}, include_user=True, period="2026-11", default_cap=5)
        assert (e.monthly_limit, e.monthly_limit_source) == (None, "none")
        assert (e.per_request_cap, e.per_request_cap_source) == (5, "default")
        assert e.team_pool_limit is None

    def test_cap_user_then_team_then_org(self):
        rows = {"ORG": _row(cap=9), "TEAM": _row(cap=7), "USER": _row(cap=3)}
        e = resolve(rows, include_user=True, period="p", default_cap=5)
        assert (e.per_request_cap, e.per_request_cap_source) == (3, "user")
        rows.pop("USER")
        assert resolve(rows, include_user=True, period="p", default_cap=5).per_request_cap == 7
        rows.pop("TEAM")
        assert resolve(rows, include_user=True, period="p",
                       default_cap=5).per_request_cap_source == "organization"

    def test_personal_limit_user_then_org_team_is_pool(self):
        rows = {"ORG": _row(100), "TEAM": _row(1000), "USER": _row(20)}
        e = resolve(rows, include_user=True, period="p", default_cap=5)
        assert (e.monthly_limit, e.monthly_limit_source, e.team_pool_limit) == (20, "user", 1000)
        rows.pop("USER")
        e = resolve(rows, include_user=True, period="p", default_cap=5)
        assert (e.monthly_limit, e.monthly_limit_source) == (100, "organization")
        e = resolve({"TEAM": _row(50)}, include_user=True, period="p", default_cap=5)
        assert e.monthly_limit is None and e.team_pool_limit == 50

    def test_team_view_ignores_user_row(self):
        rows = {"USER": _row(1, 1), "TEAM": _row(40, 4)}
        e = resolve(rows, include_user=False, period="p", default_cap=5)
        assert e.monthly_limit is None and e.per_request_cap == 4 and e.team_pool_limit == 40


class TestValidation:
    @pytest.mark.parametrize("body", [{"monthly_query_limit": -1}, {"per_request_cap": 0},
                                      {"per_request_cap": 51},
                                      {"monthly_query_limit": 10_000_001}])
    def test_rejects(self, body):
        with pytest.raises(ValidationError):
            WebSearchPolicyUpdate(**body)

    def test_accepts_zero_limit_and_nulls(self):
        assert WebSearchPolicyUpdate(monthly_query_limit=0).monthly_query_limit == 0
        assert WebSearchPolicyUpdate().model_dump() == {"monthly_query_limit": None,
                                                         "per_request_cap": None}

    def test_actions_live_with_budget_and_limit_actions(self):
        for a in ("SET_WEB_SEARCH_QUOTA", "SET_WEB_SEARCH_REQUEST_CAP", "CLEAR_WEB_SEARCH_QUOTA"):
            assert group_for_action(a) == group_for_action("SET_RATE_LIMIT")
        assert group_for_action("SET_WEB_SEARCH_BUDGET_INCLUSION") == "other"  # 만들지 않는다


# ── 라우터: 감사 로그 ───────────────────────────────────────────────────────────
def _admin():
    return CurrentUser(user_id=uuid.uuid4(), email="a@x", role=UserRole.ADMIN, team_id=None)


def _request():
    r = MagicMock()
    r.client.host = "10.0.0.1"
    r.state.request_id = "rid"
    return r


async def _put(func, before, body, **path):
    session = MagicMock()
    session.commit = AsyncMock()
    svc = MagicMock()
    after = body.model_dump()
    svc.upsert = AsyncMock(return_value=(before, after))
    with patch("app.routers.web_search_quota.WebSearchQuotaService", return_value=svc), \
         patch("app.routers.web_search_quota._require_target", AsyncMock()), \
         patch("app.routers.web_search_quota.audit_logger") as al:
        al.log = AsyncMock()
        resp = await func(_request(), **path, body=body, admin=_admin(), session=session)
    return resp, [c.kwargs for c in al.log.await_args_list], svc, session


class TestRouterAudit:
    async def test_new_row_logs_both_actions(self):
        from app.routers.web_search_quota import put_team

        tid = uuid.uuid4()
        resp, logs, svc, session = await _put(
            put_team, None, WebSearchPolicyUpdate(monthly_query_limit=500, per_request_cap=4),
            team_id=tid)
        assert [log["action"] for log in logs] == ["SET_WEB_SEARCH_QUOTA",
                                                  "SET_WEB_SEARCH_REQUEST_CAP"]
        assert logs[0]["resource_id"] == f"TEAM:{tid}"
        assert logs[0]["changes"] == {"before": {"monthly_query_limit": None},
                                      "after": {"monthly_query_limit": 500}}
        assert svc.upsert.await_args.args[:2] == ("TEAM", tid)
        session.commit.assert_awaited()
        assert resp["monthly_query_limit"] == 500

    async def test_only_changed_axis_is_logged(self):
        from app.routers.web_search_quota import put_user

        _, logs, _, _ = await _put(
            put_user, {"monthly_query_limit": 100, "per_request_cap": 5},
            WebSearchPolicyUpdate(monthly_query_limit=100, per_request_cap=2), user_id=uuid.uuid4())
        assert [log["action"] for log in logs] == ["SET_WEB_SEARCH_REQUEST_CAP"]
        assert logs[0]["changes"]["after"] == {"per_request_cap": 2}

    async def test_org_row(self):
        from app.routers.web_search_quota import put_org

        session = MagicMock()
        session.commit = AsyncMock()
        svc = MagicMock()
        svc.upsert = AsyncMock(return_value=({"monthly_query_limit": 1, "per_request_cap": None},
                                             {"monthly_query_limit": 2, "per_request_cap": None}))
        with patch("app.routers.web_search_quota.WebSearchQuotaService", return_value=svc), \
             patch("app.routers.web_search_quota.audit_logger") as al:
            al.log = AsyncMock()
            await put_org(_request(), WebSearchPolicyUpdate(monthly_query_limit=2),
                          admin=_admin(), session=session)
        assert svc.upsert.await_args.args[:2] == ("ORG", None)
        assert al.log.await_args.kwargs["resource_id"] == "ORG"

    async def test_clear_logs_and_missing_is_404(self):
        from app.core.exceptions import NotFoundError
        from app.routers.web_search_quota import delete_user

        uid = uuid.uuid4()
        for before in ({"monthly_query_limit": 3, "per_request_cap": None}, None):
            session = MagicMock()
            session.commit = AsyncMock()
            svc = MagicMock()
            svc.delete = AsyncMock(return_value=before)
            with patch("app.routers.web_search_quota.WebSearchQuotaService", return_value=svc), \
                 patch("app.routers.web_search_quota.audit_logger") as al:
                al.log = AsyncMock()
                if before is None:
                    with pytest.raises(NotFoundError):
                        await delete_user(_request(), uid, admin=_admin(), session=session)
                    al.log.assert_not_awaited()
                else:
                    await delete_user(_request(), uid, admin=_admin(), session=session)
                    kw = al.log.await_args.kwargs
                    assert kw["action"] == "CLEAR_WEB_SEARCH_QUOTA"
                    assert kw["changes"] == {"before": before, "after": None}

    def test_mutations_require_human_admin(self):
        from app.core.auth import require_human_admin
        from app.routers.web_search_quota import router

        for route in router.routes:
            if {"PUT", "DELETE"} & set(route.methods):
                deps = [d.call for d in route.dependant.dependencies]
                assert require_human_admin in deps, route.path


# ── 실 PG ──────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")
async def test_real_pg_upsert_overview_and_effective():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.usage_filters import current_kst_period
    from app.services.web_search_quota_service import WebSearchQuotaService

    user = uuid.UUID("00000000-0000-4000-a000-000000000010")
    team = uuid.UUID("00000000-0000-4000-a000-000000000003")
    period = current_kst_period()
    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            await s.execute(text("DELETE FROM budget.web_search_policies"))
            await s.execute(text(
                "INSERT INTO usage.usage_logs (request_id, user_id, team_id, dept_id, model_alias, "
                "provider, input_tokens, output_tokens, web_search_count, cost_usd, latency_ms, "
                "status, requested_at, completed_at) VALUES ('wsq-1', :u, :t, "
                "'00000000-0000-4000-a000-000000000002', 'm', 'BEDROCK', 1, 1, 7, 0, 1, 'ERROR', "
                "now() - interval '1 minute', now())"), {"u": user, "t": team})
            svc = WebSearchQuotaService(s)
            await svc.upsert("ORG", None, monthly_query_limit=100, per_request_cap=None,
                             actor_id=None)
            await svc.upsert("TEAM", team, monthly_query_limit=40, per_request_cap=3,
                             actor_id=None)
            before, _ = await svc.upsert("TEAM", team, monthly_query_limit=10, per_request_cap=3,
                                         actor_id=None)
            assert before == {"monthly_query_limit": 40, "per_request_cap": 3}

            eff = await svc.effective_for_user(user, team, period)
            assert (eff.monthly_limit, eff.monthly_limit_source) == (100, "organization")
            assert eff.used >= 7 and eff.remaining == 100 - eff.used  # 실패 요청의 검색도 센다
            assert eff.team_pool_limit == 10 and eff.team_used >= 7
            assert (eff.per_request_cap, eff.per_request_cap_source) == (3, "team")

            await svc.upsert("USER", user, monthly_query_limit=5, per_request_cap=None,
                             actor_id=None)
            eff = await svc.effective_for_user(user, team, period)
            assert (eff.monthly_limit, eff.remaining) == (5, 0)

            ov = await svc.overview(period)
            assert ov.org.monthly_query_limit == 100 and ov.default_per_request_cap == 5
            t = next(x for x in ov.teams if x.team_id == str(team))
            assert (t.monthly_query_limit, t.per_request_cap) == (10, 3) and t.used >= 7
            assert [u.user_id for u in ov.users] == [str(user)]

            assert await svc.delete("USER", user) == {"monthly_query_limit": 5,
                                                      "per_request_cap": None}
            assert await svc.delete("USER", user) is None
            # (scope, scope_id) 유일 — ORG 행은 하나뿐.
            await svc.upsert("ORG", None, monthly_query_limit=None, per_request_cap=2,
                             actor_id=None)
            n = (await s.execute(text(
                "SELECT count(*) FROM budget.web_search_policies WHERE scope='ORG'"))).scalar()
            assert n == 1
            await trans.rollback()
    finally:
        await engine.dispose()
