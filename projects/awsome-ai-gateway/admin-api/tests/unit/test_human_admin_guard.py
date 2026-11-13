"""require_human_admin — svc 토큰이 파괴/우회 표면의 mutation 에 도달 못 함.

R22: svc- 토큰은 require_admin 을 통과하므로 라우팅 ARN 재기록(프롬프트
리디렉션)·예산 spent 리셋·레이트리밋 해제·전량 sync·모델 카탈로그/정책 쓰기는
사람 admin 전용이어야 한다. 가드가 빠져도 핸들러에 require_admin 만 남아
조용히 통과하므로, route.dependencies 를 함수 identity 로 직접 검증한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

from app.core.auth import require_human_admin
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole


def _dep_fns(route: APIRoute):
    return {d.dependency for d in route.dependencies}


def _routes(router):
    return [r for r in router.routes if isinstance(r, APIRoute)]


@pytest.mark.parametrize(
    "router_path",
    [
        "app.routers.routing",
        "app.routers.models",
        "app.routers.budgets",
        "app.routers.rate_limits",
        "app.routers.apps",
        "app.routers.users",
        "app.routers.chat_agent",
    ],
)
def test_every_mutation_route_requires_human_admin(router_path):
    """보호 라우터의 모든 POST/PUT/DELETE/PATCH 라우트에 가드 등록."""
    import importlib

    mod = importlib.import_module(router_path)
    router = mod.router
    mutations = [
        r for r in _routes(router)
        if r.methods & {"POST", "PUT", "DELETE", "PATCH"}
    ]
    assert mutations, f"{router_path}: expected mutation routes"
    for route in mutations:
        assert require_human_admin in _dep_fns(route), (
            f"{router_path} {route.path} {route.methods} missing "
            "require_human_admin"
        )


def test_chat_agent_router_level_guard():
    """chat_agent 는 라우터 레벨 — GET 도 svc 토큰 차단."""
    from app.routers.chat_agent import router

    assert require_human_admin in {d.dependency for d in router.dependencies}


def _svc_admin():
    u = MagicMock()
    u.is_service_token = True
    u.service_token_id = uuid.uuid4()
    u.user_id = uuid.UUID(int=0x11)
    u.role = UserRole.ADMIN
    u.team_id = None
    u.is_service_account = False
    return u


@pytest.mark.asyncio
async def test_require_human_admin_rejects_svc_and_passes_human():
    svc = _svc_admin()
    with pytest.raises(ForbiddenError):
        await require_human_admin(admin=svc)

    human = _svc_admin()
    human.is_service_token = False
    human.service_token_id = None
    assert await require_human_admin(admin=human) is human
