# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""외부 통합 토큰(``itg-``) — 발급·검증·rotate·계보 목록.

토큰 행(해시·수명·rotate 계보·폐기)은 ``auth.service_tokens`` 를 그대로 쓰고, 권한(통합 이름·
scope·CIDR·분당 상한)은 ``auth.integration_tokens`` 부속 행에 둔다. 발급/rotate 의 수명 규칙,
FOR UPDATE 분기 차단, 계보 rotate 빈도 상한(429)은 ``ServiceTokenService`` 의 구현을 그대로 탄다.
"""
from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.models.auth import ServiceToken
from app.models.integration import IntegrationToken
from app.services.service_token_service import (
    INTEGRATION_TOKEN_PREFIX,
    SERVICE_TOKEN_PREFIX,
    SERVICE_TOKEN_RANDOM_BYTES,
    ServiceTokenService,
    hash_token,
)

#: 현재 정의된 통합. 코드 내부 개념은 일반화(통합 토큰)하고 경로·이름만 aicm 이다.
INTEGRATION_AICM = "aicm"

#: phase 0a scope — 발급 시 이 목록 밖의 값은 거부한다(오타가 조용히 무권한 토큰이 되지 않게).
#: reactivation·spent seeding·app budget·managed lock 은 AICM 미결 질문에 달려 있어 발급하지 않는다.
SCOPES: frozenset[str] = frozenset(
    {
        "tokens:read",
        "tokens:rotate",
        # /aicm/v1/admin mirror scopes — legacy 제거로 users:provision/users:lifecycle/
        # policy:*/budgets:limits:write 는 대상 엔드포인트가 사라져 카탈로그에서 빠졌다.
        # 하나의 토큰이 필요한 표면만 갖도록 발급 시 선택한다 — 읽기/쓰기를 나눈 이유는
        # 유출 시 폭발반경 때문.
        "users:read",
        "users:write",
        "users:sync",
        "budgets:read",
        "budgets:write",
        "models:read",

    }
)


@dataclass(frozen=True)
class IntegrationTokenRecord:
    token_id: uuid.UUID
    name: str
    integration: str
    scopes: frozenset[str]
    allowed_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(default=())
    rate_limit_per_min: int | None = None
    lineage_id: uuid.UUID | None = None


def _parse_cidrs(values) -> tuple:
    out = []
    for v in values or ():
        out.append(v if isinstance(v, (ipaddress.IPv4Network, ipaddress.IPv6Network))
                   else ipaddress.ip_network(str(v), strict=False))
    return tuple(out)


def well_formed(raw: str, prefixes: tuple[str, ...] | None = None) -> bool:
    """DB 왕복 전 형식 검사 — ``itg-``/``svc-`` + 64 hex.

    phase 0a 는 ``itg-`` 를 발급했고, admin mirror(option A)는 ``svc-`` 접두사를 유지한
    scoped integration 토큰을 쓴다. verify 의 호출자가 허용 접두사를 좁힌다.
    """
    prefixes = prefixes or (INTEGRATION_TOKEN_PREFIX, SERVICE_TOKEN_PREFIX)
    prefix = next((p for p in prefixes if raw.startswith(p)), None)
    if prefix is None:
        return False
    body = raw[len(prefix):]
    return len(body) == SERVICE_TOKEN_RANDOM_BYTES * 2 and all(c in "0123456789abcdef" for c in body)


class IntegrationTokenService:
    def __init__(self, base: ServiceTokenService | None = None) -> None:
        self._base = base or ServiceTokenService()

    @staticmethod
    def validate_scopes(scopes: list[str]) -> list[str]:
        unknown = sorted(set(scopes) - SCOPES)
        if unknown:
            raise ValidationError(f"unknown scopes: {unknown}; allowed={sorted(SCOPES)}")
        if not scopes:
            raise ValidationError("at least one scope is required")
        return sorted(set(scopes))

    @staticmethod
    def validate_cidrs(cidrs: list[str] | None) -> list[str] | None:
        if not cidrs:
            return None
        out = []
        for c in cidrs:
            try:
                out.append(str(ipaddress.ip_network(c, strict=True)))
            except ValueError as e:
                raise ValidationError(f"invalid CIDR {c!r}: {e}") from e
        return out

    async def issue(
        self,
        session: AsyncSession,
        *,
        name: str,
        created_by: uuid.UUID,
        scopes: list[str],
        allowed_cidrs: list[str] | None,
        rate_limit_per_min: int | None,
        expiry_days: int | None,
        integration: str = INTEGRATION_AICM,
        prefix: str = SERVICE_TOKEN_PREFIX,
    ):
        scopes = self.validate_scopes(scopes)
        cidrs = self.validate_cidrs(allowed_cidrs)
        if rate_limit_per_min is not None and rate_limit_per_min < 1:
            raise ValidationError("rate_limit_per_min must be >= 1")
        resp = await self._base.issue(
            session, name=name, created_by=created_by, expiry_days=expiry_days,
            # option A mirror 는 ``Bearer svc-...`` 를 그대로 쓴다. integration grant 가
            # 있으므로 이 svc- 는 real /admin 표면에서는 core/auth 가 거부한다.
            # legacy lifecycle 유지용으로만 호출자가 명시적으로 itg- 를 고를 수 있다.
            prefix=prefix,
        )
        tid = uuid.UUID(resp.id)
        session.add(
            IntegrationToken(
                token_id=tid, integration=integration, scopes=scopes, allowed_cidrs=cidrs,
                rate_limit_per_min=rate_limit_per_min, lineage_id=tid,
            )
        )
        await session.flush()
        return resp, scopes, cidrs

    async def verify(
        self,
        session: AsyncSession,
        raw: str,
        *,
        prefixes: tuple[str, ...] | None = None,
    ) -> IntegrationTokenRecord | None:
        if not well_formed(raw, prefixes=prefixes):
            return None
        tok = await self._base.verify(session, hash_token(raw))
        if tok is None:
            return None
        grant = await session.get(IntegrationToken, tok.id)
        if grant is None:
            return None
        return IntegrationTokenRecord(
            token_id=tok.id,
            name=tok.name,
            integration=grant.integration,
            scopes=frozenset(grant.scopes or ()),
            allowed_cidrs=_parse_cidrs(grant.allowed_cidrs),
            rate_limit_per_min=grant.rate_limit_per_min,
            lineage_id=grant.lineage_id,
        )

    async def rotate(self, session: AsyncSession, *, token_id: uuid.UUID, actor_user_id: uuid.UUID):
        """self-rotate — ``ServiceTokenService.rotate`` (수명 상한·grace·분기 차단·빈도 상한) 후
        권한 부속 행을 복사한다. scope/CIDR 은 넓힐 수 없다(새 토큰은 사람 admin 이 발급)."""
        grant = await session.get(IntegrationToken, token_id)
        if grant is None:
            raise ValidationError("not an integration token")
        # 권한 부속 행 복사는 ServiceTokenService.rotate 가 한 곳에서 한다.
        return await self._base.rotate(session, token_id=token_id, created_by=actor_user_id)

    async def list_lineage(self, session: AsyncSession, lineage_id: uuid.UUID) -> list[dict]:
        rows = (
            await session.execute(
                select(ServiceToken, IntegrationToken)
                .join(IntegrationToken, IntegrationToken.token_id == ServiceToken.id)
                .where(IntegrationToken.lineage_id == lineage_id)
                .order_by(ServiceToken.created_at.desc())
            )
        ).all()
        out = []
        for st, g in rows:
            out.append({
                "id": str(st.id),
                "name": st.name,
                "token_prefix": st.token_prefix,
                "created_at": st.created_at,
                "expires_at": st.expires_at,
                "revoked_at": st.revoked_at,
                "rotated_from": str(st.rotated_from) if st.rotated_from else None,
                "integration": g.integration,
                "scopes": sorted(g.scopes or ()),
                "allowed_cidrs": [str(c) for c in g.allowed_cidrs] if g.allowed_cidrs else None,
                "rate_limit_per_min": g.rate_limit_per_min,
            })
        return out

