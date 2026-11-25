# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/users 재설계용 최소 백엔드 — 트리의 팀 표시 이름 + /admin/users/activity.

  1. TEAM·USER 노드의 ``team_display_name`` 은 예산 관리와 같은 규칙(기본 부서가 아니면
     "부서_팀") — 화면 간 팀 이름이 같아야 대조할 수 있다.
  2. ``GET /admin/users/activity`` — 요청한 id 마다 한 행(last_used_at, web_searches),
     200명 상한, ``/users/{user_id}`` 보다 먼저 선언(경로 충돌 방지).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers import users as users_router
from tests.regression.test_high_org_tree_count_semantics import _build, _dept, _member, _org, _team


async def test_team_display_name_follows_budget_rule(monkeypatch):
    default_dept = _dept(name="Default Department", teams=[_team(name="Default Team", members=[_member(name="d")])])
    nds = _dept(name="NDS", teams=[_team(name="Developers", members=[_member(name="n")])])
    ssir = _dept(name="SSIR", teams=[_team(name="Developers", members=[_member(name="s")])])
    settings = SimpleNamespace(DEFAULT_DEPT_ID=str(default_dept.id))
    with patch("app.services.user_team_service.get_settings", return_value=settings):
        tree = await _build(_org(name="Org", departments=[default_dept, nds, ssir]))
    by_dept = {d.name: d.children[0] for d in tree.children}
    assert by_dept["Default Department"].meta.team_display_name == "Default Team"
    assert by_dept["NDS"].meta.team_display_name == "NDS_Developers"
    assert by_dept["SSIR"].meta.team_display_name == "SSIR_Developers"  # 같은 'Developers' 둘이 구분된다
    # 사용자 노드도 소속 팀의 같은 표시 이름
    assert by_dept["NDS"].children[0].meta.team_display_name == "NDS_Developers"
    assert by_dept["NDS"].name == "Developers"  # 트리 라벨(name)은 그대로


def _session(last_rows, ws_rows):
    s = AsyncMock()
    r1, r2 = MagicMock(), MagicMock()
    r1.all.return_value = last_rows
    r2.all.return_value = ws_rows
    s.execute = AsyncMock(side_effect=[r1, r2])
    return s


async def test_activity_one_row_per_requested_id_with_defaults():
    a, b = uuid.uuid4(), uuid.uuid4()
    t = datetime(2026, 11, 6, 8, 50, tzinfo=UTC)
    from types import SimpleNamespace as _NS

    s = _session([(a, t), (b, None)], [_NS(user_id=a, q=3)])
    r = await users_router.get_users_activity(user_id=[a, b, a], period="2026-11", admin=None, session=s)
    assert r.period == "2026-11"
    assert [(i.user_id, i.last_used_at, i.web_searches) for i in r.items] == [
        (str(a), t, 3),
        (str(b), None, 0),  # 사용 기록 없음 → None / 0
    ]
    # 중복 id 는 한 번만 조회
    assert s.execute.await_args_list[0].args[1] == {"ids": [a, b]}
    # 웹 검색 수는 하이브리드 롤업 읽기 — 같은 사용자 집합·그 달로 좁힌다.
    ws_params = s.execute.await_args_list[1].args[1]
    assert ws_params["r_users"] == [str(a), str(b)]
    assert str(ws_params["r_start"]) == "2026-11-01" and str(ws_params["r_end"]) == "2026-11-30"


async def test_activity_defaults_to_current_period():
    s = _session([], [])
    with patch("app.core.usage_filters.current_kst_period", return_value="2026-12"):
        r = await users_router.get_users_activity(user_id=[uuid.uuid4()], period=None, admin=None, session=s)
    assert r.period == "2026-12"


async def test_activity_caps_ids_at_200():
    with pytest.raises(HTTPException) as ei:
        await users_router.get_users_activity(
            user_id=[uuid.uuid4() for _ in range(201)], period=None, admin=None, session=AsyncMock()
        )
    assert ei.value.status_code == 422


def test_activity_route_is_declared_before_get_user_id_routes():
    gets = [r.path for r in users_router.router.routes if "GET" in getattr(r, "methods", set())]
    act = gets.index("/admin/users/activity")
    dynamic = [i for i, p in enumerate(gets) if p.startswith("/admin/users/{user_id}")]
    assert dynamic and act < min(dynamic)
