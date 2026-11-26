# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 — AICM(외부 비용관리 시스템) 통합 API phase 0a.

``AICM_API_ENABLED=true`` 일 때만 ``main.create_app`` 이 이 라우터를 마운트한다.

범위(phase 0a): 통합 토큰 bootstrap/list/rotate, ``POST /users/provision``,
``POST /users/{user_id}/deactivate``. 정책·예산·sync·reactivation 등 나머지 P0/P1 엔드포인트는
AICM 미결 질문에 달려 있어 이 라우터에 없다.

- 인증: ``itg-`` 통합 토큰 + scope(``core/integration_auth.py``). svc- 토큰·사람 JWT 는 401.
  예외는 토큰 발급(``POST /aicm/v1/service-tokens``) 하나 — 사람 admin(JWT) 전용.
- 토큰 rotate/provision/deactivate: ``Idempotency-Key`` 필수, 감사 행에 ``_integration`` 태그.
- 업무 로직은 기존 서비스 계층을 재사용한다 — 이 모듈은 인증·멱등·오류 매핑만 한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_human_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.idempotency import run_idempotent
from app.core.integration_auth import (
    RL_LIFECYCLE,
    IntegrationPrincipal,
    client_ip,
    require_integration,
)
from app.services.integration_token_service import INTEGRATION_AICM, IntegrationTokenService

router = APIRouter(prefix="/aicm/v1", tags=["AICM Integration"])

_IDEM_HEADER_DOC = "Idempotency-Key 헤더 필수(1–128자). 같은 키+같은 요청 → 저장된 응답 재생."


# ── schemas (이름은 Aicm 접두사 — 기존 OpenAPI 컴포넌트 이름과 겹치지 않게) ──────────


class AicmTokenIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1, max_length=8)
    allowed_cidrs: list[str] | None = Field(None, max_length=16, description="호출 허용 원천 IP 대역. 생략 = 제한 없음")
    rate_limit_per_min: int | None = Field(None, ge=1, le=10000, description="write 등급 분당 상한 재정의")
    expiry_days: int | None = Field(None, ge=1, le=365)


class AicmTokenIssueResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    token: str = Field(description="평문 토큰 — 발급/rotate 응답에서 1회만")
    integration: str
    scopes: list[str]
    allowed_cidrs: list[str] | None = None
    rate_limit_per_min: int | None = None


class AicmTokenItem(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    rotated_from: str | None = None
    integration: str
    scopes: list[str]
    allowed_cidrs: list[str] | None = None
    rate_limit_per_min: int | None = None


class AicmTokenListResponse(BaseModel):
    items: list[AicmTokenItem]


class AicmRotateResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    token: str


class AicmLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attribute: str = Field(
        "Cognito_Subject",
        description="SAML 쪽 식별 속성. Cognito_Subject = NameID(권장) 또는 매핑된 속성 이름(예: email)",
    )
    value: str = Field(min_length=1, max_length=2048)


class AicmProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(None, max_length=255)
    groups: list[str] = Field(min_length=1, max_length=20)
    link: AicmLink | None = None


class AicmProvisionResponse(BaseModel):
    user_id: str
    cognito_username: str
    state: str = Field(description="created | created_unlinked | linked | existing | already_federated")
    team_id: str | None = None
    role: str | None = None
    is_active: bool | None = None
    sync_errors: list[str] = []


class AicmDeactivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    global_sign_out: bool = True


class AicmDeactivateResponse(BaseModel):
    user_id: str
    state: str = Field(description="deactivated | already_inactive")
    cognito: str
    revoked_key_count: int


# ── helpers ──────────────────────────────────────────────────────────────────


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "") or request.headers.get("x-request-id", "")


def _lifecycle(request: Request):
    from app.services.cognito_admin import CognitoAdmin, build_client
    from app.services.user_lifecycle_service import UserLifecycleService

    client = getattr(request.app.state, "aicm_cognito_client", None)
    if client is None and get_settings().COGNITO_USER_POOL_ID:
        client = build_client()
        request.app.state.aicm_cognito_client = client
    redis = getattr(request.app.state, "redis", None)
    return UserLifecycleService(
        CognitoAdmin(client, redis=redis),
        key_service=request.app.state.key_service,
        cache_mgr=request.app.state.cache_mgr,
        redis=redis,
    )


def _idem(request, session, handler, **kw):
    return run_idempotent(request, session, integration=INTEGRATION_AICM, handler=handler, **kw)


# ── service tokens ───────────────────────────────────────────────────────────


@router.post("/service-tokens", response_model=AicmTokenIssueResponse)
async def issue_integration_token(
    request: Request,
    body: AicmTokenIssueRequest,
    admin: CurrentUser = Depends(require_human_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """통합 토큰 발급 — **사람 admin(JWT) 전용**. 평문은 이 응답에서 1회만. scope 는 발급 후 넓힐 수 없다."""
    svc = IntegrationTokenService(request.app.state.service_token_service)
    resp, scopes, cidrs = await svc.issue(
        session, name=body.name, created_by=admin.user_id, scopes=body.scopes,
        allowed_cidrs=body.allowed_cidrs, rate_limit_per_min=body.rate_limit_per_min,
        expiry_days=body.expiry_days,
    )
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="SERVICE_TOKEN_ISSUE",
        resource_type="ServiceToken",
        resource_id=str(resp.id),
        changes={"after": {
            "name": body.name, "expiry_days": body.expiry_days, "kind": "integration",
            "integration": INTEGRATION_AICM, "scopes": scopes, "allowed_cidrs": cidrs,
            "rate_limit_per_min": body.rate_limit_per_min,
        }},
        ip_address=client_ip(request),
        request_id=_rid(request),
    )
    return AicmTokenIssueResponse(
        **resp.model_dump(), integration=INTEGRATION_AICM, scopes=scopes, allowed_cidrs=cidrs,
        rate_limit_per_min=body.rate_limit_per_min,
    )


@router.get("/service-tokens", response_model=AicmTokenListResponse)
async def list_integration_tokens(
    p: IntegrationPrincipal = Depends(require_integration("tokens:read")),
    session: AsyncSession = Depends(get_db_session),
):
    """호출 토큰과 같은 rotate 계보의 토큰만(다른 통합/계보는 보이지 않는다)."""
    items = await IntegrationTokenService().list_lineage(session, p.token.lineage_id)
    return AicmTokenListResponse(items=[AicmTokenItem(**i) for i in items])


@router.post("/service-tokens/rotate", response_model=AicmRotateResponse, description=_IDEM_HEADER_DOC)
async def rotate_integration_token(
    request: Request,
    p: IntegrationPrincipal = Depends(require_integration("tokens:rotate")),
    session: AsyncSession = Depends(get_db_session),
):
    """self-rotate — 구 토큰 24h grace, scope/CIDR 복사. 재전송은 새 토큰을 다시 주지 않는다(409)."""

    async def handler():
        svc = IntegrationTokenService(request.app.state.service_token_service)
        resp = await svc.rotate(session, token_id=p.token.token_id, actor_user_id=p.actor.user_id)
        await audit_logger.log(
            session,
            actor_user_id=p.actor.user_id,
            actor_role=p.actor.role.value,
            service_token_id=p.token.token_id,
            action="SERVICE_TOKEN_ROTATE",
            resource_type="ServiceToken",
            resource_id=str(resp.id),
            changes={"before": {"token_id": str(p.token.token_id)}, "after": {"token_id": str(resp.id)}},
            ip_address=p.client_ip,
            request_id=_rid(request),
        )
        return 200, AicmRotateResponse(**resp.model_dump())

    return await _idem(request, session, handler, secret_fields=("token",))


# ── users ────────────────────────────────────────────────────────────────────


@router.post("/users/provision", response_model=AicmProvisionResponse, description=_IDEM_HEADER_DOC)
async def provision_user(
    request: Request,
    body: AicmProvisionRequest,
    p: IntegrationPrincipal = Depends(require_integration("users:provision", RL_LIFECYCLE)),
    session: AsyncSession = Depends(get_db_session),
):
    """Cognito AdminCreateUser(초대 메일 없음, 그룹 속성) → AdminLinkProviderForUser(ADFS SAML) →
    게이트웨이 sync. 각 단계는 확인 후 실행이라 재시도하면 이어서 끝낸다."""

    async def handler():
        out, after = await _lifecycle(request).provision(
            session, email=body.email, display_name=body.display_name, groups=body.groups,
            link=body.link.model_dump() if body.link else None, actor=p.actor,
            ip_address=p.client_ip, request_id=_rid(request),
        )
        return 200, out, after

    return await _idem(request, session, handler)


@router.post("/users/{user_id}/deactivate", response_model=AicmDeactivateResponse, description=_IDEM_HEADER_DOC)
async def deactivate_user(
    request: Request,
    user_id: uuid.UUID,
    body: AicmDeactivateRequest,
    p: IntegrationPrincipal = Depends(require_integration("users:lifecycle", RL_LIFECYCLE)),
    session: AsyncSession = Depends(get_db_session),
):
    """Cognito AdminDisableUser(+GlobalSignOut) → DB is_active=false + VK 전량 폐기 + 감사(한 트랜잭션)
    → 커밋 후 Redis DEL + 게이트웨이 pub/sub(user_ids). ADMIN·서비스 계정·시스템 사용자는 403."""

    async def handler():
        out, after = await _lifecycle(request).deactivate(
            session, user_id=user_id, reason=body.reason, global_sign_out=body.global_sign_out,
            actor=p.actor, ip_address=p.client_ip, request_id=_rid(request),
        )
        return 200, out, after

    return await _idem(request, session, handler)
