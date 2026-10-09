# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""upsert_aggregation 의 advisory-lock 직렬화 회귀 (R4-B2-1).

배경: SELECT→INSERT 는 스케줄러와 `/internal/scheduler/run` 이 겹칠 때 양쪽이
miss 후 INSERT 해 한쪽이 UniqueViolation 으로 전체 배치를 롤백시켰다. 그리고
`scope_id` 가 NULL(GLOBAL) 이면 유니크 인덱스조차 발동하지 않아 중복 행이
조용히 생긴다. 그래서 키별 `pg_advisory_xact_lock` 으로 직렬화한다 —
이 테스트는 락이 **SELECT 이전에** 같은 세션에서 발행되는지를 고정한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.usage import ROIAggregation, ROIScope
from app.repositories.analytics_repository import AnalyticsRepository


def _agg(scope_id: uuid.UUID | None = None) -> ROIAggregation:
    return ROIAggregation(
        id=uuid.uuid4(),
        period="2026-10",
        scope=ROIScope.GLOBAL if scope_id is None else ROIScope.TEAM,
        scope_id=scope_id,
        total_cost_usd=Decimal("1"),
        cost_per_user_usd=Decimal("0"),
        budget_utilization_pct=Decimal("0"),
        cost_by_model={},
        active_users=0,
        active_user_rate_pct=Decimal("0"),
        requests_per_user_per_day=Decimal("0"),
        activation_gap_pct=Decimal("0"),
        aggregated_at=datetime.now(timezone.utc),
        aggregated_by="test",
    )


def _session_with_existing(existing):
    """execute 호출 순서를 기록하는 세션 double.

    - 락 문장(pg_advisory_xact_lock) → scalar_result 없음
    - SELECT ROIAggregation → `existing` 반환
    """
    session = AsyncMock()
    calls: list[str] = []

    async def execute(stmt, params=None):
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            calls.append("lock")
            result = MagicMock()
            return result
        calls.append("select")
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        return result

    session.execute = AsyncMock(side_effect=execute)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session._calls = calls
    return session


@pytest.mark.asyncio
async def test_lock_runs_before_select():
    session = _session_with_existing(None)
    repo = AnalyticsRepository(session)

    await repo.upsert_aggregation(_agg())

    assert session._calls[:2] == ["lock", "select"]


@pytest.mark.asyncio
async def test_existing_row_is_updated_not_duplicated():
    existing = MagicMock()
    session = _session_with_existing(existing)
    repo = AnalyticsRepository(session)

    agg = _agg()
    agg.total_cost_usd = Decimal("42")
    out = await repo.upsert_aggregation(agg)

    assert out is existing
    assert existing.total_cost_usd == Decimal("42")
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_lock_key_covers_null_scope_id():
    """GLOBAL(scope_id=None)은 유니크 인덱스가 못 잡는다 — 락 키가 NULL 도
    구분하는지(같은 키면 같은 락) 검증."""
    captured: list[str] = []
    session = _session_with_existing(None)

    async def execute(stmt, params=None):
        if params and "k" in params:
            captured.append(params["k"])
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session.execute = AsyncMock(side_effect=execute)
    repo = AnalyticsRepository(session)

    await repo.upsert_aggregation(_agg())
    await repo.upsert_aggregation(_agg())

    # 같은 (period, scope, scope_id=None) → 동일 락 키 두 번.
    assert len(captured) == 2 and captured[0] == captured[1]
    assert "GLOBAL" in captured[0]
