# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 사용자 대상 보호 — ADMIN/시스템/서비스 계정 대상은 403 + 감사.

통합 API 는 게이트웨이의 일반 사용자만 다룬다. 운영 권한(ADMIN), 내부 합성 계정,
서비스 계정을 통합 토큰이 조회·변경하지 못하게 모든 사용자 대상 경로에서 같은
판정과 같은 거부 감사를 쓴다.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select

from app.core.audit import audit_logger
from app.models.auth import User, UserRole

#: 03_seed_data.sql 의 시스템/합성 사용자 — bootstrap admin 과 svc/integration 행위자.
SYSTEM_USER_IDS: tuple[uuid.UUID, ...] = (
    uuid.UUID("00000000-0000-4000-a000-000000000010"),
    uuid.UUID("00000000-0000-4000-a000-000000000011"),
)


def protected_reason(user: User) -> str | None:
    if user.id in SYSTEM_USER_IDS:
        return "target_is_system_user"
    if user.is_service_account:
        return "target_is_service_account"
    if user.role == UserRole.ADMIN:
        return "target_is_admin"
    return None


def protected_error_payload(reason: str, *, operation: str) -> dict:
    """IntegrationError(403) 핸들러와 같은 봉투 — 멱등 저장소가 재생할 수 있는 정상 응답용."""
    return {
        "error": {
            "type": "forbidden",
            "code": reason,
            "message": "AICM integration cannot target ADMIN, system, or service-account users",
            "details": {"reason": reason, "operation": operation},
        }
    }


async def user_by_id(session, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def users_by_email_ci(session, email: str, *, lock: bool = False) -> list[User]:
    stmt = select(User).where(func.lower(User.email) == email.lower())
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars().all())


async def users_matching_identity(
    session,
    *,
    sub: str | None = None,
    emails: list[str] | tuple[str, ...] = (),
    lock: bool = False,
) -> list[User]:
    clauses = []
    if sub:
        clauses.append(User.sso_subject == sub)
    lowered = [e.lower() for e in emails if e]
    if lowered:
        clauses.append(func.lower(User.email).in_(lowered))
    if not clauses:
        return []
    stmt = select(User).where(or_(*clauses))
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars().all())


async def audit_protected_refusal(
    session,
    *,
    actor,
    user: User,
    operation: str,
    resource_type: str,
    ip_address: str,
    request_id: str,
) -> str:
    """보호 대상 거부를 실패 감사로 남기고 그 reason 코드를 돌려준다."""
    reason = protected_reason(user) or "protected_target"
    await audit_logger.log(
        session,
        actor_user_id=actor.user_id,
        actor_role=actor.role.value,
        service_token_id=getattr(actor, "service_token_id", None),
        action="AICM_TARGET_REFUSED",
        resource_type=resource_type,
        resource_id=str(user.id),
        result="FAILURE",
        changes={
            "operation": operation,
            "reason": reason,
            "target": {
                "user_id": str(user.id),
                "email": user.email,
                "role": user.role.value,
                "is_service_account": user.is_service_account,
            },
        },
        ip_address=ip_address,
        request_id=request_id,
    )
    return reason
