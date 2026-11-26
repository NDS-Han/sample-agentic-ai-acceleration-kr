# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AICM-managed field ownership recording + optional human-write lock."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.exceptions import ManagedFieldLockedError
from app.models.integration import ExternalManagement
from app.services.integration_token_service import INTEGRATION_AICM

RESOURCE_USER = "user"
RESOURCE_TEAM = "team"
FIELD_BUDGET = "budget"
FIELD_DEFAULT_CAP = "default-cap"
FIELD_ALLOWED_CLIENTS = "allowed-clients"
FIELD_ALLOWED_MODELS = "allowed-models"
STATE_MANAGED = "managed"
STATE_RELEASED = "released"
VALUE_SET = "set"
VALUE_CLEARED = "cleared"


def app_budget_facet(client: str) -> str:
    return f"app-budget:{client}"


def app_group_budget_facet(group: str) -> str:
    return f"app-group-budget:{group}"


def is_aicm_actor(actor: CurrentUser | None) -> bool:
    return bool(actor is not None and getattr(actor, "integration", None) == INTEGRATION_AICM)


def _dedupe(specs: Iterable[tuple[str, uuid.UUID, str]]) -> list[tuple[str, uuid.UUID, str]]:
    return sorted(set(specs), key=lambda x: (x[0], str(x[1]), x[2]))


async def record_aicm_write(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: uuid.UUID,
    facet: str,
    actor: CurrentUser,
    request_id: str = "",
    cleared: bool = False,
) -> None:
    """AICM mirror principal 의 성공 쓰기를 같은 트랜잭션에 upsert 한다.

    플래그와 무관하게 항상 기록한다. DELETE 는 ``cleared=True`` 로 남기되
    ``state`` 는 managed 를 유지한다 — 해제된 값도 AICM 의 의도된 상태이므로
    release 전에는 사람이 되돌릴 수 없다(선택한 의미).
    """
    if not is_aicm_actor(actor):
        return
    now = datetime.now(timezone.utc)
    values = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "facet": facet,
        "managed_by": INTEGRATION_AICM,
        "state": STATE_MANAGED,
        "value_state": VALUE_CLEARED if cleared else VALUE_SET,
        "last_written_at": now,
        "token_id": actor.service_token_id,
        "lineage_id": getattr(actor, "integration_lineage_id", None),
        "request_id": (request_id or "")[:128],
        "released_at": None,
        "released_by": None,
        "release_reason": None,
        "updated_at": now,
    }
    stmt = pg_insert(ExternalManagement).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_type", "resource_id", "facet"],
        set_={k: v for k, v in values.items() if k not in {"resource_type", "resource_id", "facet"}},
    )
    await session.execute(stmt)


def _release_hint(resource_type: str, resource_id: uuid.UUID, facet: str) -> str:
    return (
        f"POST /admin/managed-fields/{resource_type}/{resource_id}/{facet}/release "
        "with a non-empty JSON body {\"reason\": \"...\"}; the field is editable until AICM writes it again"
    )


async def assert_writable(
    session: AsyncSession,
    specs: Iterable[tuple[str, uuid.UUID, str]],
    *,
    actor: CurrentUser,
) -> None:
    """플래그 ON 에서 사람 쓰기가 managed 필드를 건드리면 409.

    OFF 이면 쿼리 없이 즉시 반환한다. AICM actor 는 어떤 상태에서도 막지 않는다.
    여러 필드를 한 요청이 바꾸는 경우에도 tuple IN 한 번의 인덱스 조회만 한다.
    """
    if not get_settings().AICM_MANAGED_LOCK_ENABLED or is_aicm_actor(actor):
        return
    unique = _dedupe(specs)
    if not unique:
        return
    stmt = select(ExternalManagement).where(
        tuple_(ExternalManagement.resource_type, ExternalManagement.resource_id, ExternalManagement.facet).in_(unique),
        ExternalManagement.state == STATE_MANAGED,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return
    first = rows[0]
    conflicts = [
        {
            "resource_type": r.resource_type,
            "resource_id": str(r.resource_id),
            "field": r.facet,
            "managed_by": r.managed_by,
            "since": r.last_written_at.isoformat(),
        }
        for r in rows
    ]
    raise ManagedFieldLockedError(
        "This field is managed by AICM and cannot be edited by a human admin until the lock is released.",
        details={
            **conflicts[0],
            "release": _release_hint(first.resource_type, first.resource_id, first.facet),
            "conflicts": conflicts,
        },
    )


def managed_info(row: ExternalManagement):
    from app.schemas.common import ManagedByInfo

    return ManagedByInfo(source=row.managed_by, since=row.last_written_at, field=row.facet)


async def managed_rows(
    session: AsyncSession,
    *,
    resource_type: str | None = None,
    resource_ids: list[uuid.UUID] | None = None,
    facets: list[str] | None = None,
) -> list[ExternalManagement]:
    """플래그 ON 일 때 활성 managed 행을 읽는다. OFF 면 쿼리 없이 빈 목록."""
    if not get_settings().AICM_MANAGED_LOCK_ENABLED:
        return []
    stmt = select(ExternalManagement).where(ExternalManagement.state == STATE_MANAGED)
    if resource_type is not None:
        stmt = stmt.where(ExternalManagement.resource_type == resource_type)
    if resource_ids is not None:
        if not resource_ids:
            return []
        stmt = stmt.where(ExternalManagement.resource_id.in_(resource_ids))
    if facets is not None:
        if not facets:
            return []
        stmt = stmt.where(ExternalManagement.facet.in_(facets))
    return list((await session.execute(stmt)).scalars().all())


async def managed_map(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_ids: list[uuid.UUID],
    facets: list[str] | None = None,
) -> dict[tuple[uuid.UUID, str], ExternalManagement]:
    rows = await managed_rows(
        session, resource_type=resource_type, resource_ids=resource_ids, facets=facets
    )
    return {(r.resource_id, r.facet): r for r in rows}
