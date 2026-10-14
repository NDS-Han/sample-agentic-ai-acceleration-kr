# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R15 스케줄러 신뢰성 회귀 — advisory lock, 전월 재집계, cron 타임존, ROI upsert.

Kiro R15 리뷰가 확인한 결함들:
- 크론이 컨테이너 TZ(UTC) 기준이라 "새벽 잡"이 KST 점심에 돌았다.
- 롤링 배포/멀티 레플리카 공존 시 같은 잡이 동시에 두 번 돌 수 있었다.
- upsert UPDATE 분기가 생산성 지표 4개를 갱신하지 않아 첫 실행 값이 얼어붙었다.
- 매월 마지막 실행 이후의 말일 사용량/지연 도착분이 지난달 ROI 행에 반영될
  기회가 없었다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.usage import ROIAggregation, ROIScope
from app.repositories.analytics_repository import AnalyticsRepository
from app.scheduler import main as sched


# ── 전월 재집계 + 잡 락 ──────────────────────────────────────────────────────


def test_prev_period_month_boundaries():
    assert sched._prev_period("2026-10") == "2026-09"
    assert sched._prev_period("2026-01") == "2025-12"


def _agg_ctx():
    session = MagicMock()
    session.execute = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class _FixedDateTime(datetime):
    _fixed = datetime(2026, 10, 1, 0, 30)

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._fixed.replace(tzinfo=None).astimezone(timezone.utc).astimezone(tz)
        return cls._fixed


async def test_day1_aggregates_previous_month_too():
    """새 달 1일엔 전월도 재집계 — 말일 뒷부분·지연 도착 사용량이 지난달
    행에 반영될 마지막 기회다(upsert 멱등이라 재집계는 안전)."""
    calls: list[str] = []

    async def fake_aggregate(session, period):
        calls.append(period)

    _FixedDateTime._fixed = datetime(2026, 10, 1, 0, 30)
    with (
        patch.object(sched, "AsyncSessionLocal", return_value=_agg_ctx()),
        patch.object(sched, "aggregate_usage", side_effect=fake_aggregate),
        patch.object(sched, "_try_job_lock", AsyncMock(return_value=True)),
        patch.object(sched, "current_kst_period", return_value="2026-10"),
        patch.object(sched, "datetime", _FixedDateTime),
    ):
        await sched.run_aggregation()

    assert calls == ["2026-10", "2026-09"]


async def test_mid_month_aggregates_current_only():
    calls: list[str] = []

    async def fake_aggregate(session, period):
        calls.append(period)

    _FixedDateTime._fixed = datetime(2026, 10, 15, 12, 0)
    with (
        patch.object(sched, "AsyncSessionLocal", return_value=_agg_ctx()),
        patch.object(sched, "aggregate_usage", side_effect=fake_aggregate),
        patch.object(sched, "_try_job_lock", AsyncMock(return_value=True)),
        patch.object(sched, "current_kst_period", return_value="2026-10"),
        patch.object(sched, "datetime", _FixedDateTime),
    ):
        await sched.run_aggregation()

    assert calls == ["2026-10"]


async def test_job_skipped_when_lock_held_elsewhere():
    """다른 인스턴스(롤링 배포 중 구 pod 등)가 잡을 잡고 있으면 스킵한다."""
    aggregate = AsyncMock()
    _FixedDateTime._fixed = datetime(2026, 10, 15, 12, 0)
    with (
        patch.object(sched, "AsyncSessionLocal", return_value=_agg_ctx()),
        patch.object(sched, "aggregate_usage", aggregate),
        patch.object(sched, "_try_job_lock", AsyncMock(return_value=False)),
        patch.object(sched, "current_kst_period", return_value="2026-10"),
        patch.object(sched, "datetime", _FixedDateTime),
    ):
        await sched.run_aggregation()

    aggregate.assert_not_awaited()


# ── cron 타임존 + 잡 안전 옵션 ───────────────────────────────────────────────


def test_scheduler_uses_reporting_timezone():
    """AsyncIOScheduler 와 각 CronTrigger 가 REPORTING_TIMEZONE 기준이어야 한다.

    미지정 시 컨테이너 TZ(UTC)가 쓰여 야간 잡이 KST 피크에 실행됐다.
    """
    captured: dict = {}

    class FakeScheduler:
        def __init__(self, **kw):
            captured.update(kw)
            self.jobs: list = []

        def add_job(self, fn, trigger, **kw):
            self.jobs.append((fn, trigger, kw))

        def start(self):
            pass

        def shutdown(self):
            pass

    class FakeLoop:
        def add_signal_handler(self, *a, **kw):
            pass

        def stop(self):
            pass

        def run_forever(self):
            pass

        def close(self):
            pass

    fake = FakeScheduler()
    with (
        patch.object(sched, "AsyncIOScheduler", lambda **kw: fake.__init__(**kw) or fake),
        patch.object(sched.asyncio, "new_event_loop", lambda: FakeLoop()),
        patch.object(sched.asyncio, "set_event_loop", lambda loop: None),
    ):
        sched.main()

    from zoneinfo import ZoneInfo

    expected = ZoneInfo(sched.get_settings().REPORTING_TIMEZONE)
    # AsyncIOScheduler 는 tz 이름 문자열 또는 tzinfo 를 받는다 — 문자열 비교.
    assert str(captured["timezone"]) == str(expected)
    assert fake.jobs, "cron jobs were not registered"
    for _fn, trigger, kw in fake.jobs:
        assert trigger.timezone == expected
        # 중첩 실행/misfire 폭주 방지 옵션이 잡마다 적용됐는지
        assert kw["max_instances"] == 1
        assert kw["coalesce"] is True
        assert kw["misfire_grace_time"] > 0


# ── ROI upsert UPDATE 분기 생산성 지표 갱신 ─────────────────────────────────


def _agg(**over) -> ROIAggregation:
    base = dict(
        id=uuid.uuid4(),
        period="2026-10",
        scope=ROIScope.GLOBAL,
        scope_id=None,
        total_cost_usd=Decimal("5"),
        cost_per_user_usd=Decimal("1"),
        budget_utilization_pct=Decimal("0"),
        cost_by_model={},
        active_users=5,
        active_user_rate_pct=Decimal("0"),
        requests_per_user_per_day=Decimal("0"),
        activation_gap_pct=Decimal("0"),
        code_acceptance_rate_pct=Decimal("42.5"),
        cost_per_accepted_code_usd=Decimal("0.1234"),
        generated_lines_per_session=Decimal("77.0"),
        roi_index=Decimal("9.5"),
        aggregated_at=datetime.now(timezone.utc),
        aggregated_by="r15",
    )
    base.update(over)
    return ROIAggregation(**base)


def _session_returning(existing):
    session = AsyncMock()

    async def execute(stmt, params=None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = (
            existing if "pg_advisory_xact_lock" not in str(stmt) else True
        )
        return result

    session.execute = execute
    return session


async def test_upsert_update_branch_copies_productivity_metrics():
    """UPDATE 분기도 생산성 지표를 갱신한다 — 예전엔 INSERT 시에만 써서
    월 중 재집계가 반영되지 않았다."""
    existing = ROIAggregation(
        id=uuid.uuid4(),
        period="2026-10",
        scope=ROIScope.GLOBAL,
        scope_id=None,
        total_cost_usd=Decimal("1"),
        cost_per_user_usd=Decimal("0"),
        budget_utilization_pct=Decimal("0"),
        cost_by_model={},
        active_users=1,
        active_user_rate_pct=Decimal("0"),
        requests_per_user_per_day=Decimal("0"),
        activation_gap_pct=Decimal("0"),
        code_acceptance_rate_pct=None,
        cost_per_accepted_code_usd=None,
        generated_lines_per_session=None,
        roi_index=None,
        aggregated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        aggregated_by="old",
    )
    repo = AnalyticsRepository(_session_returning(existing))
    new = _agg()

    result = await repo.upsert_aggregation(new)

    assert result is existing
    assert existing.code_acceptance_rate_pct == new.code_acceptance_rate_pct
    assert existing.cost_per_accepted_code_usd == new.cost_per_accepted_code_usd
    assert existing.generated_lines_per_session == new.generated_lines_per_session
    assert existing.roi_index == new.roi_index
