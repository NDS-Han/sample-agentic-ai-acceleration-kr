# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""지난 시간·날짜에 늦게 도착한 usage_logs → 롤업 dirty 표시(같은 트랜잭션, 잠금 대기 없음).

실 PG 경합(재집계 중에도 표시가 기다리지 않고, 재집계 커밋 뒤에도 남는다) 검증은
tests/regression/test_rollup_v2_realpg.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import worker.batch_flusher as bf
import worker.daily_aggregator as da
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


def test_hour_buckets_follow_local_midnight_kolkata_and_dst():
    """버킷은 로컬 자정 + k 시간 — 30분 오프셋(Kolkata)·DST(23h/25h) 모두 날짜 경계와 맞는다."""
    ist = ZoneInfo("Asia/Kolkata")
    b = da.hour_buckets(ist, date(2026, 11, 24))
    assert len(b) == 24
    assert b[0][0] == datetime(2026, 11, 23, 18, 30, tzinfo=UTC)
    assert b[-1][1] == datetime(2026, 11, 24, 18, 30, tzinfo=UTC)
    ny = ZoneInfo("America/New_York")
    assert len(da.hour_buckets(ny, date(2026, 3, 8))) == 23
    assert len(da.hour_buckets(ny, date(2026, 11, 1))) == 25
    d, hs, he = da.bucket_of(ist, datetime(2026, 11, 24, 5, 10, tzinfo=UTC))  # 10:40 IST
    assert (d, hs, he) == (date(2026, 11, 24), datetime(2026, 11, 24, 4, 30, tzinfo=UTC),
                           datetime(2026, 11, 24, 5, 30, tzinfo=UTC))


def test_late_marks_hours_and_days_with_margin():
    now = datetime(2026, 11, 24, 5, 40, tzinfo=UTC)         # 11:10 IST
    today0 = datetime(2026, 11, 23, 18, 30, tzinfo=UTC)     # 오늘 00:00 IST
    entries = [
        _entry("cur", now - timedelta(minutes=5)),                  # 현재 시간 → 표시 없음
        _entry("prev", now - timedelta(minutes=50)),                # 10:20 IST → 시간 표시
        _entry("yday", today0 - timedelta(minutes=1)),              # 어제 23:59 → 시간+날짜
        _entry("old", today0 - timedelta(days=40)),                 # 40일 전
        _entry("dup", today0 - timedelta(minutes=2)),               # 어제 같은 시간(중복)
    ]
    marks = bf._late_marks(entries, "Asia/Kolkata", now=now, margin_sec=120)
    yday = date(2026, 11, 23)
    assert (yday, None) in marks and (date(2026, 10, 15), None) in marks  # 40일 전 00:00 IST
    assert (yday, datetime(2026, 11, 23, 17, 30, tzinfo=UTC)) in marks
    assert (date(2026, 11, 24), datetime(2026, 11, 24, 4, 30, tzinfo=UTC)) in marks
    assert not any(m[1] == datetime(2026, 11, 24, 5, 30, tzinfo=UTC) for m in marks)
    assert len(marks) == 5  # 중복 제거
    # 여유(margin): 현재 시간 끝 1분 전의 행은 시간 표시를 받는다(늦은 커밋 대비).
    near = datetime(2026, 11, 24, 6, 29, tzinfo=UTC)
    assert bf._late_marks([_entry("n", near)], "Asia/Kolkata", now=near, margin_sec=120) == [
        (date(2026, 11, 24), datetime(2026, 11, 24, 5, 30, tzinfo=UTC))]


def test_late_marks_empty_for_current_hour():
    now = datetime(2026, 11, 24, 5, 40, tzinfo=UTC)
    assert bf._late_marks([_entry("a", now)], "UTC", now=now) == []


@pytest.mark.asyncio
async def test_flush_marks_dirty_in_same_transaction(monkeypatch):
    """dirty 표시는 usage_logs INSERT 와 같은 세션·같은 commit 안 — 레지스트리는 건드리지 않는다."""
    s = MagicMock()
    s.reporting_timezone = "UTC"
    s.rollup_dirty_margin_sec = 120
    monkeypatch.setattr(bf, "get_settings", lambda: s)
    order: list[str] = []

    async def _execute(stmt, params=None):
        sql = str(stmt)
        assert "daily_aggregate_days" not in sql  # 재집계 행 잠금을 기다리는 경로 제거
        order.append("dirty" if "rollup_dirty_marks" in sql else sql.split()[0])
        r = MagicMock()
        if "RETURNING" in sql:
            r.scalar_one_or_none.return_value = params["request_id"]
        r.__iter__ = lambda self=None: iter([])
        if "rollup_dirty_marks" in sql:
            assert params == {"days": [date(2026, 4, 21), date(2026, 4, 21)],
                              "hours": [None, datetime(2026, 4, 21, 10, tzinfo=UTC)]}
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
