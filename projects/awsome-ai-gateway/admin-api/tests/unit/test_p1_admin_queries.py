# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""p1-admin-queries — 조직 트리 축약형·marks, VK eager loading 차단, 사용자×모델 scope_total,
CSV 내보내기 스트리밍(헤더 먼저)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import StreamingResponse

from app.core.auth import CurrentUser
from app.models.auth import UserRole

ADMIN = CurrentUser(user_id=uuid.uuid4(), email="a@x", role=UserRole.ADMIN, team_id=None)


# ── VK eager loading ─────────────────────────────────────────────────────────
def test_virtual_key_relationships_never_eager_load():
    """select(User) 가 사용자마다 VK 전부(폐기·만료 이력 포함)를 selectin 으로 끌어오던 경로 차단."""
    from app.models.auth import User, VirtualKey

    assert User.virtual_keys.property.lazy == "raise"
    assert User.virtual_keys.property.passive_deletes is True
    assert VirtualKey.user.property.lazy == "raise"


def test_select_user_compiles_without_virtual_key_load():
    from sqlalchemy import select

    from app.models.auth import User

    sql = str(select(User).compile())
    assert "virtual_keys" not in sql


# ── 조직 트리 축약형 ─────────────────────────────────────────────────────────
def _member(name, *, active=True, role="DEVELOPER"):
    return SimpleNamespace(id=uuid.uuid4(), display_name=name, email=f"{name}@x", is_active=active,
                           role=SimpleNamespace(value=role))


def _org():
    lead = _member("lead", role="TEAM_LEADER")
    t1 = SimpleNamespace(id=uuid.uuid4(), name="Alpha", leader_user_id=lead.id,
                         members=[lead, _member("b"), _member("gone", active=False)])
    t2 = SimpleNamespace(id=uuid.uuid4(), name="Empty", leader_user_id=None, members=[])
    dept = SimpleNamespace(id=uuid.uuid4(), name="Dept", teams=[t1, t2])
    return SimpleNamespace(id=uuid.uuid4(), name="Org", departments=[dept]), t1, lead


async def _tree(org, custom, **kw):
    from app.services.user_team_service import UserTeamService

    svc = UserTeamService(cache_mgr=MagicMock(), key_service=MagicMock())
    session = AsyncMock()
    res = MagicMock()
    res.scalars.return_value = list(custom)
    session.execute = AsyncMock(return_value=res)
    with patch("app.services.user_team_service.UserRepository") as Repo:
        Repo.return_value.list_org_tree_lite = AsyncMock(return_value=[org])
        return await svc.get_org_tree(session, **kw)


def _hydrate(node: dict, team: dict | None = None) -> dict:
    """admin-ui lib/utils/orgTreeHydrate.ts 와 같은 규칙(서버 축약형이 손실 없는지 확인용)."""
    keys = ["member_count", "team_count", "leader_name", "leader_user_id", "email", "role",
            "team_name", "has_custom_policies", "custom_policy_count", "team_display_name"]
    meta = {k: node["meta"].get(k) for k in keys}
    if node["type"] == "USER":
        meta["team_name"] = meta["team_name"] or team["name"]
        meta["team_display_name"] = meta["team_display_name"] or team["meta"].get("team_display_name")
        meta["has_custom_policies"] = bool(meta["has_custom_policies"])
        return {**node, "meta": meta, "children": []}
    parent = node if node["type"] == "TEAM" else team
    return {**node, "meta": meta, "children": [_hydrate(c, parent) for c in node["children"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("include_empty", [False, True])
async def test_slim_tree_is_lossless_after_hydration(include_empty):
    org, t1, lead = _org()
    custom = [lead.id]
    full = (await _tree(org, custom, include_empty=include_empty)).model_dump(mode="json")
    slim = await _tree(org, custom, include_empty=include_empty, slim=True)
    assert _hydrate(slim) == full

    team = slim["children"][0]["children"][0]
    by_name = {u["name"]: u for u in team["children"]}
    user = by_name["lead"]
    assert "children" not in user and "team_name" not in user["meta"]
    assert "member_count" not in user["meta"]  # None 생략
    assert user["meta"].get("has_custom_policies") is True
    assert "has_custom_policies" not in by_name["b"]["meta"]  # False 생략
    assert "gone" not in by_name  # 비활성은 트리에 없다
    assert team["meta"]["custom_policy_count"] == 1
    if include_empty:
        empty = slim["children"][0]["children"][1]
        assert empty["children"] == [] and empty["meta"]["member_count"] == 0


@pytest.mark.asyncio
async def test_tree_marks_for_one_team():
    from app.services.user_team_service import UserTeamService

    lead, b, gone = _member("lead", role="TEAM_LEADER"), _member("b"), _member("gone", active=False)
    tid = uuid.uuid4()
    team_res = MagicMock()
    team_res.first.return_value = SimpleNamespace(id=tid, leader_user_id=lead.id)
    rows_res = MagicMock()
    rows_res.all.return_value = [lead, b, gone]
    custom_res = MagicMock()
    custom_res.scalars.return_value = [b.id]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[team_res, rows_res, custom_res])
    svc = UserTeamService(cache_mgr=MagicMock(), key_service=MagicMock())
    out = await svc.get_org_tree_marks(session, tid)
    assert out == {
        "team_id": str(tid),
        "leader_user_id": str(lead.id),
        "leader_name": "lead",
        "leader_email": "lead@x",
        "custom_policy_count": 1,
        "members": [
            {"id": str(lead.id), "role": "TEAM_LEADER", "has_custom_policies": False},
            {"id": str(b.id), "role": "DEVELOPER", "has_custom_policies": True},
        ],
    }


@pytest.mark.asyncio
async def test_tree_marks_unknown_team_404():
    from app.core.exceptions import NotFoundError
    from app.services.user_team_service import UserTeamService

    res = MagicMock()
    res.first.return_value = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=res)
    with pytest.raises(NotFoundError):
        await UserTeamService(cache_mgr=MagicMock(), key_service=MagicMock()).get_org_tree_marks(
            session, uuid.uuid4())


def test_tree_marks_route_registered_before_param_routes():
    from app.routers.users import router

    paths = [r.path for r in router.routes]
    assert "/admin/users/tree/marks" in paths


# ── 사용자 × 모델 scope_total ─────────────────────────────────────────────────
def test_scope_total_sums_all_rows_not_just_the_page():
    from app.services.user_model_usage import page_from_payload

    payload = {
        "period": "2026-11", "timezone": "Asia/Kolkata", "start_date": "2026-11-01",
        "end_date": "2026-11-30", "top_models": [], "other_models": [],
        "users": [
            [f"u{i}", f"n{i}", f"e{i}", "T", [[None, 10, 1, 0, 0, 0.5, 2]]] for i in range(7)
        ],
    }
    out = page_from_payload(payload, page=1, page_size=1)
    assert len(out["rows"]) == 1 and out["total_users"] == 7
    assert out["scope_total"] == {
        "input_tokens": 70, "output_tokens": 7, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "total_tokens": 77, "cost_usd": 3.5, "requests": 14,
    }
    # 검색이 걸리면 검색된 행만 합한다(total_users 와 같은 집합).
    assert page_from_payload(payload, q="n3", page_size=1)["scope_total"]["requests"] == 2


def test_user_models_page_size_cap_is_500():
    import inspect

    from app.routers.analytics import get_user_models

    q = inspect.signature(get_user_models).parameters["page_size"].default
    le = [m.le for m in q.metadata if hasattr(m, "le")]
    assert le == [500]


# ── CSV 내보내기 스트리밍 ─────────────────────────────────────────────────────
class _SessCM:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


async def _drain(resp: StreamingResponse) -> bytes:
    out = b""
    async for c in resp.body_iterator:
        out += c if isinstance(c, bytes) else c.encode()
    return out


@pytest.mark.asyncio
async def test_user_model_export_streams_after_validation():
    from app.routers import analytics

    req_session = AsyncMock()
    body_session = AsyncMock()
    order: list[str] = []
    req_session.close = AsyncMock(side_effect=lambda: order.append("request-session-closed"))

    async def chunks(s, rng, scope_ids, client):
        assert s is body_session
        order.append("query")
        yield "period,timezone\n"
        yield "a,b\n"

    with patch("app.services.analytics_service._resolve_scope",
               AsyncMock(return_value=(None, None))), \
         patch("app.services.user_model_usage.export_csv_chunks", chunks), \
         patch("app.core.db.AnalyticsSessionLocal", lambda: _SessCM(body_session)):
        resp = await analytics.export_analytics(
            MagicMock(), format="csv", period="2026-11", group_by="model", scope="all",
            client=None, start_date=None, end_date=None, view="user_model", user=ADMIN,
            session=req_session)
        assert isinstance(resp, StreamingResponse)
        assert resp.headers["content-disposition"].startswith("attachment;")
        assert order == ["request-session-closed"]  # 쿼리는 헤더 뒤(본문 생성기)에서 돈다
        assert await _drain(resp) == b"period,timezone\na,b\n"
    assert order == ["request-session-closed", "query"]
    body_session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_export_scope_403_happens_before_streaming():
    from app.core.exceptions import ForbiddenError
    from app.routers import analytics

    with patch("app.services.analytics_service._resolve_scope",
               AsyncMock(side_effect=ForbiddenError("no"))):
        with pytest.raises(ForbiddenError):
            await analytics.export_analytics(
                MagicMock(), format="csv", period="2026-11", group_by="team", scope="team:x",
                client=None, start_date=None, end_date=None, view=None, user=ADMIN,
                session=AsyncMock())


@pytest.mark.asyncio
async def test_export_bad_range_400_before_streaming():
    from app.core.exceptions import ValidationError
    from app.routers import analytics

    with pytest.raises(ValidationError):
        await analytics.export_analytics(
            MagicMock(), format="csv", period="2026-11", group_by="team", scope="all",
            client=None, start_date="2026-11-01", end_date=None, view=None, user=ADMIN,
            session=AsyncMock())


@pytest.mark.asyncio
async def test_aggregate_export_streams_service_output():
    from app.routers import analytics

    svc = MagicMock()
    svc.export_analytics = AsyncMock(return_value=("period,team\n", "text/csv"))
    request = MagicMock()
    request.app.state.analytics_service = svc
    body_session = AsyncMock()
    sid = [uuid.uuid4()]
    with patch("app.services.analytics_service._resolve_scope",
               AsyncMock(return_value=(None, sid))), \
         patch("app.core.db.AnalyticsSessionLocal", lambda: _SessCM(body_session)):
        resp = await analytics.export_analytics(
            request, format="csv", period="2026-11", group_by="team", scope="all", client=None,
            start_date=None, end_date=None, view=None, user=ADMIN, session=AsyncMock())
        svc.export_analytics.assert_not_called()
        assert await _drain(resp) == b"period,team\n"
    assert svc.export_analytics.await_args.kwargs["scope_ids"] == sid
    assert resp.headers["content-disposition"] == 'attachment; filename="analytics_2026-11.csv"'
