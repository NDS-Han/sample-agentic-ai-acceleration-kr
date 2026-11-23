# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""DST 리전 배포의 집계 경계 + REPORTING_TIMEZONE 검증.

US/EU 배포(America/Los_Angeles·America/New_York·Europe/Berlin·Europe/London)는 DST
가 있어 전환일 하루가 23h(봄)·25h(가을)다. 경계는 리포팅 tz 의 달력 날짜 00:00 을
zoneinfo 로 UTC 에 옮긴 값이어야 하고, 고정 오프셋·`+24h` 산술이면 1시간이 옆 날로
샌다. 기대 UTC 값은 ZoneInfo 로 계산하지 않고 손으로 적는다(동어반복 방지).

2026 전환일: US 03-08(봄)·11-01(가을), EU 03-29(봄)·10-25(가을).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import usage_filters as uf
from app.core.config import Settings, get_settings


@pytest.fixture
def reporting_tz(monkeypatch):
    def _set(tz: str) -> None:
        monkeypatch.setenv("REPORTING_TIMEZONE", tz)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


# ── B1: 일 버킷 = 23h/25h ──────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize(
    ("tz", "day", "start", "end", "hours"),
    [
        # US 봄: 02:00 → 03:00 (하루 23h)
        ("America/Los_Angeles", "2026-03-08", "2026-03-08T08:00Z", "2026-03-09T07:00Z", 23),
        ("America/New_York", "2026-03-08", "2026-03-08T05:00Z", "2026-03-09T04:00Z", 23),
        # US 가을: 02:00 → 01:00 (01시대 반복, 하루 25h)
        ("America/Los_Angeles", "2026-11-01", "2026-11-01T07:00Z", "2026-11-02T08:00Z", 25),
        ("America/New_York", "2026-11-01", "2026-11-01T04:00Z", "2026-11-02T05:00Z", 25),
        # EU 봄: 01:00Z 에 전환
        ("Europe/Berlin", "2026-03-29", "2026-03-28T23:00Z", "2026-03-29T22:00Z", 23),
        ("Europe/London", "2026-03-29", "2026-03-29T00:00Z", "2026-03-29T23:00Z", 23),
        # EU 가을
        ("Europe/Berlin", "2026-10-25", "2026-10-24T22:00Z", "2026-10-25T23:00Z", 25),
        ("Europe/London", "2026-10-25", "2026-10-24T23:00Z", "2026-10-26T00:00Z", 25),
        # 회귀 — DST 없는 존은 항상 24h, 종전 값 그대로
        ("Asia/Kolkata", "2026-03-08", "2026-03-07T18:30Z", "2026-03-08T18:30Z", 24),
        ("Asia/Kolkata", "2026-11-01", "2026-10-31T18:30Z", "2026-11-01T18:30Z", 24),
        ("Asia/Seoul", "2026-03-29", "2026-03-28T15:00Z", "2026-03-29T15:00Z", 24),
        ("Asia/Seoul", "2026-10-25", "2026-10-24T15:00Z", "2026-10-25T15:00Z", 24),
    ],
)
def test_transition_day_bucket_is_23h_or_25h(reporting_tz, tz, day, start, end, hours):
    reporting_tz(tz)
    got_start, got_end = uf.day_range_to_utc(day, day)
    assert (got_start, got_end) == (_utc(start), _utc(end))
    assert got_end - got_start == timedelta(hours=hours)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tz", "first", "last"),
    [
        ("America/Los_Angeles", "2026-03-07", "2026-03-09"),
        ("America/New_York", "2026-10-31", "2026-11-02"),
        ("Europe/Berlin", "2026-03-28", "2026-03-30"),
        ("Europe/London", "2026-10-24", "2026-10-26"),
    ],
)
def test_consecutive_day_buckets_tile_without_gap_or_overlap(reporting_tz, tz, first, last):
    """전환일을 낀 연속 일 버킷: 앞날의 끝 == 다음날의 시작(빈틈·겹침 0), 합계 = 구간."""
    reporting_tz(tz)
    days = []
    d = datetime.strptime(first, "%Y-%m-%d").date()
    end = datetime.strptime(last, "%Y-%m-%d").date()
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    buckets = [uf.day_range_to_utc(x, x) for x in days]
    for (_, a_end), (b_start, _) in zip(buckets, buckets[1:], strict=False):
        assert a_end == b_start
    assert (buckets[0][0], buckets[-1][1]) == uf.day_range_to_utc(first, last)
    total = buckets[-1][1] - buckets[0][0]
    # 3일 중 하루가 전환일 → 71h(봄) 또는 73h(가을)
    assert total in (timedelta(hours=71), timedelta(hours=73))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tz", "period", "start", "end"),
    [
        ("America/New_York", "2026-03", "2026-03-01T05:00Z", "2026-04-01T04:00Z"),
        ("America/New_York", "2026-11", "2026-11-01T04:00Z", "2026-12-01T05:00Z"),
        ("Europe/Berlin", "2026-03", "2026-02-28T23:00Z", "2026-03-31T22:00Z"),
        ("Europe/Berlin", "2026-10", "2026-09-30T22:00Z", "2026-10-31T23:00Z"),
        ("Europe/London", "2026-03", "2026-03-01T00:00Z", "2026-03-31T23:00Z"),
        ("Europe/London", "2026-10", "2026-09-30T23:00Z", "2026-11-01T00:00Z"),
        # 회귀
        ("Asia/Kolkata", "2026-03", "2026-02-28T18:30Z", "2026-03-31T18:30Z"),
        ("Asia/Seoul", "2026-10", "2026-09-30T15:00Z", "2026-10-31T15:00Z"),
    ],
)
def test_month_boundaries_on_dst_months(reporting_tz, tz, period, start, end):
    reporting_tz(tz)
    assert uf.period_to_utc_range(period) == (_utc(start), _utc(end))


@pytest.mark.unit
@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Europe/Berlin", "Asia/Kolkata"])
def test_sql_day_bucket_uses_iana_name_not_offset(reporting_tz, tz):
    """일별 추이의 GROUP BY 식 = date(timezone('<IANA>', requested_at)) — PG 가 행마다
    그 시점의 오프셋을 적용한다. '+09'·INTERVAL 같은 고정 오프셋이 아니어야 한다."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from app.models.usage import UsageLog

    reporting_tz(tz)
    expr = func.date(func.timezone(uf.reporting_tz_sql(), UsageLog.requested_at))
    sql = str(
        select(expr).compile(
            dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
        )
    )
    assert f"timezone('{tz}', usage.usage_logs.requested_at)" in sql
    assert "interval" not in sql.lower()


# ── B1: REPORTING_TIMEZONE 는 정규 IANA 이름만 ────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize(
    "tz",
    [
        "America/Los_Angeles", "America/New_York", "Europe/Berlin", "Europe/London",
        "Asia/Kolkata", "Asia/Seoul", "UTC", "Etc/GMT+5",
    ],
)
def test_reporting_timezone_accepts_canonical_iana(tz):
    assert Settings(REPORTING_TIMEZONE=tz).REPORTING_TIMEZONE == tz


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tz", "hint"),
    [
        # 고정 오프셋 레거시 존 — tzdata 가 있으면 ZoneInfo 는 받아 주지만 DST 가 없다
        ("EST", "America/New_York"),
        ("MST", "America/Denver"),
        ("EST5EDT", "America/New_York"),
        ("CET", "Europe/Berlin"),
        ("US/Pacific", "America/Los_Angeles"),
        ("GMT", "UTC"),
        ("IST", "Asia/Kolkata"),
        ("utc", "UTC"),
    ],
)
def test_reporting_timezone_rejects_legacy_or_abbreviation(tz, hint):
    with pytest.raises(ValueError) as ei:
        Settings(REPORTING_TIMEZONE=tz)
    assert "not a valid IANA timezone name" in str(ei.value)
    assert hint in str(ei.value)
