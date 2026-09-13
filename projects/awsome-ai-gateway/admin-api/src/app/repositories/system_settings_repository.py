# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models.system import SystemSetting


class SystemSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> SystemSetting | None:
        stmt = select(SystemSetting).where(SystemSetting.key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self, key: str, value: Any, updated_by: uuid.UUID | None = None
    ) -> None:
        """Insert or update a setting by key (ON CONFLICT DO UPDATE)."""
        stmt = pg_insert(SystemSetting).values(
            key=key, value=value, updated_by=updated_by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={
                "value": stmt.excluded.value,
                "updated_by": stmt.excluded.updated_by,
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
