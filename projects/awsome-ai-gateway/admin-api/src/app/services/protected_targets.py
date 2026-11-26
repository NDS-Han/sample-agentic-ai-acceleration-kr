# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""보호 대상 사용자 분류 — integration guard 와 admin budget guard 의 공용 규칙."""
from __future__ import annotations

import uuid

from app.models.auth import UserRole

#: 시스템 합성 사용자(03_seed_data.sql) — 사람/admin 표면에서도 spent seed 대상이 될 수 없다.
SYSTEM_USER_IDS = frozenset({
    uuid.UUID("00000000-0000-4000-a000-000000000010"),
    uuid.UUID("00000000-0000-4000-a000-000000000011"),
})


def protected_user_target_code(user_id: uuid.UUID, user) -> str | None:
    """guard_user_target 의 분류 순서: system user → service account → ADMIN."""
    if user_id in SYSTEM_USER_IDS:
        return "target_is_system_user"
    if user is None:
        return None
    if getattr(user, "is_service_account", False):
        return "target_is_service_account"
    if user.role == UserRole.ADMIN:
        return "target_is_admin"
    return None
