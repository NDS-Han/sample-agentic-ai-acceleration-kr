# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregator 단위 테스트 (실 PG 없이 검증 가능한 부분).

실 PG 의미 검증(DST 버킷, 재집계, dirty 경합, gateway 합계 불변)은
tests/regression/test_rollup_v2_realpg.py 에 있다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

import worker.daily_aggregator as da


def _settings(tz="Asia/Seoul", recent=2, lookback=30):
    s = MagicMock()
    s.reporting_timezone = tz
    s.daily_usage_agg_recent_days = recent
    s.daily_usage_agg_backfill_lookback_days = lookback
    s.daily_usage_agg_command_timeout = 900.0
    s.daily_usage_agg_nightly_lock_wait_sec = 0
    s.daily_usage_agg_failed_retry_sec = 10800
    s.daily_usage_agg_hourly_bridge_days = 2
    return s


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.setattr(da, "get_settings", lambda: _settings())


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
    """GROUP BY 가 유니크 입도와 같아야 한다 — 어긋나면 한 날짜 INSERT 가 자체 충돌한다."""
    for sql in (da._AGG_SQL, da._HOUR_SQL):
        group_by = sql.split("GROUP BY")[-1]
        for col in ("user_id", "team_id", "dept_id", "model_alias",
                    "COALESCE(client, 'other')", "status"):
            assert col in group_by, col
        # 상태를 거르지 않는다 — gateway /v1/usage/* 합계(전 status)가 그대로여야 한다.
        assert "status =" not in sql and "SUCCESS" not in sql
        assert "ON CONFLICT" not in sql  # 날짜/시간 단위 DELETE+INSERT
        # 웹 검색 3값(수·비용·미추적 수) — admin-api 가 raw 와 같은 값을 합성한다.
        assert "SUM(web_search_cost_usd)" in sql and "web_search_untracked" in sql


def test_sql_bind_params_are_declared():
    """주석 속 `:word` 가 바인드 파라미터로 파싱되면 실 PG 실행이 깨진다(R23 r3)."""
    expected = {
        "_LOCK_DAY_SQL": {"day"},
        "_DELETE_DAY_SQL": {"day"},
        "_AGG_SQL": {"day", "start", "end"},
        "_CLEAR_DAY_MARKS_SQL": {"day"},
        "_DELETE_DAY_HOURS_SQL": {"day"},
        "_DELETE_DAY_HOUR_REG_SQL": {"day"},
        "_MARK_DAY_SQL": {"day", "tz", "grain"},
        "_FAIL_DAY_SQL": {"day", "err"},
        "_LOCK_HOUR_SQL": {"hs", "day", "tz"},
        "_DELETE_HOUR_SQL": {"hs"},
        "_HOUR_SQL": {"hs", "day", "start", "end"},
        "_CLEAR_HOUR_MARKS_SQL": {"hs"},
        "_MARK_HOUR_SQL": {"hs", "day", "tz", "grain"},
        "_MONTH_READY_SQL": {"period", "through", "tz", "grain"},
        "_DELETE_MONTH_SQL": {"period"},
        "_MONTH_SQL": {"period", "through"},
        "_MARK_MONTH_SQL": {"period", "tz", "through", "grain"},
        "_UNTRUSTED_DAYS_SQL": {"from_day", "to_day", "today", "tz", "grain"},
        "_DIRTY_DAYS_SQL": {"from_day", "today", "retry_after"},
        "_GOOD_HOURS_SQL": {"days", "tz", "grain"},
        "_MARKED_HOURS_SQL": {"days"},
        "_DAY_TRUSTED_SQL": {"days", "tz", "grain"},
        "_EARLIEST_IN_WINDOW_SQL": {"lookback_start"},
        "_EARLIEST_DAY_SQL": set(),
        "_STALE_MONTHS_SQL": {"from_day", "today", "tz", "grain"},
    }
    for name, binds in expected.items():
        assert set(text(getattr(da, name))._bindparams.keys()) == binds, name


def test_startup_and_cli_earliest_are_index_friendly():
    """기동 점검/CLI 의 최초 시각은 min(requested_at) — 컬럼을 함수로 감싸면 전체 스캔이다."""
    for sql in (da._EARLIEST_DAY_SQL, da._EARLIEST_IN_WINDOW_SQL):
        assert "min(requested_at)" in sql and "AT TIME ZONE" not in sql
    assert "requested_at >= :lookback_start" in da._EARLIEST_IN_WINDOW_SQL


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
    """타임아웃 → 잠금 → DELETE → INSERT → 표시·시간 행 정리 → clean 표시 → commit 1회."""
    session, calls = _recording_session()
    n = await da.aggregate_day(session, "Asia/Kolkata", date(2026, 11, 20))
    assert n == 3
    kinds = [c[0].split()[0] for c in calls]
    assert kinds == ["SET", "INSERT", "DELETE", "INSERT", "DELETE", "DELETE", "DELETE", "UPDATE"]
    assert "statement_timeout = '900s'" in calls[0][0]
    assert "daily_aggregate_days" in calls[1][0]
    assert "rollup_dirty_marks" in calls[4][0]
    assert "hourly_aggregates" in calls[5][0] and "hourly_aggregate_hours" in calls[6][0]
    assert "dirty = false" in calls[7][0] and "failed_at = NULL" in calls[7][0]
    assert calls[7][1]["tz"] == "Asia/Kolkata" and calls[7][1]["grain"] == 3
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregate_hour_one_transaction():
    session, calls = _recording_session(rowcount=5)
    s = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)
    n = await da.aggregate_hour(session, "Asia/Kolkata", date(2026, 11, 24), s,
                                s + da._HOUR)
    assert n == 5
    kinds = [c[0].split()[0] for c in calls]
    assert kinds == ["SET", "INSERT", "DELETE", "INSERT", "DELETE", "UPDATE"]
    assert "hourly_aggregate_hours" in calls[1][0]
    assert "rollup_dirty_marks" in calls[4][0] and calls[4][1]["hs"] == s
    assert calls[5][1]["grain"] == 3
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recompute_days_skip_forward_marks_failed_and_continues():
    """한 날짜가 실패해도 다음 날짜를 집계하고, 실패한 날짜에 failed_at 을 남긴다."""
    from sqlalchemy.exc import DBAPIError

    session, calls = _recording_session()
    inner = session.execute.side_effect

    async def _execute(stmt, params=None):
        if "INSERT INTO usage.daily_aggregates" in str(stmt) and params["day"] == date(2026, 11, 2):
            raise DBAPIError("INSERT", {},
                             Exception("canceling statement due to statement timeout"))
        return await inner(stmt, params)

    session.execute = AsyncMock(side_effect=_execute)
    await da.recompute_days(session, "UTC", [date(2026, 11, 1), date(2026, 11, 2),
                                             date(2026, 11, 3)], today_local=date(2026, 11, 10))
    done = [c[1]["day"] for c in calls
            if c[0].lstrip().startswith("UPDATE usage.daily_aggregate_days")]
    assert done == [date(2026, 11, 1), date(2026, 11, 3)]
    failed = [c[1] for c in calls if "failed_at, last_error" in c[0]]
    assert len(failed) == 1 and failed[0]["day"] == date(2026, 11, 2)
    assert "statement timeout" in failed[0]["err"]


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
    # 월 레지스트리에 grain 3 을 남긴다 — admin-api 는 grain 3 월만 읽는다.
    session3, calls3 = _recording_session()
    await da.rebuild_month(session3, "UTC", date(2026, 11, 1), date(2026, 11, 20))
    mark = next(c for c in calls3 if "monthly_aggregate_months" in c[0])
    assert mark[1]["grain"] == 3


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
    session = AsyncMock()
    r = MagicMock()
    r.scalar_one_or_none = lambda: None
    session.execute = AsyncMock(return_value=r)
    assert await da.backfill_if_empty(session) == 0
    assert all("daily_aggregates (" not in str(c.args[0]) for c in session.execute.await_args_list)
    # 전체 이력 스캔 없이 lookback 경계로 묶인 min(requested_at) 하나만 묻는다.
    first = session.execute.await_args_list[0]
    assert "requested_at >= :lookback_start" in str(first.args[0])


@pytest.mark.asyncio
async def test_missing_days_bounded_to_lookback():
    session = AsyncMock()
    lo = MagicMock()
    lo.scalar_one_or_none = lambda: datetime(2025, 1, 1, tzinfo=UTC)  # 아주 오래된 데이터
    rows = MagicMock()
    rows.__iter__ = lambda self=None: iter([(date(2026, 10, 30),), (date(2026, 11, 20),)])
    session.execute = AsyncMock(side_effect=[lo, rows])
    days = await da.missing_days(session, "UTC", date(2026, 11, 24), lookback_days=30)
    assert days == [date(2026, 10, 30), date(2026, 11, 20)]
    params = session.execute.await_args_list[1].args[1]
    assert params["from_day"] == date(2026, 10, 25)  # lookback 하한(전체 이력 아님)


@pytest.mark.asyncio
async def test_pending_hours_only_complete_unaggregated_or_marked():
    tz = "Asia/Kolkata"
    today = date(2026, 11, 24)
    b = da.hour_buckets(ZoneInfo(tz), today)
    now = b[5][0] + timedelta(minutes=10)  # 6번째 시간 진행 중
    yday_last = da.hour_buckets(ZoneInfo(tz), date(2026, 11, 23))[-1]

    def _rows(items):
        r = MagicMock()
        r.__iter__ = lambda self=None: iter(items)
        return r

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        # 어제는 일 집계 신뢰 불가(야간 잡 전), 그제는 신뢰 가능
        _rows([(today, False), (date(2026, 11, 23), False), (date(2026, 11, 22), True)]),
        # 집계된 시간: 오늘 0~3, 어제 전부(마지막 시간 제외)
        _rows([(s,) for s, _ in b[:4]]
              + [(s,) for s, _ in da.hour_buckets(ZoneInfo(tz), date(2026, 11, 23))[:-1]]),
        # 표시된 시간: 오늘 1번
        _rows([(b[1][0],)]),
    ])
    out = await da.pending_hours(session, tz, today, now, bridge_days=2)
    assert [(d, s) for d, s, _ in out] == [
        (date(2026, 11, 23), yday_last[0]),
        (today, b[1][0]), (today, b[4][0]),
    ]  # 진행 중인 5번은 제외


@pytest.mark.asyncio
async def test_hourly_job_skips_when_lock_held(monkeypatch):
    class _Lock:
        async def __aenter__(self):
            return False

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(da, "aggregation_lock", lambda engine, wait_seconds=0: _Lock())
    factory = MagicMock()
    factory.kw = {"bind": object()}
    await da.run_hourly_rollup(factory)
    factory.assert_not_called()
