# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Usage rollups — usage_logs → 시간별 / 일별 / 월 누적 롤업 (cost-recorder-worker).

admin-api 의 Dashboard·Analytics·웹 검색·사용자×모델 읽기는 이 롤업을 하이브리드로 읽는다
(admin-api ``services/usage_rollup.py``): 신뢰 가능한 날짜 = 일별/월 누적, 오늘(과 아직 일 집계가
없는 날) = 완료된 시간의 시간별 롤업, 현재 진행 중인 시간만 raw.

## 입도 (migration 0060/0061, grain_version 3)
(date|hour_start, user_id, team_id, dept_id, model_alias, client, status) 한 행.
  - date   : settings.reporting_timezone 캘린더 날짜. admin-api REPORTING_TIMEZONE 과 같아야
             한다 — 레지스트리(tz 열)에 기록해 admin-api 는 tz 가 다르면 롤업을 안 믿는다.
  - hour_start : 그 날짜 00:00(리포팅 tz) + k 시간(UTC 저장). 날짜 경계와 항상 맞는다
             (Asia/Kolkata 처럼 30분 오프셋이어도, DST 전환일 23h/25h 여도).
  - client : COALESCE(usage_logs.client,'other') — admin-api 앱 필터와 같은 접힘.
  - status : SUCCESS/ERROR/TIMEOUT 모두. Analytics 비용은 SUCCESS, 웹 검색·에러 수는 전 status.
  - team/dept : 요청 시점 귀속(usage_logs 값 그대로) — 팀 스코프가 raw 와 일치한다.
  - request_count(계량 행) / call_count(count(*)) / latency_ms_sum
  - web_search_count / web_search_cost_usd(SUM, 추적 행이 없으면 NULL) / web_search_untracked
    (비용 NULL 행의 검색 수) — admin-api ws_from_row 와 같은 세 값이 합성 가능하다.

## 재집계 = REPEATABLE READ 한 트랜잭션
레지스트리 행 잠금(UPSERT) → 롤업 DELETE → INSERT … SELECT → dirty 표시 DELETE → clean 표시.
REPEATABLE READ 라 집계 SELECT 와 표시 DELETE 가 **같은 스냅샷**을 본다: 스냅샷 뒤에 커밋된
늦은 로그의 표시(rollup_dirty_marks)는 보이지 않으므로 지워지지 않고 남는다 → 다음 시간 잡이
그 날짜/시간을 다시 집계한다. (READ COMMITTED 면 문장마다 스냅샷이 달라 그 사이에 커밋된
표시를 지우고 행은 놓치는 창이 있다.)

## 늦게 도착한 로그 (rollup_dirty_marks)
batch_flusher 가 이미 끝난(또는 곧 끝날) 시간·날짜의 usage_logs 를 넣으면 같은 트랜잭션에서
표시 행을 INSERT 한다. 유니크가 없는 INSERT 라 **어떤 잠금도 기다리지 않는다** — 예전에는
레지스트리 행을 UPDATE 해서 재집계 트랜잭션의 행 잠금(cold 47~88s)을 flush 가 기다렸다.

## 작업
  - 시간 잡(매시 :05): 오늘(과 일 집계가 없는 최근 날짜)의 완료된 시간 중 미집계·표시된 시간,
    그리고 dirty/표시/실패(재시도 대기 경과)한 과거 날짜를 다시 집계 + 월 누적 갱신.
  - 야간 잡(00:10): 최근 N일 + lookback 안의 신뢰 불가 날짜(레지스트리 없음·grain<3·dirty·표시).
  - 기동 점검: lookback 안에서만(min(requested_at) 은 인덱스 끝점 조회) — 전체 이력 스캔 없음.
날짜 하나가 실패하면 그 날짜에 failed_at/last_error 를 남기고 **다음 날짜로 넘어간다**
(skip-forward). 모든 쓰기는 advisory lock 727_001 아래에서만 돈다.

## 전용 엔진
집계는 워커 flush 엔진(command_timeout 60s)과 분리된 작은 풀을 쓴다 — cold 하루 집계가 60s 를
넘어 실패하던 문제(실측 47~88s)와, 집계가 flush 커넥션을 점유하는 문제를 함께 없앤다.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from worker.config import get_settings

logger = structlog.get_logger(__name__)

#: daily_aggregate_days / hourly_aggregate_hours / monthly_aggregate_months.grain_version —
#: admin-api 가 이 값 이상만 신뢰한다. 3 = 웹 검색 열 포함(0061).
GRAIN_VERSION = 3

_HOUR = timedelta(hours=1)


# ── 시간 경계 ────────────────────────────────────────────────────────────────


def _day_window(tz: ZoneInfo, local_day: date) -> tuple[datetime, datetime]:
    """로컬 날짜 하루의 [start, end) UTC 구간 — DST 전환일은 23h/25h 다."""
    start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def hour_buckets(tz: ZoneInfo, local_day: date) -> list[tuple[datetime, datetime]]:
    """그 날짜의 시간 버킷 [start, end) 목록 — 00:00(로컬) + k 시간, 마지막은 날짜 끝에서 자른다.

    admin-api ``usage_rollup.hour_buckets`` 와 같은 정의여야 한다(테스트가 고정).
    """
    start, end = _day_window(tz, local_day)
    out: list[tuple[datetime, datetime]] = []
    s = start
    while s < end:
        e = min(s + _HOUR, end)
        out.append((s, e))
        s = e
    return out


def bucket_of(tz: ZoneInfo, ts: datetime) -> tuple[date, datetime, datetime]:
    """시각 → (로컬 날짜, 시간 버킷 시작, 버킷 끝)."""
    d = ts.astimezone(tz).date()
    start, end = _day_window(tz, d)
    k = int((ts.astimezone(UTC) - start).total_seconds() // 3600)
    hs = start + k * _HOUR
    return d, hs, min(hs + _HOUR, end)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(p: date) -> date:
    nxt = date(p.year + (p.month == 12), p.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


# ── 지표 / 상태 ──────────────────────────────────────────────────────────────


@dataclass
class RollupStatus:
    """작업별 마지막 성공 시각(epoch)·마지막 실행 시간 — 게이지 콜백이 읽는다."""

    last_success: dict[str, float] = field(default_factory=dict)
    last_duration: dict[str, float] = field(default_factory=dict)


STATUS = RollupStatus()
_METRICS = None  # WorkerMetrics — main 이 set_metrics 로 연결(테스트는 None)


def set_metrics(metrics) -> None:
    global _METRICS
    _METRICS = metrics


def _record_run(job: str, started: float, ok: bool) -> None:
    dur = time.monotonic() - started
    STATUS.last_duration[job] = dur
    if ok:
        STATUS.last_success[job] = time.time()
    if _METRICS is not None:
        _METRICS.rollup_run_duration.record(dur, {"job": job, "outcome": "ok" if ok else "error"})


def _record_unit(job: str, unit: str, ok: bool) -> None:
    if _METRICS is None:
        return
    if ok:
        _METRICS.rollup_units.add(1, {"job": job, "unit": unit})
    else:
        _METRICS.rollup_failures.add(1, {"job": job, "unit": unit})


# ── 공통 집계 SELECT ─────────────────────────────────────────────────────────

#: usage_logs [start, end) → 롤업 입도 한 행. 앞의 키 열은 호출부가 붙인다.
_GRAIN_SELECT = """
  user_id, team_id, dept_id, model_alias,
  COALESCE(client, 'other'),
  status,
  SUM(input_tokens),
  SUM(output_tokens),
  SUM(cache_creation_tokens),
  SUM(cache_read_tokens),
  SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens),
  SUM(cost_usd),
  -- request_count = 종전 정의(0 토큰·0 비용 마커 행 제외) — Redis requests 카운터와 같은 규칙.
  COUNT(*) FILTER (
      WHERE input_tokens + output_tokens
            + cache_creation_tokens + cache_read_tokens > 0
         OR cost_usd > 0
  ),
  COUNT(*),
  SUM(latency_ms),
  SUM(web_search_count),
  -- 추적 행이 없으면 NULL(= 미추적) — admin-api 는 NULL 과 0 을 구분해 보여준다.
  SUM(web_search_cost_usd),
  COALESCE(SUM(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL), 0)
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :end
GROUP BY user_id, team_id, dept_id, model_alias, COALESCE(client, 'other'), status
"""

_GRAIN_COLS = """user_id, team_id, dept_id, model_alias, client, status,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count, call_count, latency_ms_sum,
   web_search_count, web_search_cost_usd, web_search_untracked"""


# ── 일 재집계 ────────────────────────────────────────────────────────────────

#: 레지스트리 행 잠금 — UPSERT 는 기존 행이면 행 잠금을, 새 행이면 삽입 잠금을 잡는다.
_LOCK_DAY_SQL = """
INSERT INTO usage.daily_aggregate_days (date) VALUES (:day)
ON CONFLICT (date) DO UPDATE SET date = EXCLUDED.date
"""

_DELETE_DAY_SQL = "DELETE FROM usage.daily_aggregates WHERE date = :day"

_AGG_SQL = f"""
INSERT INTO usage.daily_aggregates
  (date, {_GRAIN_COLS})
SELECT
  CAST(:day AS date),
{_GRAIN_SELECT}"""

#: 이 날짜의 표시(날짜·시간 모두) 중 **이 스냅샷이 본 것만** 지운다(REPEATABLE READ).
_CLEAR_DAY_MARKS_SQL = "DELETE FROM usage.rollup_dirty_marks WHERE date = :day"

#: 일 집계가 생긴 날짜의 시간 롤업은 더 이상 읽히지 않는다 — 지워서 작게 유지한다.
#: (남겨 두면 이 날짜가 나중에 다시 dirty 가 될 때 낡은 시간 행이 대체 소스로 쓰일 수 있다.)
_DELETE_DAY_HOURS_SQL = "DELETE FROM usage.hourly_aggregates WHERE date = :day"
_DELETE_DAY_HOUR_REG_SQL = "DELETE FROM usage.hourly_aggregate_hours WHERE date = :day"

_MARK_DAY_SQL = """
UPDATE usage.daily_aggregate_days
   SET tz = :tz, grain_version = :grain, dirty = false, aggregated_at = clock_timestamp(),
       failed_at = NULL, last_error = NULL
 WHERE date = :day
"""

_FAIL_DAY_SQL = """
INSERT INTO usage.daily_aggregate_days (date, failed_at, last_error)
VALUES (:day, clock_timestamp(), :err)
ON CONFLICT (date) DO UPDATE SET failed_at = EXCLUDED.failed_at, last_error = EXCLUDED.last_error
"""


def _timeout_sql() -> str:
    secs = int(max(1.0, float(get_settings().daily_usage_agg_command_timeout)))
    # 리터럴(검증된 int)이라 인젝션 여지가 없다. SET LOCAL 은 명시 트랜잭션 안에서만 유효하다.
    return f"SET LOCAL statement_timeout = '{secs}s'"


def _is_serialization_failure(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "40001" or "could not serialize" in str(exc).lower()


async def _with_retry(session: AsyncSession, fn, *args):
    """REPEATABLE READ 직렬화 실패(동시에 레지스트리 행을 바꾼 트랜잭션 — 구 워커 롤아웃 중)는
    두 번까지 다시 시도한다. 그 밖의 오류는 그대로 올린다."""
    for attempt in range(3):
        try:
            return await fn(session, *args)
        except DBAPIError as exc:
            await session.rollback()
            if not _is_serialization_failure(exc) or attempt == 2:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")


async def _aggregate_day_once(session: AsyncSession, tz_name: str, day: date) -> int:
    start, end = _day_window(ZoneInfo(tz_name), day)
    await session.execute(text(_timeout_sql()))
    await session.execute(text(_LOCK_DAY_SQL), {"day": day})
    await session.execute(text(_DELETE_DAY_SQL), {"day": day})
    result = await session.execute(text(_AGG_SQL), {"day": day, "start": start, "end": end})
    count: int = result.rowcount
    await session.execute(text(_CLEAR_DAY_MARKS_SQL), {"day": day})
    await session.execute(text(_DELETE_DAY_HOURS_SQL), {"day": day})
    await session.execute(text(_DELETE_DAY_HOUR_REG_SQL), {"day": day})
    await session.execute(
        text(_MARK_DAY_SQL), {"day": day, "tz": tz_name, "grain": GRAIN_VERSION}
    )
    await session.commit()
    return count


async def aggregate_day(session: AsyncSession, tz_name: str, day: date) -> int:
    """하루를 재집계하고 레지스트리를 clean 으로 표시한다(한 트랜잭션 + commit)."""
    return await _with_retry(session, _aggregate_day_once, tz_name, day)


async def _mark_day_failed(session: AsyncSession, day: date, exc: BaseException) -> None:
    """실패 기록 — 실패 원인이 그 날짜 레지스트리 행의 잠금이었을 수 있으므로 짧게 기다리고
    포기한다(기록 실패는 로그만 — 다음 날짜로 넘어가는 것이 우선)."""
    try:
        await session.rollback()
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '10s'"))
        await session.execute(text(_FAIL_DAY_SQL), {"day": day, "err": str(exc)[:500]})
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("daily_aggregator.mark_failed_failed", date=str(day), exc_info=True)


# ── 시간 재집계 ──────────────────────────────────────────────────────────────

_LOCK_HOUR_SQL = """
INSERT INTO usage.hourly_aggregate_hours (hour_start, date, tz, grain_version, aggregated_at)
VALUES (:hs, :day, :tz, 0, clock_timestamp())
ON CONFLICT (hour_start) DO UPDATE SET hour_start = EXCLUDED.hour_start
"""

_DELETE_HOUR_SQL = "DELETE FROM usage.hourly_aggregates WHERE hour_start = :hs"

_HOUR_SQL = f"""
INSERT INTO usage.hourly_aggregates
  (hour_start, date, {_GRAIN_COLS})
SELECT
  CAST(:hs AS timestamptz), CAST(:day AS date),
{_GRAIN_SELECT}"""

_CLEAR_HOUR_MARKS_SQL = "DELETE FROM usage.rollup_dirty_marks WHERE hour_start = :hs"

_MARK_HOUR_SQL = """
UPDATE usage.hourly_aggregate_hours
   SET date = :day, tz = :tz, grain_version = :grain, aggregated_at = clock_timestamp()
 WHERE hour_start = :hs
"""


async def _aggregate_hour_once(
    session: AsyncSession, tz_name: str, day: date, start: datetime, end: datetime
) -> int:
    p = {"hs": start, "day": day, "tz": tz_name, "start": start, "end": end}
    await session.execute(text(_timeout_sql()))
    await session.execute(text(_LOCK_HOUR_SQL), p)
    await session.execute(text(_DELETE_HOUR_SQL), p)
    count: int = (await session.execute(text(_HOUR_SQL), p)).rowcount
    await session.execute(text(_CLEAR_HOUR_MARKS_SQL), p)
    await session.execute(text(_MARK_HOUR_SQL), {**p, "grain": GRAIN_VERSION})
    await session.commit()
    return count


async def aggregate_hour(
    session: AsyncSession, tz_name: str, day: date, start: datetime, end: datetime
) -> int:
    """완료된 시간 버킷 하나를 재집계한다(한 트랜잭션 + commit)."""
    return await _with_retry(session, _aggregate_hour_once, tz_name, day, start, end)


# ── 월 누적 ──────────────────────────────────────────────────────────────────

#: [period, covered_through] 의 모든 날짜가 신뢰 가능한가(레지스트리 v3·clean·같은 tz·표시 없음).
_MONTH_READY_SQL = """
SELECT COALESCE(bool_and(COALESCE(
         r.grain_version >= :grain AND NOT r.dirty AND r.tz = :tz
         AND r.aggregated_at IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM usage.rollup_dirty_marks m
                         WHERE m.date = g.d::date AND m.hour_start IS NULL), false)), false)
FROM generate_series(CAST(:period AS date), CAST(:through AS date), interval '1 day') AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
"""

_DELETE_MONTH_SQL = "DELETE FROM usage.monthly_aggregates WHERE period = :period"

_MONTH_SQL = """
INSERT INTO usage.monthly_aggregates
  (period, user_id, team_id, dept_id, model_alias, client, status,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, call_count, request_count, latency_ms_sum,
   web_search_count, web_search_cost_usd, web_search_untracked)
SELECT CAST(:period AS date), user_id, team_id, dept_id, model_alias, client, status,
       SUM(input_tokens), SUM(output_tokens), SUM(cache_creation_tokens), SUM(cache_read_tokens),
       SUM(total_tokens), SUM(total_cost_usd), SUM(call_count), SUM(request_count),
       SUM(latency_ms_sum), SUM(web_search_count), SUM(web_search_cost_usd),
       SUM(web_search_untracked)
FROM usage.daily_aggregates
WHERE date >= :period AND date <= :through AND status IS NOT NULL
GROUP BY user_id, team_id, dept_id, model_alias, client, status
"""

_MARK_MONTH_SQL = """
INSERT INTO usage.monthly_aggregate_months (period, tz, covered_through, built_at, grain_version)
VALUES (:period, :tz, :through, clock_timestamp(), :grain)
ON CONFLICT (period) DO UPDATE
   SET tz = EXCLUDED.tz, covered_through = EXCLUDED.covered_through,
       built_at = EXCLUDED.built_at, grain_version = EXCLUDED.grain_version
"""


async def rebuild_month(
    session: AsyncSession, tz_name: str, period: date, today_local: date
) -> int | None:
    """period 달의 월 누적을 [period, min(월말, 어제)] 로 다시 만든다.

    그 구간에 신뢰할 수 없는 날짜가 하나라도 있으면 만들지 않고 None — admin-api 는
    그 달을 일별로 읽는다(정확하고 여전히 롤업). 백필 CLI 가 과거를 채우면 만들어진다.
    """
    through = min(_month_end(period), today_local - timedelta(days=1))
    if through < period:
        return None  # 이번 달 1일 — 완료된 날짜가 아직 없다
    params = {"period": period, "through": through, "tz": tz_name, "grain": GRAIN_VERSION}
    await session.execute(text(_timeout_sql()))
    ready = (await session.execute(text(_MONTH_READY_SQL), params)).scalar()
    if not ready:
        await session.rollback()
        logger.info("monthly_aggregate.skipped_untrusted_days", period=str(period))
        return None
    await session.execute(text(_DELETE_MONTH_SQL), params)
    count: int = (await session.execute(text(_MONTH_SQL), params)).rowcount
    await session.execute(text(_MARK_MONTH_SQL), params)
    await session.commit()
    logger.info(
        "monthly_aggregate.rebuilt", period=str(period), through=str(through), rows=count
    )
    return count


async def recompute_days(
    session: AsyncSession,
    tz_name: str,
    days: Iterable[date],
    *,
    today_local: date,
    log_event: str = "daily_aggregator.ran",
    job: str = "nightly",
) -> int:
    """날짜들을 차례로 재집계(날짜당 commit)한 뒤, 건드린 달과 이번 달의 월 누적을 다시 만든다.

    오늘 이후 날짜는 건너뛴다. 한 날짜가 실패하면 failed_at 을 남기고 다음 날짜로 넘어간다
    (skip-forward) — 실패한 날짜가 낀 달은 rebuild_month 가 알아서 건너뛴다.
    """
    total = 0
    failed: list[date] = []
    months: set[date] = {_month_start(today_local - timedelta(days=1))}
    for day in sorted(set(days)):
        if day >= today_local:
            continue
        t0 = time.monotonic()
        try:
            count = await aggregate_day(session, tz_name, day)
        except Exception as exc:  # noqa: BLE001 — 다음 날짜로 넘어간다
            failed.append(day)
            _record_unit(job, "day", ok=False)
            logger.error("daily_aggregator.day_failed", date=str(day), job=job,
                         error=str(exc)[:300], seconds=round(time.monotonic() - t0, 1))
            await _mark_day_failed(session, day, exc)
            continue
        _record_unit(job, "day", ok=True)
        total += count
        months.add(_month_start(day))
        logger.info(log_event, date=str(day), timezone=tz_name, inserted=count,
                    seconds=round(time.monotonic() - t0, 1))
    for period in sorted(months):
        try:
            await rebuild_month(session, tz_name, period, today_local)
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            _record_unit(job, "month", ok=False)
            logger.error("monthly_aggregate.failed", period=str(period), error=str(exc)[:300])
    if failed:
        logger.warning("daily_aggregator.days_failed", job=job, days=[str(d) for d in failed])
    return total


# ── 재집계 대상 선정 ─────────────────────────────────────────────────────────

#: 날짜가 일 롤업에서 신뢰 가능한지 — admin-api 의 판정과 같은 조건.
_DAY_UNTRUSTED_COND = """(r.date IS NULL OR r.grain_version < :grain OR r.dirty
   OR r.tz IS DISTINCT FROM :tz OR r.aggregated_at IS NULL
   OR EXISTS (SELECT 1 FROM usage.rollup_dirty_marks m
              WHERE m.date = g.d::date AND m.hour_start IS NULL))"""

#: [from, to] 중 신뢰 불가 날짜 + 나이 무관 dirty/표시 날짜(레지스트리가 있는 것).
_UNTRUSTED_DAYS_SQL = f"""
SELECT g.d::date
FROM generate_series(CAST(:from_day AS date), CAST(:to_day AS date), interval '1 day') AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
WHERE {_DAY_UNTRUSTED_COND}
UNION
SELECT date FROM usage.daily_aggregate_days WHERE dirty AND date < :today
UNION
SELECT DISTINCT date FROM usage.rollup_dirty_marks WHERE hour_start IS NULL AND date < :today
"""


async def nightly_days(
    session: AsyncSession, tz_name: str, today_local: date, recent_days: int, lookback_days: int
) -> list[date]:
    """야간 재집계 대상: 최근 N일(항상) ∪ lookback 안의 신뢰 불가 날짜 ∪ 모든 dirty/표시 날짜."""
    days = {today_local - timedelta(days=o) for o in range(1, recent_days + 1)}
    rows = await session.execute(
        text(_UNTRUSTED_DAYS_SQL),
        {
            "from_day": today_local - timedelta(days=lookback_days),
            "to_day": today_local - timedelta(days=1),
            "today": today_local,
            "tz": tz_name,
            "grain": GRAIN_VERSION,
        },
    )
    days.update(r[0] for r in rows)
    return sorted(days)


#: 시간 잡이 다시 집계할 과거 날짜 — 이미 한 번 집계된 날짜 중 dirty·표시·실패(재시도 대기 경과).
#: 레지스트리가 없는 날짜(어제, 야간 잡 전)는 야간 잡 몫이다(같은 날짜를 두 번 집계하지 않게).
_DIRTY_DAYS_SQL = """
SELECT r.date FROM usage.daily_aggregate_days r
WHERE r.date >= :from_day AND r.date < :today
  AND (r.dirty
       OR EXISTS (SELECT 1 FROM usage.rollup_dirty_marks m
                  WHERE m.date = r.date AND m.hour_start IS NULL)
       OR (r.failed_at IS NOT NULL
           AND r.failed_at < clock_timestamp() - make_interval(secs => :retry_after)))
  AND (r.aggregated_at IS NOT NULL OR r.failed_at IS NOT NULL)
ORDER BY 1
"""

#: 시간 버킷 상태 — 신뢰 가능한 시간 + 표시된 시간.
_GOOD_HOURS_SQL = """
SELECT hour_start FROM usage.hourly_aggregate_hours
WHERE date = ANY(CAST(:days AS date[])) AND tz = :tz AND grain_version >= :grain
"""
_MARKED_HOURS_SQL = """
SELECT DISTINCT hour_start FROM usage.rollup_dirty_marks
WHERE date = ANY(CAST(:days AS date[])) AND hour_start IS NOT NULL
"""
_DAY_TRUSTED_SQL = f"""
SELECT g.d::date, NOT {_DAY_UNTRUSTED_COND} AS trusted
FROM unnest(CAST(:days AS date[])) AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
"""


#: 월 누적이 낡았거나 없는 달 — 그 달의 일 롤업이 월 재구성 뒤에 다시 집계됐거나(재집계 중단·
#: 수동 백필 등) 레지스트리 행이 없다. lookback 안의 달만 본다(시간 잡이 재구성한다).
_STALE_MONTHS_SQL = """
SELECT date_trunc('month', r.date)::date AS period
FROM usage.daily_aggregate_days r
LEFT JOIN usage.monthly_aggregate_months m
       ON m.period = date_trunc('month', r.date)::date
WHERE r.date >= :from_day AND r.date < :today AND r.aggregated_at IS NOT NULL
GROUP BY 1
HAVING bool_or(m.period IS NULL OR m.grain_version < :grain OR m.tz IS DISTINCT FROM :tz
               OR r.aggregated_at > m.built_at)
ORDER BY 1
"""


async def stale_months(session: AsyncSession, tz_name: str, today_local: date,
                       lookback_days: int) -> list[date]:
    rows = await session.execute(text(_STALE_MONTHS_SQL), {
        "from_day": today_local - timedelta(days=lookback_days), "today": today_local,
        "tz": tz_name, "grain": GRAIN_VERSION})
    out = [r[0] for r in rows]
    await session.commit()
    return out


async def pending_hours(
    session: AsyncSession, tz_name: str, today_local: date, now: datetime, bridge_days: int
) -> list[tuple[date, datetime, datetime]]:
    """다시 집계할 완료된 시간 버킷: 오늘 + 일 집계가 신뢰 불가인 최근 bridge_days 일."""
    tz = ZoneInfo(tz_name)
    candidates = [today_local - timedelta(days=o) for o in range(0, bridge_days + 1)]
    p = {"days": candidates, "tz": tz_name, "grain": GRAIN_VERSION}
    trusted = {r[0]: r[1] for r in await session.execute(text(_DAY_TRUSTED_SQL), p)}
    days = [d for d in candidates if d == today_local or not trusted.get(d, False)]
    if not days:
        return []
    p["days"] = days
    good = {r[0] for r in await session.execute(text(_GOOD_HOURS_SQL), p)}
    marked = {r[0] for r in await session.execute(text(_MARKED_HOURS_SQL), p)}
    await session.commit()
    out = []
    for d in sorted(days):
        for s, e in hour_buckets(tz, d):
            if e <= now and (s not in good or s in marked):
                out.append((d, s, e))
    return out


#: 기동 점검의 하한 — lookback 안의 가장 이른 요청(인덱스 끝점 조회 — 전체 스캔 아님).
_EARLIEST_IN_WINDOW_SQL = """
SELECT min(requested_at) FROM usage.usage_logs WHERE requested_at >= :lookback_start
"""

#: 백필 CLI 기본 --from — 가장 오래된 요청 시각. 컬럼을 함수로 감싸지 않아야 인덱스 끝점
#: 조회 하나로 끝난다(예전 MIN((requested_at AT TIME ZONE tz)::date) 는 전체 스캔 9~13s).
_EARLIEST_DAY_SQL = "SELECT min(requested_at) FROM usage.usage_logs"


async def earliest_day(session: AsyncSession, tz_name: str) -> date | None:
    ts = (await session.execute(text(_EARLIEST_DAY_SQL))).scalar_one_or_none()
    return ts.astimezone(ZoneInfo(tz_name)).date() if ts is not None else None


async def missing_days(
    session: AsyncSession, tz_name: str, today_local: date, lookback_days: int
) -> list[date]:
    """기동 점검 대상 — lookback 안에서 데이터가 시작된 날짜부터 어제까지의 신뢰 불가 날짜.

    lookback 밖의 과거(0061 이전 grain 2 날짜 포함)는 백필 CLI 가 통제된 속도로 다시 만든다.
    """
    tz = ZoneInfo(tz_name)
    lookback_start = today_local - timedelta(days=lookback_days)
    lo_ts = (
        await session.execute(
            text(_EARLIEST_IN_WINDOW_SQL),
            {"lookback_start": _day_window(tz, lookback_start)[0]},
        )
    ).scalar_one_or_none()
    if lo_ts is None:
        return []
    lo = max(lo_ts.astimezone(tz).date(), lookback_start)
    to_day = today_local - timedelta(days=1)
    if to_day < lo:
        return []
    rows = await session.execute(
        text(_UNTRUSTED_DAYS_SQL),
        {"from_day": lo, "to_day": to_day, "today": today_local, "tz": tz_name,
         "grain": GRAIN_VERSION},
    )
    return sorted({r[0] for r in rows if lo <= r[0] <= to_day})


# ── 잠금 ─────────────────────────────────────────────────────────────────────

#: pg advisory lock 키 — 시간 잡·야간 잡·기동 점검·백필 CLI 가 공유한다. 일/시간 재집계 자체는
#: 레지스트리 행 잠금으로도 안전하지만, 월 재구성의 일관성(일 재집계와 겹치지 않음)과
#: N replica 의 중복 스캔 부하를 이 락이 막는다.
_DAILY_AGG_LOCK_KEY = 727_001


@contextlib.asynccontextmanager
async def aggregation_lock(engine: AsyncEngine, wait_seconds: float = 0,
                           key: int = _DAILY_AGG_LOCK_KEY) -> AsyncIterator[bool]:
    """advisory lock 을 **전용 AUTOCOMMIT 커넥션**에서 잡는다.

    ⚠️ advisory lock 은 커넥션 레벨이다 — 집계 세션이 날짜마다 commit 하면 커넥션이
       풀로 돌아가 unlock 이 다른 커넥션에서 실행될 수 있다(실측: 락 고착 → 전 replica
       영구 스킵). 전용 커넥션이 닫히면 락도 풀리므로 고착 경로가 없다.
    wait_seconds>0 이면 그동안 5초 간격으로 다시 시도한다(야간 잡·백필 CLI).
    key — 기본은 롤업 락(727_001). 비용 대조 잡(reconciliation)은 자기 락(727_002)을 쓴다.
    """
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
                )
            ).scalar()
            if got or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(5.0, max(0.0, deadline - asyncio.get_running_loop().time())))
        try:
            yield bool(got)
        finally:
            if got:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"), {"k": key}
                    )
                except Exception:
                    # 락 커넥션이 끊겼으면 서버 측에서 이미 해제됐다(R23 r3 N-6).
                    logger.warning("daily_aggregator.unlock_failed")


# ── 엔트리포인트 ─────────────────────────────────────────────────────────────


def _today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def _now() -> datetime:
    return datetime.now(UTC)


async def run_daily_aggregation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """APScheduler 가 리포팅 tz 00:10 에 호출. 최근 N일 + 신뢰 불가 + dirty 날짜를 재집계하고
    월 누적을 갱신한다. 다중 replica 에서는 lock 을 잡은 하나만 실행한다(전부 멱등)."""
    started = time.monotonic()
    ok = False
    settings = get_settings()
    try:
        wait = float(getattr(settings, "daily_usage_agg_nightly_lock_wait_sec", 0) or 0)
        async with aggregation_lock(session_factory.kw["bind"], wait) as got:
            if not got:
                # 락 고착이면 이 로그가 매일 남는 유일한 신호다 — WARN.
                logger.warning("daily_aggregator.skipped_lock_held")
                return
            tz_name = settings.reporting_timezone
            today_local = _today(tz_name)
            async with session_factory() as session:
                days = await nightly_days(
                    session, tz_name, today_local,
                    settings.daily_usage_agg_recent_days,
                    settings.daily_usage_agg_backfill_lookback_days,
                )
                await session.commit()
                await recompute_days(session, tz_name, days, today_local=today_local)
            ok = True
    except Exception:
        logger.exception("daily_aggregator.failed")
    finally:
        _record_run("nightly", started, ok)


async def run_hourly_rollup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """매시 :05 — dirty/실패 과거 날짜 재집계 → 완료된 시간 버킷 집계 → 월 누적 갱신.

    lock 을 기다리지 않는다(야간 잡·백필이 쥐고 있으면 이번 회차는 건너뛴다 — admin-api 는
    그동안 그 시간들을 raw 로 읽는다, 정확하지만 조금 느리다).
    """
    started = time.monotonic()
    ok = False
    settings = get_settings()
    try:
        async with aggregation_lock(session_factory.kw["bind"]) as got:
            if not got:
                logger.info("hourly_rollup.skipped_lock_held")
                return
            tz_name = settings.reporting_timezone
            today_local = _today(tz_name)
            async with session_factory() as session:
                # 1) 과거 날짜 — 먼저 한다: 다시 집계된 날짜는 시간 다리가 필요 없다.
                rows = await session.execute(
                    text(_DIRTY_DAYS_SQL),
                    {"from_day": today_local - timedelta(
                        days=settings.daily_usage_agg_backfill_lookback_days),
                     "today": today_local,
                     "retry_after": int(settings.daily_usage_agg_failed_retry_sec)},
                )
                dirty = [r[0] for r in rows]
                await session.commit()
                if dirty:
                    await recompute_days(session, tz_name, dirty, today_local=today_local,
                                         log_event="hourly_rollup.day_recomputed",
                                         job="hourly")
                # 1b) 낡은 월 누적 재구성 — 일 재집계가 월 재구성 전에 끊겼던 달 등. 월 누적이
                #     낡으면 admin-api 는 그 달을 일별로 읽는다(정확하지만 행이 30배 많다).
                for period in await stale_months(
                        session, tz_name, today_local,
                        settings.daily_usage_agg_backfill_lookback_days):
                    try:
                        await rebuild_month(session, tz_name, period, today_local)
                    except Exception as exc:  # noqa: BLE001
                        await session.rollback()
                        _record_unit("hourly", "month", ok=False)
                        logger.error("monthly_aggregate.failed", period=str(period),
                                     error=str(exc)[:300])
                # 2) 시간 버킷 — 오늘 + 일 집계가 아직 신뢰 불가인 최근 날짜.
                hours = await pending_hours(
                    session, tz_name, today_local, _now(),
                    int(settings.daily_usage_agg_hourly_bridge_days),
                )
                done = 0
                for d, s, e in hours:
                    try:
                        await aggregate_hour(session, tz_name, d, s, e)
                        done += 1
                        _record_unit("hourly", "hour", ok=True)
                    except Exception as exc:  # noqa: BLE001 — 다음 시간으로 넘어간다
                        await session.rollback()
                        _record_unit("hourly", "hour", ok=False)
                        logger.error("hourly_rollup.hour_failed", hour_start=s.isoformat(),
                                     error=str(exc)[:300])
                logger.info("hourly_rollup.ran", hours=done, pending=len(hours),
                            dirty_days=[str(d) for d in dirty])
            ok = True
    except Exception:
        logger.exception("hourly_rollup.failed")
    finally:
        _record_run("hourly", started, ok)


async def backfill_if_empty(session: AsyncSession) -> int:
    """기동 시 lookback 안의 신뢰 불가 날짜를 채운다(날짜당 commit, 재개 안전)."""
    settings = get_settings()
    tz_name = settings.reporting_timezone
    today_local = _today(tz_name)
    days = await missing_days(
        session, tz_name, today_local, settings.daily_usage_agg_backfill_lookback_days
    )
    await session.commit()
    if not days:
        return 0
    total = await recompute_days(
        session, tz_name, days, today_local=today_local,
        log_event="daily_aggregator.backfilled_missing_day", job="startup",
    )
    logger.warning(
        "daily_aggregator.startup_backfill_recovered",
        days=[str(d) for d in days],
        inserted=total,
    )
    return total


async def run_startup_backfill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """기동 시 1회 — 시간·야간 잡·백필 CLI 와 같은 advisory lock 아래에서만 돈다."""
    started = time.monotonic()
    ok = False
    try:
        async with aggregation_lock(session_factory.kw["bind"]) as got:
            if not got:
                logger.info("daily_aggregator.startup_backfill_skipped_lock_held")
                return
            async with session_factory() as session:
                count = await backfill_if_empty(session)
                logger.info("daily_aggregator.startup_backfill_done", count=count)
            ok = True
    except Exception:
        logger.exception("daily_aggregator.startup_backfill_failed")
    finally:
        _record_run("startup", started, ok)
