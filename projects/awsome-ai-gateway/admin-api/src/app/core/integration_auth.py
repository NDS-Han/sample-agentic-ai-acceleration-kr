# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 인증 의존성 — scoped 통합 토큰, 원천 IP(CIDR), 토큰 계보별 레이트리밋.

legacy lifecycle 라우터는 ``itg-`` 토큰을, `/aicm/v1/admin/*` mirror 는 AICM scope 를
가진 ``svc-`` 토큰을 받는다. integration grant 가 있는 ``svc-`` 는 real `/admin/*` 에서는
`core.auth.get_current_user` 가 거부한다 — mirror/people surface 분리.

판정 순서: 형식/해시(401) → 통합 이름(401) → 원천 IP(403) → scope(403) → 레이트리밋(429).
IP 를 scope 보다 먼저 보는 이유: 허용 대역 밖의 호출자에게 토큰의 권한 구성을 드러내지 않는다.
"""
from __future__ import annotations

import inspect
import ipaddress
import time
import uuid
from dataclasses import dataclass

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_tags
from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, get_db_session
from app.core.exceptions import IntegrationError
from app.models.auth import UserRole
from app.services.integration_token_service import (
    INTEGRATION_AICM,
    IntegrationTokenRecord,
    IntegrationTokenService,
)
from app.services.service_token_service import INTEGRATION_TOKEN_PREFIX, SERVICE_TOKEN_PREFIX

logger = structlog.get_logger()

#: 통합 호출자의 FK 용 합성 사용자 — svc- 토큰과 같은 행(03_seed_data.sql).
INTEGRATION_ACTOR_USER_ID = uuid.UUID("00000000-0000-4000-a000-000000000011")

RL_READ = "read"
RL_WRITE = "write"
RL_LIFECYCLE = "lifecycle"
RL_SYNC = "sync"

MIRROR_PATH_PREFIX = "/aicm/v1/admin/"


@dataclass(frozen=True)
class IntegrationPrincipal:
    actor: CurrentUser  # 서비스 계층에 넘기는 행위자(합성 ADMIN, service_token_id = 토큰 id)
    token: IntegrationTokenRecord
    client_ip: str

    @property
    def integration(self) -> str:
        return self.token.integration


def client_ip(request: Request) -> str:
    """직접 프록시(ALB)가 붙인 XFF 가장 오른쪽 홉 — routers/auth_oidc.py 와 같은 규칙."""
    from app.routers.auth_oidc import _client_ip

    return _client_ip(request)


def _unauthorized(message: str = "Invalid or expired integration token") -> IntegrationError:
    return IntegrationError(message, code="invalid_integration_token", status_code=401)


def _bearer(request: Request) -> str:
    # 쿠키 폴백 없음 — 통합 토큰은 Authorization 헤더로만 받는다(브라우저 세션과 분리).
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized("Missing or invalid Authorization header")
    return auth[7:].strip()


def _ip_allowed(ip: str, cidrs) -> bool:
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in cidrs if net.version == addr.version)


def _actor_for(rec: IntegrationTokenRecord) -> CurrentUser:
    return CurrentUser(
        user_id=INTEGRATION_ACTOR_USER_ID,
        email=f"integration-{rec.integration}@admin.local",
        role=UserRole.ADMIN,
        team_id=None,
        is_service_token=True,
        service_token_id=rec.token_id,
    )


def _principal_for(rec: IntegrationTokenRecord, ip: str) -> IntegrationPrincipal:
    return IntegrationPrincipal(actor=_actor_for(rec), token=rec, client_ip=ip)


async def authenticate_mirror_actor(request: Request) -> CurrentUser:
    """`/aicm/v1/admin/*` 의 `get_current_user` 대체 인증.

    scope/rate-limit 검사는 route-level `require_integration` 이 한다 — 여기서는
    신원이 "AICM integration grant 가 있는 svc- 토큰"임만 확정한다. 사람 JWT/개발
    토큰/일반 svc- 는 mirror 에서 절대 사용자 신원으로 해석하지 않는다.
    """
    cached = getattr(request.state, "integration_principal", None)
    if cached is not None:
        return cached.actor
    if not get_settings().AICM_API_ENABLED:
        raise _unauthorized()
    raw = _bearer(request)
    if not raw.startswith(SERVICE_TOKEN_PREFIX):
        # mirror 에서 사람 JWT 를 서비스 토큰으로 오인하지 않게 401 로 끊는다.
        raise _unauthorized("AICM mirror requires a scoped svc integration token")
    async with AsyncSessionLocal() as session:
        rec = await _verify_token(session, raw, (SERVICE_TOKEN_PREFIX,))
    if rec is None or rec.integration != INTEGRATION_AICM:
        raise _unauthorized()
    ip = client_ip(request)
    if not _ip_allowed(ip, rec.allowed_cidrs):
        logger.warning("aicm.source_ip_rejected", token_id=str(rec.token_id), ip=ip)
        raise IntegrationError(
            "source IP is not allowed for this integration token",
            code="source_ip_not_allowed", status_code=403,
        )
    principal = _principal_for(rec, ip)
    request.state.integration_principal = principal
    return principal.actor


# Redis 장애 시 프로세스 로컬 고정창 — {key: (window, count)}.
_LOCAL_RL: dict[str, tuple[int, int]] = {}


async def _hit(redis, key: str, window_s: int, limit: int) -> tuple[bool, int]:
    """고정창 카운터 +1. 반환 (허용 여부, Retry-After 초)."""
    now = time.time()
    window = int(now // window_s)
    retry_after = max(1, int((window + 1) * window_s - now) + 1)
    full = f"{key}:{window}"
    if redis is not None:
        try:
            n = await redis.incr(full)
            if n == 1:
                await redis.expire(full, window_s + 5)
            return n <= limit, retry_after
        except Exception:
            logger.warning("aicm.rate_limit_redis_unavailable", key=key)
    # Redis 장애: 통합 경로는 제어면이고 scope/IP 로 이미 좁혀져 있다 — replica 로컬 창으로
    # 계속 센다(차단을 풀지 않는다). cap 을 replica 수 가정치로 나눠 전체 상한을 크게 넘지 않게 한다.
    local_limit = max(1, limit // max(1, get_settings().AICM_COGNITO_FALLBACK_DIVISOR))
    w, c = _LOCAL_RL.get(full, (window, 0))
    c += 1
    _LOCAL_RL[full] = (window, c)
    if len(_LOCAL_RL) > 10000:
        for k, (wkey, _) in list(_LOCAL_RL.items()):
            if wkey < window:
                del _LOCAL_RL[k]
    return c <= local_limit, retry_after


async def enforce_rate_limit(request: Request, rec: IntegrationTokenRecord, rl_class: str) -> None:
    s = get_settings()
    if rl_class == RL_READ:
        limit = s.AICM_RL_READ_PER_MIN
    elif rl_class == RL_LIFECYCLE:
        limit = s.AICM_RL_LIFECYCLE_PER_MIN
    elif rl_class == RL_SYNC:
        limit = getattr(s, "AICM_RL_SYNC_PER_MIN", 10)
    else:
        limit = rec.rate_limit_per_min or s.AICM_RL_WRITE_PER_MIN
    redis = getattr(request.app.state, "redis", None)
    ok, retry_after = await _hit(redis, f"itg:rl:{rec.lineage_id}:{rl_class}", 60, limit)
    if not ok:
        raise IntegrationError(
            f"integration rate limit exceeded ({rl_class}: {limit}/min)",
            code="integration_rate_limited", status_code=429, retry_after=retry_after,
        )


async def _verify_token(
    session: AsyncSession, raw: str, prefixes: tuple[str, ...]
) -> IntegrationTokenRecord | None:
    """IntegrationTokenService.verify — 테스트의 기존 monkeypatch( prefixes 파라미터 없음)와 호환."""
    svc = IntegrationTokenService()
    if "prefixes" in inspect.signature(svc.verify).parameters:
        return await svc.verify(session, raw, prefixes=prefixes)
    return await svc.verify(session, raw)


def require_integration(
    scope: str,
    rl_class: str | None = None,
    *,
    token_prefix: str = INTEGRATION_TOKEN_PREFIX,
):
    """scope 를 가진 integration 토큰만 통과시키는 의존성. rl_class 기본값: GET=read, 그 외 write.

    mirror 경로에서는 `authenticate_mirror_actor` 가 미리 넣은 principal 을 재사용해
    중복 해시조회를 피하고, 이 의존성은 scope/레이트리밋/audit 태그를 담당한다.
    """

    async def _dep(
        request: Request, session: AsyncSession = Depends(get_db_session)
    ):
        raw = _bearer(request)
        if not raw.startswith(token_prefix):
            # 표면 혼용은 유효한 integration grant 가 있을 때만 403 으로 구분한다.
            # 일반 svc-/모르는 토큰은 기존처럼 401 — 권한 구성을 유추시키지 않는다.
            other = await _verify_token(
                session, raw, (INTEGRATION_TOKEN_PREFIX, SERVICE_TOKEN_PREFIX)
            )
            if other is not None and other.integration == INTEGRATION_AICM:
                raise IntegrationError(
                    "integration token is not valid on this surface",
                    code="integration_surface_forbidden", status_code=403,
                )
            raise _unauthorized()
        principal = getattr(request.state, "integration_principal", None)
        if principal is not None and raw.startswith(SERVICE_TOKEN_PREFIX) == (token_prefix == SERVICE_TOKEN_PREFIX):
            rec = principal.token
        else:
            rec = await _verify_token(session, raw, (token_prefix,))
        if rec is None or rec.integration != INTEGRATION_AICM:
            raise _unauthorized()
        ip = client_ip(request)
        if not _ip_allowed(ip, rec.allowed_cidrs):
            logger.warning("aicm.source_ip_rejected", token_id=str(rec.token_id), ip=ip)
            raise IntegrationError(
                "source IP is not allowed for this integration token",
                code="source_ip_not_allowed", status_code=403,
            )
        if scope not in rec.scopes:
            raise IntegrationError(
                f"token lacks required scope: {scope}", code="insufficient_scope", status_code=403,
                details={"required_scope": scope},
            )
        cls = rl_class or (RL_READ if request.method in ("GET", "HEAD") else RL_WRITE)
        await enforce_rate_limit(request, rec, cls)

        tags = {"_integration": rec.integration}
        idem = request.headers.get("Idempotency-Key")
        if idem:
            tags["_idempotency_key"] = idem[:128]
        tags_token = audit_tags.set(tags)

        principal = _principal_for(rec, ip)
        request.state.integration_principal = principal
        try:
            yield principal
        finally:
            audit_tags.reset(tags_token)

    return _dep
