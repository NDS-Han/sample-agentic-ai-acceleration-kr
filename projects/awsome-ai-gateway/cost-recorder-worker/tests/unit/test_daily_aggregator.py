# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregator 단위 테스트 (실 PG 없이 검증 가능한 부분).

실 PG 의미 검증(DST 버킷, 재집계, dirty 경합, gateway 합계 불변)은
tests/regression/test_rollup_v2_realpg.py 에 있다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

import worker.daily_aggregator as da


def _settings(tz="Asia/Seoul", recent=8, lookback=30):
    s = MagicMock()
    s.reporting_timezone = tz
    s.daily_usage_agg_recent_days = recent
    s.daily_usage_agg_backfill_lookback_days = lookback
    return s


def test_day_window_follows_dst_in_new_york():
    """전환일 하루는 23h/25h — 고정 오프셋·+24h 산술이면 경계가 1시간 틀어진다."""
    ny = ZoneInfo("America/New_York")
    s, e = da._day_window(ny, date(2026, 3, 8))
    assert (s, e) == (
        datetime(2026, 3, 8, 5, tzinfo=UTC),
        datetime(2026, 3, 9, 4, tzinfo=UTC),
    )
    assert (e - s).total_seconds() == 23 * 3600
    s, e = da._day_window(ny, date(2026, 11, 1))
    assert (e - s).total_seconds() == 25 * 3600
    s, e = da._day_window(ZoneInfo("Asia/Kolkata"), date(2026, 11, 1))
    assert s == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)


def test_agg_sql_groups_by_full_grain():
    """GROUP BY 가 v2 유니크 입도와 같아야 한다 — 어긋나면 한 날짜 INSERT 가 자체 충돌한다."""
    group_by = da._AGG_SQL.split("GROUP BY")[-1]
    for col in ("user_id", "team_id", "dept_id", "model_alias",
                "COALESCE(client, 'other')", "status"):
        assert col in group_by, col
    # 상태를 거르지 않는다 — gateway /v1/usage/* 합계(전 status)가 그대로여야 한다.
    assert "status =" not in da._AGG_SQL and "SUCCESS" not in da._AGG_SQL
    assert "ON CONFLICT" not in da._AGG_SQL  # 날짜 단위 DELETE+INSERT


def test_sql_bind_params_are_declared():
    """주석 속 `:word` 가 바인드 파라미터로 파싱되면 실 PG 실행이 깨진다(R23 r3)."""
    expected = {
        "_LOCK_DAY_SQL": {"day"},
        "_DELETE_DAY_SQL": {"day"},
        "_AGG_SQL": {"day", "start", "end"},
        "_MARK_DAY_SQL": {"day", "tz", "grain"},
        "_MONTH_READY_SQL": {"period", "through", "tz", "grain"},
        "_DELETE_MONTH_SQL": {"period"},
        "_MONTH_SQL": {"period", "through"},
        "_MARK_MONTH_SQL": {"period", "tz", "through"},
        "_UNTRUSTED_DAYS_SQL": {"from_day", "to_day", "today", "tz", "grain"},
        "_EARLIEST_DAY_SQL": {"tz"},
        "_MISSING_DATES_SQL": {"tz", "lookback_start", "lookback_start_date", "today_start"},
    }
    for name, binds in expected.items():
        assert set(text(getattr(da, name))._bindparams.keys()) == binds, name


def _recording_session(rowcount=3, month_ready=True):
    session = AsyncMock()
    calls: list[tuple[str, dict]] = []

    async def _execute(stmt, params=None):
        sql = str(stmt)
        calls.append((sql, params or {}))
        r = MagicMock()
        r.rowcount = rowcount
        r.scalar = lambda: month_ready
        r.__iter__ = lambda self=None: iter([])
        return r

    session.execute = AsyncMock(side_effect=_execute)
    return session, calls


@pytest.mark.asyncio
async def test_aggregate_day_locks_then_replaces_in_one_transaction():
    """잠금 → DELETE → INSERT → clean 표시 → commit 1회. 순서가 바뀌면 dirty 경합 창이 생긴다."""
    session, calls = _recording_session()
    n = await da.aggregate_day(session, "Asia/Kolkata", date(2026, 11, 20))
    assert n == 3
    kinds = [c[0].split()[0] for c in calls]
    assert kinds == ["INSERT", "DELETE", "INSERT", "UPDATE"]
    assert "daily_aggregate_days" in calls[0][0]
    assert "dirty = false" in calls[3][0]
    assert calls[3][1]["tz"] == "Asia/Kolkata" and calls[3][1]["grain"] == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recompute_days_skips_today_and_rebuilds_touched_months():
    session, calls = _recording_session()
    today = date(2026, 12, 2)
    await da.recompute_days(
        session, "Asia/Seoul",
        [date(2026, 10, 30), date(2026, 12, 1), today, date(2026, 12, 1)],
        today_local=today,
    )
    aggregated = [c[1]["day"] for c in calls
                  if c[0].startswith("\nINSERT INTO usage.daily_aggregates")]
    assert aggregated == [date(2026, 10, 30), date(2026, 12, 1)]  # 오늘 제외·중복 제거
    months = [c[1]["period"] for c in calls if "INSERT INTO usage.monthly_aggregates" in c[0]]
    assert months == [date(2026, 10, 1), date(2026, 12, 1)]
    dec = next(c[1] for c in calls if "INSERT INTO usage.monthly_aggregates" in c[0]
               and c[1]["period"] == date(2026, 12, 1))
    assert dec["through"] == date(2026, 12, 1)  # 이번 달은 어제까지


@pytest.mark.asyncio
async def test_rebuild_month_skips_when_untrusted_and_on_first_day():
    session, calls = _recording_session(month_ready=False)
    assert await da.rebuild_month(session, "UTC", date(2026, 11, 1), date(2026, 11, 20)) is None
    assert not any("monthly_aggregates" in c[0] and "INSERT" in c[0] for c in calls)
    session2, calls2 = _recording_session()
    assert await da.rebuild_month(session2, "UTC", date(2026, 11, 1), date(2026, 11, 1)) is None
    assert calls2 == []


@pytest.mark.asyncio
async def test_nightly_days_union_recent_and_untrusted(monkeypatch):
    session = AsyncMock()
    r = MagicMock()
    r.__iter__ = lambda self=None: iter([(date(2026, 1, 5),), (date(2026, 11, 22),)])
    session.execute = AsyncMock(return_value=r)
    days = await da.nightly_days(
        session, "UTC", date(2026, 11, 24), recent_days=2, lookback_days=30
    )
    assert days == [date(2026, 1, 5), date(2026, 11, 22), date(2026, 11, 23)]
    params = session.execute.await_args.args[1]
    assert params["from_day"] == date(2026, 10, 25) and params["to_day"] == date(2026, 11, 23)


@pytest.mark.asyncio
async def test_startup_backfill_takes_the_aggregation_lock(monkeypatch):
    """기동 백필도 야간 잡·CLI 와 같은 advisory lock 아래에서만 — 못 잡으면 건너뛴다."""
    monkeypatch.setattr(da, "get_settings", lambda: _settings())
    ran = []

    class _Lock:
        def __init__(self, got):
            self.got = got

        async def __aenter__(self):
            return self.got

        async def __aexit__(self, *a):
            return False

    async def _backfill(session):
        ran.append(True)
        return 0

    monkeypatch.setattr(da, "backfill_if_empty", _backfill)
    factory = MagicMock()
    factory.kw = {"bind": object()}
    factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(da, "aggregation_lock", lambda engine, wait_seconds=0: _Lock(False))
    await da.run_startup_backfill(factory)
    assert ran == []
    monkeypatch.setattr(da, "aggregation_lock", lambda engine, wait_seconds=0: _Lock(True))
    await da.run_startup_backfill(factory)
    assert ran == [True]


@pytest.mark.asyncio
async def test_startup_backfill_empty_history_is_noop(monkeypatch):
    monkeypatch.setattr(da, "get_settings", lambda: _settings())
    session = AsyncMock()
    r = MagicMock()
    r.scalar_one_or_none = lambda: None
    session.execute = AsyncMock(return_value=r)
    assert await da.backfill_if_empty(session) == 0
    assert all("daily_aggregates (" not in str(c.args[0]) for c in session.execute.await_args_list)
