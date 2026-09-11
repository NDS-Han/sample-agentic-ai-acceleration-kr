# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""POST /v1/auth/exchange — OIDC JWT → Virtual Key.

cli (gateway-cli) 가 IDP 로부터 받은 access JWT 를 이 엔드포인트로 교환.
Admin JWT 인증 불필요 (사용자 신원은 OIDC JWT 자체로).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.db import get_db_session
from app.core.oidc_verifier import OIDCConfigError
from app.schemas.cli import VirtualKeyIssueResponse
from app.schemas.oidc import OIDCExchangeRequest
from app.services.oidc_service import (
    OIDCAuthError,
    OIDCNotProvisionableError,
    OIDCService,
)

router = APIRouter(prefix="/v1/auth", tags=["OIDC Auth"])


def _oidc_error(status_code: int, err_type: str, message: str) -> HTTPException:
    """프로젝트 에러 봉투를 **한 번만** 씌운 HTTPException 을 만든다.

    ⚠️ 이 라우터는 상태코드로 표현할 수 없는 type(oidc_disabled / idp_unavailable /
    not_provisionable)을 쓰려고 detail 에 봉투를 직접 넣는다. 그런데 FastAPI 기본
    핸들러가 detail 을 한 번 더 감싸서 응답이 `{"detail": {"error": {...}}}` **이중
    봉투**로 나갔다(실측) — 클라이언트는 다른 엔드포인트와 달리 한 단계를 더 파고
    들어야 error.type 을 볼 수 있었다. 이제 main.py 의 http_exception_handler 가
    "이미 봉투인 detail 은 통과" 시키므로 응답은 `{"error": {...}}` 하나가 된다.
    (그 통과 규칙이 사라지면 이중 봉투가 조용히 돌아온다 →
    tests/regression/test_medium_error_envelope_normalization.py 가 못 박는다.)

    code 는 다른 봉투들과 마찬가지로 채운다. 비어 있으면 admin-ui api-client 가
    error_code 를 'UNKNOWN_ERROR' 로 표시한다(admin-ui/src/lib/api-client.ts:54).
    """
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "type": err_type,
                "code": err_type.upper(),
                "message": message,
            }
        },
    )


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _oidc_error(401, "invalid_token", "Missing Bearer token")
    token = auth[len("Bearer "):].strip()
    if not token:
        raise _oidc_error(401, "invalid_token", "Empty Bearer token")
    return token


@router.post("/exchange", response_model=VirtualKeyIssueResponse)
async def exchange_oidc_jwt(
    request: Request,
    body: OIDCExchangeRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """OIDC access JWT 를 Virtual Key 로 교환.

    Auth: Authorization: Bearer <OIDC access JWT>
    """
    svc: OIDCService | None = request.app.state.oidc_service
    if svc is None:
        raise _oidc_error(
            503,
            "oidc_disabled",
            "OIDC is not configured. Set OIDC_ISSUER_URL + OIDC_AUDIENCE.",
        )

    token = _extract_bearer(request)
    redis = request.app.state.redis

    try:
        return await svc.exchange_jwt_for_vk(
            session,
            redis=redis,
            token=token,
            device_name=body.device_name,
            sso_session_expires_at=body.sso_session_expires_at,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )
    except OIDCAuthError as e:
        raise _oidc_error(401, "invalid_token", str(e))
    except OIDCNotProvisionableError as e:
        raise _oidc_error(403, "not_provisionable", str(e))
    except OIDCConfigError as e:
        raise _oidc_error(503, "idp_unavailable", str(e))
