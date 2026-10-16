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
                    rotated_from=str(r.rotated_from) if r.rotated_from else None,
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
        # FOR UPDATE — 두 개의 동시 rotate 가 같은 구 토큰에서 신 토큰을 각각
        # 발급하는 것을 직렬화한다. 락 없이 두 rotate 가 통과하면 grace 창의
        # 구 토큰 사본이 둘 다 유효한 교체 토큰을 생산한다.
        old = await repo.get_by_id(token_id, for_update=True)
        if old is None:
            raise NotFoundError("ServiceToken", str(token_id))

        now = datetime.now(timezone.utc)
        if old.expires_at <= now:
            raise ValidationError("expired service token cannot be rotated")
        # revoked_at 이 세팅됐다는 것 자체가 "이미 rotate(또는 revoke) 됐다" 는
        # 뜻이다. 미래 시각(grace)도 거부 — grace 중 재rotate 를 허용하면
        # 유출 토큰이 신 토큰을 무한히 연쇄 발급할 수 있다.
        if old.revoked_at is not None:
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
        # grace — 구 토큰은 24h 간 진행 중 호출이 끊기지 않게 유효를 유지.
        # revoked_at != None 인 토큰은 위에서 이미 거부했으므로 이 값은 최초
        # 1회만 쓰인다(재rotate 불가 → grace 연장 루프 없음).
        grace_until = now + timedelta(hours=SERVICE_TOKEN_ROTATE_GRACE_HOURS)
        await repo.set_revoked_at(old.id, grace_until)
        logger.info(
            "service_token.rotated",
            old_id=str(old.id),
            new_id=resp.id,
            grace_until=grace_until.isoformat(),
            triggered_by=str(created_by),
        )
        return resp

    async def revoke(
        self, session: AsyncSession, *, token_id: uuid.UUID
    ) -> list[str]:
        """폐기 + rotate 계보 연쇄 폐기. 실제로 폐기된 토큰 id 목록을 돌려준다
        (감사 행이 연쇄 범위를 기록하게 하기 위해 — R21 r2).

        유출 토큰은 self-rotate(``/rotate``)로 후속 토큰을 만들 수 있다 —
        관리자가 알고 있는 id 하나만 폐기하면 공격자의 새 토큰이 살아남으므로
        ``rotated_from`` 사슬을 **양방향**으로 따라가며 전부 즉시 무효화한다.
        하향(자손)뿐 아니라 상향(조상)도 걷는다 — 유출 토큰 T 를 공격자가
        self-rotate 해 C 를 만든 뒤 관리자가 C 만 폐기하면, grace 창
        (revoked_at > now)에 있는 T 가 최대 24h 더 인증된다(R21 r3 S4).
        """
        repo = ServiceTokenRepository(session)
        tok = await repo.get_by_id(token_id)
        if tok is None:
            raise NotFoundError("ServiceToken", str(token_id))
        now = datetime.now(timezone.utc)
        queue = [tok]
        revoked: list[str] = []
        seen: set[uuid.UUID] = set()
        while queue:
            cur = queue.pop()
            if cur.id in seen:
                continue
            seen.add(cur.id)
            # 자손 탐색은 폐기 여부와 무관하게 해야 한다 — grace 만료 등으로
            # revoked_at<=now 인 조상을 건너뛰면 그 아래 살아있는 후속 토큰이
            # 연쇄에서 빠진다(R21 r2 M-C). 쓰기만 이미 폐기된 행에 대해 생략.
            if not (cur.revoked_at is not None and cur.revoked_at <= now):
                await repo.set_revoked_at(cur.id, now)
                revoked.append(str(cur.id))
            queue.extend(await repo.list_rotated_from(cur.id))
            # 상향 — rotate 는 항상 구 토큰에 grace 를 쓰므로, 자식이 있는 조상은
            # revoked_at 이 설정돼 있다. grace 창에 있는 조상을 now 로 덮어
            # 유출 사슬 전체를 닫는다.
            if cur.rotated_from is not None and cur.rotated_from not in seen:
                parent = await repo.get_by_id(cur.rotated_from)
                if parent is not None:
                    queue.append(parent)
        logger.info(
            "service_token.revoked", token_id=str(token_id), cascaded=revoked
        )
        return revoked

    async def end_grace(
        self, session: AsyncSession, *, token_id: uuid.UUID
    ) -> tuple[bool, datetime | None]:
        """rotate grace 창(revoked_at > now)만 닫는다 — 연쇄 폐기 없음.

        DELETE /{id} 는 계보 전체를 폐기하므로 "옛 토큰의 grace 만 끝내고
        배포된 후속 토큰은 살리기" 용도에는 이 엔드포인트를 쓴다(R21 r4 N1).
        이미 만료된 폐기(revoked_at <= now)나 미폐기(None) 토큰은 no-op.

        반환: (grace 창을 실제로 닫았는지, 이전 revoked_at) — 감사가
        단축된 창을 재구성할 수 있게 before 값을 함께 돌려준다(R21 r5 N6).
        """
        repo = ServiceTokenRepository(session)
        # FOR UPDATE — 동시 rotate/revoke 와 revoked_at 쓰기가 엇갈리지 않게.
        tok = await repo.get_by_id(token_id, for_update=True)
        if tok is None:
            raise NotFoundError("ServiceToken", str(token_id))
        prev_revoked_at = tok.revoked_at
        now = datetime.now(timezone.utc)
        if prev_revoked_at is None or prev_revoked_at <= now:
            return False, prev_revoked_at  # grace 창이 없다 — 할 일 없음
        await repo.set_revoked_at(token_id, now)
        logger.info("service_token.grace_ended", token_id=str(token_id))
        return True, prev_revoked_at
