# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import ServiceToken
from app.repositories.service_token_repository import ServiceTokenRepository
from app.schemas.service_tokens import (
    ServiceTokenCreateResponse,
    ServiceTokenItem,
    ServiceTokenListResponse,
)

logger = structlog.get_logger()

SERVICE_TOKEN_PREFIX = "svc-"
SERVICE_TOKEN_RANDOM_BYTES = 32  # 32 bytes = 64 hex chars → total 67 chars with prefix
SERVICE_TOKEN_DEFAULT_EXPIRY_DAYS = 90
SERVICE_TOKEN_ROTATE_GRACE_HOURS = 24


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ServiceTokenService:
    async def issue(
        self,
        session: AsyncSession,
        *,
        name: str,
        created_by: uuid.UUID,
        expiry_days: int | None = None,
        rotated_from: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> ServiceTokenCreateResponse:
        raw = SERVICE_TOKEN_PREFIX + secrets.token_hex(SERVICE_TOKEN_RANDOM_BYTES)
        token_prefix = raw[:12]  # "svc-a3b9c1d2"
        now = datetime.now(timezone.utc)
        if expires_at is None:
            days = expiry_days if expiry_days is not None else SERVICE_TOKEN_DEFAULT_EXPIRY_DAYS
            expires_at = now + timedelta(days=days)

        tok = ServiceToken(
            id=uuid.uuid4(),
            name=name,
            token_hash=hash_token(raw),
            token_prefix=token_prefix,
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
            rotated_from=rotated_from,
        )
        repo = ServiceTokenRepository(session)
        await repo.create(tok)
        logger.info("service_token.issued", token_id=str(tok.id), name=name, expires_at=expires_at.isoformat())

        return ServiceTokenCreateResponse(
            id=str(tok.id),
            name=tok.name,
            token_prefix=token_prefix,
            created_at=now,
            expires_at=expires_at,
            token=raw,
        )

    async def verify(self, session: AsyncSession, token_hash: str) -> ServiceToken | None:
        repo = ServiceTokenRepository(session)
        tok = await repo.get_by_hash(token_hash)
        if tok is None:
            return None
        now = datetime.now(timezone.utc)
        if tok.expires_at <= now:
            return None
        if tok.revoked_at is not None and tok.revoked_at <= now:
            return None
        return tok

    async def list_tokens(self, session: AsyncSession) -> ServiceTokenListResponse:
        repo = ServiceTokenRepository(session)
        rows = await repo.list_all()
        return ServiceTokenListResponse(
            items=[
                ServiceTokenItem(
                    id=str(r.id),
                    name=r.name,
                    token_prefix=r.token_prefix,
                    created_at=r.created_at,
                    expires_at=r.expires_at,
                    revoked_at=r.revoked_at,
                )
                for r in rows
            ]
        )

    async def rotate(
        self,
        session: AsyncSession,
        *,
        token_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> ServiceTokenCreateResponse:
        repo = ServiceTokenRepository(session)
        old = await repo.get_by_id(token_id)
        if old is None:
            raise NotFoundError("ServiceToken", str(token_id))

        now = datetime.now(timezone.utc)
        if old.expires_at <= now:
            raise ValidationError("expired service token cannot be rotated")
        if old.revoked_at is not None and old.revoked_at <= now:
            raise ValidationError("revoked service token cannot be rotated")

        # 새 토큰 수명은 구 토큰의 잔여 수명을 넘지 못한다. 로테이션은 자격
        # 교체이지 수명 연장이 아니다 — 이 상한이 없으면 유출된 토큰이 self-rotate
        # 를 반복해 expires_at 을 무기한 밀어낼 수 있다. days 가 아니라
        # expires_at 을 직접 넘겨 µs 단위 오버슛도 허용하지 않는다.
        new_expires_at = min(
            now + timedelta(days=SERVICE_TOKEN_DEFAULT_EXPIRY_DAYS), old.expires_at
        )

        # Preserve the original human bootstrapper as the issuer of the new token.
        # On the rotate path the caller is always a service token, so `created_by`
        # is the synthetic service-token id which does NOT exist in auth.users and
        # would violate the service_tokens.created_by FK. old.created_by was a real
        # human admin from the original issue, so it is always FK-safe.
        resp = await self.issue(
            session, name=old.name, created_by=old.created_by, rotated_from=old.id,
            expires_at=new_expires_at,
        )
        # grace 재로테이션 상한: revoked_at 이 이미 미래 시각(기존 grace)이면
        # 그 시각을 넘기지 않는다 — 아니면 유출 토큰이 <24h 마다 rotate 를
        # 반복해 폐기 시점을 무기한 연장할 수 있다.
        grace_until = now + timedelta(hours=SERVICE_TOKEN_ROTATE_GRACE_HOURS)
        if old.revoked_at is not None:
            grace_until = min(grace_until, old.revoked_at)
        await repo.set_revoked_at(old.id, grace_until)
        logger.info(
            "service_token.rotated",
            old_id=str(old.id),
            new_id=resp.id,
            grace_until=grace_until.isoformat(),
            triggered_by=str(created_by),
        )
        return resp

    async def revoke(self, session: AsyncSession, *, token_id: uuid.UUID) -> None:
        repo = ServiceTokenRepository(session)
        tok = await repo.get_by_id(token_id)
        if tok is None:
            raise NotFoundError("ServiceToken", str(token_id))
        await repo.set_revoked_at(token_id, datetime.now(timezone.utc))
        logger.info("service_token.revoked", token_id=str(token_id))
