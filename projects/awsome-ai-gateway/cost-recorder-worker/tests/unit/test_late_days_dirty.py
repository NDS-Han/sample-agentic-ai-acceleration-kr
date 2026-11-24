# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""과거 날짜에 늦게 도착한 usage_logs → 그 날짜 롤업 dirty 표시(같은 트랜잭션).

실 PG 경합(재집계 잠금 대기) 검증은 tests/regression/test_rollup_v2_realpg.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import worker.batch_flusher as bf
from worker.schemas.cost_stream import CostStreamEntry


def _entry(rid: str, requested_at: datetime) -> CostStreamEntry:
    iso = requested_at.isoformat()
    return CostStreamEntry(
        request_id=rid, user_id="00000000-0000-0000-0000-000000000001",
        team_id="00000000-0000-0000-0000-000000000002",
        dept_id="00000000-0000-0000-0000-000000000003", model_alias="m", provider="BEDROCK",
        input_tokens=1, output_tokens=1, cache_creation_tokens=0, cache_read_tokens=0,
        cost_usd=Decimal("0.01"), latency_ms=1, is_streaming=False, estimated_usage=False,
        requested_at=iso, completed_at=iso, period=iso[:7], date=iso[:10],
    )


def test_late_days_uses_reporting_tz_and_excludes_today():
    tz = ZoneInfo("Asia/Kolkata")
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    entries = [
        _entry("a", today_start + timedelta(minutes=1)),               # 오늘 → 표시 안 함
        _entry("b", today_start - timedelta(minutes=1)),               # 어제 23:59 IST
        _entry("c", (today_start - timedelta(days=40)).astimezone(UTC)),
        _entry("d", today_start - timedelta(minutes=5)),               # 어제(중복)
    ]
    days = bf._late_days(entries, "Asia/Kolkata")
    yday = (today_start - timedelta(days=1)).date()
    assert days == sorted([yday, (today_start - timedelta(days=40)).date()])


def test_late_days_empty_for_same_day_batch():
    now = datetime.now(UTC)
    assert bf._late_days([_entry("a", now)], "UTC") == []


@pytest.mark.asyncio
async def test_flush_marks_dirty_in_same_transaction(monkeypatch):
    """dirty 표시는 usage_logs INSERT 와 같은 세션·같은 commit 안이어야 한다."""
    s = MagicMock()
    s.reporting_timezone = "UTC"
    monkeypatch.setattr(bf, "get_settings", lambda: s)
    order: list[str] = []

    async def _execute(stmt, params=None):
        sql = str(stmt)
        order.append("dirty" if "daily_aggregate_days" in sql else sql.split()[0])
        r = MagicMock()
        if "RETURNING" in sql:
            r.scalar_one_or_none.return_value = params["request_id"]
        r.__iter__ = lambda self=None: iter([])
        if "daily_aggregate_days" in sql:
            assert params == {"days": [date(2026, 4, 21)]}
        return r

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    redis = MagicMock()
    redis.pipeline.return_value.execute = AsyncMock()
    flusher = bf.BatchFlusher(session_factory=factory, redis=redis)
    await flusher.flush([_entry("late", datetime(2026, 4, 21, 10, tzinfo=UTC))])
    assert "dirty" in order
    assert order.index("dirty") < order.index("commit")
