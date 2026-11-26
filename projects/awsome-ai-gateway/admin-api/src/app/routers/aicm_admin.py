# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""`/aicm/v1/admin/*` — AICM client contract 의 26개만 선택해 `/admin` handler 를 미러링.

라우트 로직을 복사하지 않는다: 원래 `APIRoute.endpoint` 와 request/response schema 를
그대로 다시 등록하고, mirror 전용 dependency 만 앞에 붙인다. 따라서 본문·status code·
204·query validation·x-request-id 처리는 `/admin` 과 같은 코드 경로다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError
from app.core.integration_auth import (
    RL_READ,
    RL_SYNC,
    RL_WRITE,
    require_integration,
)
from app.repositories.user_repository import UserRepository
from app.routers import budgets, models, service_tokens, users
from app.services.integration_token_service import SERVICE_TOKEN_PREFIX
from app.services.protected_targets import protected_user_target_code

logger = structlog.get_logger()


@dataclass(frozen=True)
class MirrorSpec:
    method: str
    path: str
    scope: str
    rl_class: str
    user_target_guard: bool = False


# AICM_CLIENT_CONTRACT.md 의 26행 — 순서·method·path template 이 계약의 source of truth 다.
MIRROR_SPECS: tuple[MirrorSpec, ...] = (
    MirrorSpec("POST", "/admin/users/sync-cognito/user/{username}", "users:sync", RL_SYNC),
    MirrorSpec("POST", "/admin/users/sync-cognito/group/{groupname:path}", "users:sync", RL_SYNC),
    MirrorSpec("POST", "/admin/users/sync-cognito", "users:sync", RL_SYNC),
    MirrorSpec("PUT", "/admin/users/{user_id}/allowed-clients", "users:write", RL_WRITE, True),
    MirrorSpec("PUT", "/admin/users/{user_id}/allowed-models", "users:write", RL_WRITE, True),
    MirrorSpec("DELETE", "/admin/users/{user_id}/allowed-clients", "users:write", RL_WRITE, True),
    MirrorSpec("DELETE", "/admin/users/{user_id}/allowed-models", "users:write", RL_WRITE, True),
    MirrorSpec("GET", "/admin/users/{user_id}/allowed-clients", "users:read", RL_READ),
    MirrorSpec("GET", "/admin/users/{user_id}/allowed-models", "users:read", RL_READ),
    MirrorSpec("GET", "/admin/users", "users:read", RL_READ),
    MirrorSpec("GET", "/admin/teams/{team_id}/allowed-models", "users:read", RL_READ),
    MirrorSpec("PUT", "/admin/budgets/user/{user_id}", "budgets:write", RL_WRITE, True),
    MirrorSpec("PUT", "/admin/budgets/user/{user_id}/app/{client}", "budgets:write", RL_WRITE, True),
    MirrorSpec("PUT", "/admin/budgets/user/{user_id}/app-group/{group}", "budgets:write", RL_WRITE, True),
    MirrorSpec("PUT", "/admin/budgets/team/{team_id}", "budgets:write", RL_WRITE),
    MirrorSpec("PUT", "/admin/budgets/spent", "budgets:write", RL_WRITE),
    MirrorSpec("GET", "/admin/budgets/user/{user_id}/apps", "budgets:read", RL_READ),
    MirrorSpec("GET", "/admin/budgets/user/{user_id}/app-groups", "budgets:read", RL_READ),
    MirrorSpec("DELETE", "/admin/budgets/user/{user_id}/app-group/{group}", "budgets:write", RL_WRITE, True),
    MirrorSpec("GET", "/admin/budgets/summary", "budgets:read", RL_READ),
    MirrorSpec("GET", "/admin/budgets/summary/team/{team_id}/users", "budgets:read", RL_READ),
    # contract #22 — scope=USER 도 같은 method+path(#20)를 쓴다. 라우트는 한 번만 mount 하고
    # spec 행은 계약과 수를 맞추기 위해 유지한다.
    MirrorSpec("GET", "/admin/budgets/summary", "budgets:read", RL_READ),
    MirrorSpec("GET", "/admin/budgets/usage-by-app", "budgets:read", RL_READ),
    MirrorSpec("POST", "/admin/service-tokens/rotate", "tokens:rotate", RL_WRITE),
    MirrorSpec("GET", "/admin/service-tokens", "tokens:read", RL_READ),
    MirrorSpec("GET", "/admin/models", "models:read", RL_READ),
)


async def guard_user_target(
    request: Request,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """AICM 이 ADMIN/system/service-account 사용자를 바꾸지 못하게 한다(403 + audit).

    사람 admin 의 `/admin` 행동은 바꾸지 않고 mirror dependency 에만 붙는다. 읽기에는
    적용하지 않는다 — 계약상 조회는 대상 제한이 없고, lifecycle 의 쓰기 보호와 같은 표면이다.
    """
    user = await UserRepository(session).get_user(user_id)
    if user is None:
        return  # 실제 handler 와 같은 404 semantics 유지
    code = protected_user_target_code(user_id, user)
    if code is None:
        return
    principal = getattr(request.state, "integration_principal", None)
    actor = principal.actor if principal is not None else None
    await audit_logger.log(
        session,
        actor_user_id=actor.user_id if actor else user_id,
        actor_role=actor.role.value if actor else "UNKNOWN",
        service_token_id=actor.service_token_id if actor else None,
        action="AICM_ADMIN_TARGET_REFUSED",
        resource_type="User",
        resource_id=str(user_id),
        result="FAILURE",
        changes={"after": {"code": code, "path": request.url.path, "method": request.method}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
        queue_only=True,  # 거부 응답은 세션 rollback — refusal audit 는 큐로 남긴다
    )
    raise ForbiddenError(f"{code}: integration cannot modify gateway admins, system users, or service accounts")


def _source_routes() -> list[APIRoute]:
    out: list[APIRoute] = []
    for module in (users, budgets, service_tokens, models):
        out.extend(r for r in module.router.routes if isinstance(r, APIRoute))
    return out


def build_router() -> APIRouter:
    """원본 route metadata 로 mirror router 를 만든다. 없는 계약 경로는 즉시 실패."""
    router = APIRouter(prefix="/aicm/v1", tags=["AICM Admin Mirror"])
    sources = _source_routes()
    mounted: set[tuple[str, str]] = set()
    for spec in MIRROR_SPECS:
        # endpoint 20/22 는 같은 GET /summary 를 공유하므로 한 번만 등록한다.
        if (spec.method, spec.path) in mounted:
            continue
        route = next(
            (r for r in sources if r.path == spec.path and spec.method in (r.methods or set())),
            None,
        )
        if route is None:
            raise RuntimeError(f"AICM mirror source route missing: {spec.method} {spec.path}")
        deps = [Depends(require_integration(spec.scope, spec.rl_class, token_prefix=SERVICE_TOKEN_PREFIX))]
        if spec.user_target_guard:
            deps.append(Depends(guard_user_target))
        deps.extend(route.dependencies or [])
        router.add_api_route(
            route.path,
            route.endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=["AICM Admin Mirror"],
            dependencies=deps,
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=dict(route.responses or {}),
            deprecated=route.deprecated,
            methods=list(route.methods or {spec.method}),
            operation_id=f"aicm_{route.operation_id or route.name}",
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=True,
            response_class=route.response_class,
            name=f"aicm_{route.name}",
            route_class_override=type(route),
            callbacks=route.callbacks,
            openapi_extra=route.openapi_extra,
            generate_unique_id_function=route.generate_unique_id_function,
        )
        mounted.add((spec.method, spec.path))
    return router


def contract_method_paths() -> frozenset[tuple[str, str]]:
    return frozenset((s.method, s.path) for s in MIRROR_SPECS)


router = build_router()
