# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Daily Usage Aggregator — usage_logs → usage.daily_aggregates (+ 월 누적) 롤업.

admin-api/scheduler/daily_usage_aggregator.py 로부터 이관 (2026-04-21).
cost-recorder-worker 가 usage_logs 쓰기를 소유하므로 집계도 같은 프로세스가 맡는다.

## 입도 (migration 0060, grain_version 2)
(date, user_id, team_id, dept_id, model_alias, client, status) 한 행.
  - date   : settings.reporting_timezone 캘린더 날짜. admin-api REPORTING_TIMEZONE 과 같아야
             한다 — 레지스트리(tz 열)에 기록해 admin-api 는 tz 가 다르면 롤업을 안 믿는다.
  - client : COALESCE(usage_logs.client,'other') — admin-api 앱 필터와 같은 접힘.
  - status : SUCCESS/ERROR/TIMEOUT 을 모두 담는다. Analytics 는 SUCCESS 만 읽고,
             gateway-proxy /v1/usage/* 는 전 행을 합산하므로 종전 합계가 그대로다.
  - team/dept : 요청 시점 귀속(usage_logs 값 그대로) — 팀 스코프가 raw 와 일치한다.
  - request_count 는 종전 정의(계량 행만), call_count = count(*), latency_ms_sum = SUM(latency_ms).

## 날짜 재집계 = 레지스트리 행 잠금 + DELETE + INSERT (한 트랜잭션)
usage.daily_aggregate_days 의 그 날짜 행을 UPSERT 로 잠그고, 그 날짜 롤업을 지우고
다시 넣은 뒤 (grain_version=2, dirty=false, tz, aggregated_at) 로 표시한다.
  - 같은 날짜의 동시 재집계(멀티 replica·백필 CLI)는 이 행 잠금으로 직렬화된다 —
    예전 DELETE+INSERT 가 유니크 충돌로 깨졌던 이유(동시 실행)가 사라진다.
  - DELETE 가 있어야 레거시 입도 행(0060 이전)과 tz 변경 전의 행이 교체된다.

## 늦게 도착한 로그 (dirty)
batch_flusher 가 과거 날짜(리포팅 tz 기준 오늘 이전)의 usage_logs 를 넣으면 같은
트랜잭션에서 레지스트리 행을 dirty=true 로 만든다. 그 UPDATE 는 위 재집계가 잡은 행
잠금을 기다리므로, 재집계 스냅샷이 못 본 행은 항상 재집계 **이후**에 dirty 를 다시 켠다
(경합으로 dirty 가 지워지는 창이 없다). 야간 잡은 dirty 날짜를 나이와 무관하게 재집계한다.

## 월 누적 계층 (usage.monthly_aggregates)
period(월 1일)~covered_through 의 일별 롤업 합. 그 구간의 모든 날짜가 신뢰 가능
(v2·clean·같은 tz)할 때만 만든다. admin-api 는 이후 그 달의 날짜가 dirty 가 되거나
aggregated_at > built_at 이면 그 달을 월 계층에서 읽지 않는다(일별/raw 로 내려간다).
모든 쓰기(일 재집계·월 재구성)는 advisory lock 727_001 아래에서만 돈다.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from worker.config import get_settings

logger = structlog.get_logger(__name__)

#: daily_aggregate_days.grain_version — admin-api 가 이 값 이상만 신뢰한다.
GRAIN_VERSION = 2


def _day_window(tz: ZoneInfo, local_day: date) -> tuple[datetime, datetime]:
    """로컬 날짜 하루의 [start, end) UTC 구간 — DST 전환일은 23h/25h 다."""
    start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return (
        start_local.astimezone(UTC),
        end_local.astimezone(UTC),
    )


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(p: date) -> date:
    nxt = date(p.year + (p.month == 12), p.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


# ── 일 재집계 ────────────────────────────────────────────────────────────────

#: 레지스트리 행 잠금 — UPSERT 는 기존 행이면 행 잠금을, 새 행이면 삽입 잠금을 잡는다.
_LOCK_DAY_SQL = """
INSERT INTO usage.daily_aggregate_days (date) VALUES (:day)
ON CONFLICT (date) DO UPDATE SET date = EXCLUDED.date
"""

_DELETE_DAY_SQL = "DELETE FROM usage.daily_aggregates WHERE date = :day"

_AGG_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias, client, status,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count, call_count, latency_ms_sum)
SELECT
  CAST(:day AS date),
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
  SUM(latency_ms)
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :end
GROUP BY user_id, team_id, dept_id, model_alias, COALESCE(client, 'other'), status
"""

_MARK_DAY_SQL = """
UPDATE usage.daily_aggregate_days
   SET tz = :tz, grain_version = :grain, dirty = false, aggregated_at = clock_timestamp()
 WHERE date = :day
"""


async def aggregate_day(session: AsyncSession, tz_name: str, day: date) -> int:
    """하루를 새 입도로 재집계하고 레지스트리를 clean 으로 표시한다(한 트랜잭션 + commit)."""
    start, end = _day_window(ZoneInfo(tz_name), day)
    await session.execute(text(_LOCK_DAY_SQL), {"day": day})
    await session.execute(text(_DELETE_DAY_SQL), {"day": day})
    result = await session.execute(
        text(_AGG_SQL), {"day": day, "start": start, "end": end}
    )
    count: int = result.rowcount
    await session.execute(
        text(_MARK_DAY_SQL), {"day": day, "tz": tz_name, "grain": GRAIN_VERSION}
    )
    await session.commit()
    return count


# ── 월 누적 ──────────────────────────────────────────────────────────────────

#: [period, covered_through] 의 모든 날짜가 신뢰 가능한가(레지스트리 행이 있고 v2·clean·같은 tz).
_MONTH_READY_SQL = """
SELECT COALESCE(bool_and(COALESCE(
         r.grain_version >= :grain AND NOT r.dirty AND r.tz = :tz, false)), false)
FROM generate_series(CAST(:period AS date), CAST(:through AS date), interval '1 day') AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
"""

_DELETE_MONTH_SQL = "DELETE FROM usage.monthly_aggregates WHERE period = :period"

_MONTH_SQL = """
INSERT INTO usage.monthly_aggregates
  (period, user_id, team_id, dept_id, model_alias, client, status,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, call_count, request_count, latency_ms_sum)
SELECT CAST(:period AS date), user_id, team_id, dept_id, model_alias, client, status,
       SUM(input_tokens), SUM(output_tokens), SUM(cache_creation_tokens), SUM(cache_read_tokens),
       SUM(total_tokens), SUM(total_cost_usd), SUM(call_count), SUM(request_count),
       SUM(latency_ms_sum)
FROM usage.daily_aggregates
WHERE date >= :period AND date <= :through AND status IS NOT NULL
GROUP BY user_id, team_id, dept_id, model_alias, client, status
"""

_MARK_MONTH_SQL = """
INSERT INTO usage.monthly_aggregate_months (period, tz, covered_through, built_at)
VALUES (:period, :tz, :through, clock_timestamp())
ON CONFLICT (period) DO UPDATE
   SET tz = EXCLUDED.tz, covered_through = EXCLUDED.covered_through, built_at = EXCLUDED.built_at
"""


async def rebuild_month(
    session: AsyncSession, tz_name: str, period: date, today_local: date
) -> int | None:
    """period 달의 월 누적을 [period, min(월말, 어제)] 로 다시 만든다.

    그 구간에 신뢰할 수 없는 날짜가 하나라도 있으면 만들지 않고 None — admin-api 는
    그 달을 일별/raw 로 읽는다(정확하지만 느리다). 백필 CLI 가 과거를 채우면 만들어진다.
    """
    through = min(_month_end(period), today_local - timedelta(days=1))
    if through < period:
        return None  # 이번 달 1일 — 완료된 날짜가 아직 없다
    params = {"period": period, "through": through, "tz": tz_name, "grain": GRAIN_VERSION}
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
) -> int:
    """날짜들을 차례로 재집계(날짜당 commit — 중간에 죽어도 진행분이 남는다)한 뒤,
    건드린 달과 이번 달의 월 누적을 다시 만든다. 오늘 이후 날짜는 건너뛴다."""
    total = 0
    months: set[date] = {_month_start(today_local - timedelta(days=1))}
    for day in sorted(set(days)):
        if day >= today_local:
            continue
        count = await aggregate_day(session, tz_name, day)
        total += count
        months.add(_month_start(day))
        logger.info(log_event, date=str(day), timezone=tz_name, inserted=count)
    for period in sorted(months):
        await rebuild_month(session, tz_name, period, today_local)
    return total


# ── 재집계 대상 선정 ─────────────────────────────────────────────────────────

#: [from, to] 중 신뢰 불가(레지스트리 없음·v1·dirty·tz 다름) 날짜 + 나이 무관 dirty 날짜.
_UNTRUSTED_DAYS_SQL = """
SELECT g.d::date
FROM generate_series(CAST(:from_day AS date), CAST(:to_day AS date), interval '1 day') AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
WHERE r.date IS NULL OR r.grain_version < :grain OR r.dirty OR r.tz IS DISTINCT FROM :tz
UNION
SELECT date FROM usage.daily_aggregate_days WHERE dirty AND date < :today
"""


async def nightly_days(
    session: AsyncSession, tz_name: str, today_local: date, recent_days: int, lookback_days: int
) -> list[date]:
    """야간 재집계 대상: 최근 N일(항상) ∪ lookback 안의 신뢰 불가 날짜 ∪ 모든 dirty 날짜."""
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


#: 기동 백필의 시작점 — usage_logs 의 가장 오래된 날짜(로컬 타임존 기준).
_EARLIEST_DAY_SQL = """
SELECT MIN((requested_at AT TIME ZONE :tz)::date) FROM usage.usage_logs
"""

#: 롤업 행이 통째로 없는 과거 날짜(크론 실패·워커 장기 다운).
_MISSING_DATES_SQL = """
SELECT DISTINCT (requested_at AT TIME ZONE :tz)::date AS d
FROM usage.usage_logs
WHERE requested_at >= :lookback_start
  AND requested_at < :today_start
  AND (requested_at AT TIME ZONE :tz)::date NOT IN (
      SELECT DISTINCT date FROM usage.daily_aggregates
      WHERE date >= :lookback_start_date
  )
ORDER BY d
"""


async def missing_days(session: AsyncSession, tz_name: str, today_local: date) -> list[date]:
    """usage_logs 에는 있는데 롤업 행이 하나도 없는 과거 날짜(전체 이력).

    레거시 입도 행만 있는 날짜는 여기 안 잡힌다 — 그 날짜들은 gateway /usage 에 이미
    정확한 합계를 주고 있고, 새 입도로의 전환은 백필 CLI 가 통제된 속도로 한다.
    """
    tz = ZoneInfo(tz_name)
    earliest = (
        await session.execute(text(_EARLIEST_DAY_SQL), {"tz": tz_name})
    ).scalar_one_or_none()
    if earliest is None:
        return []
    start, _ = _day_window(tz, earliest)
    today_start, _ = _day_window(tz, today_local)
    rows = await session.execute(
        text(_MISSING_DATES_SQL),
        {
            "tz": tz_name,
            "lookback_start": start,
            "today_start": today_start,
            "lookback_start_date": earliest,
        },
    )
    return [r[0] for r in rows]


# ── 잠금 ─────────────────────────────────────────────────────────────────────

#: pg advisory lock 키 — 야간 잡·기동 백필·백필 CLI 가 공유한다. 일 재집계 자체는
#: 레지스트리 행 잠금으로도 안전하지만, 월 재구성의 일관성(일 재집계와 겹치지 않음)과
#: N replica 의 중복 스캔 부하를 이 락이 막는다.
_DAILY_AGG_LOCK_KEY = 727_001


@contextlib.asynccontextmanager
async def aggregation_lock(engine: AsyncEngine, wait_seconds: float = 0) -> AsyncIterator[bool]:
    """advisory lock 을 **전용 AUTOCOMMIT 커넥션**에서 잡는다.

    ⚠️ advisory lock 은 커넥션 레벨이다 — 집계 세션이 날짜마다 commit 하면 커넥션이
       풀로 돌아가 unlock 이 다른 커넥션에서 실행될 수 있다(실측: 락 고착 → 전 replica
       영구 스킵). 전용 커넥션이 닫히면 락도 풀리므로 고착 경로가 없다.
    wait_seconds>0 이면 그동안 5초 간격으로 다시 시도한다(백필 CLI).
    """
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": _DAILY_AGG_LOCK_KEY}
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
                        text("SELECT pg_advisory_unlock(:k)"), {"k": _DAILY_AGG_LOCK_KEY}
                    )
                except Exception:
                    # 락 커넥션이 끊겼으면 서버 측에서 이미 해제됐다(R23 r3 N-6).
                    logger.warning("daily_aggregator.unlock_failed")


# ── 엔트리포인트 ─────────────────────────────────────────────────────────────


def _today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


async def run_daily_aggregation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """APScheduler 가 리포팅 tz 00:10 에 호출. 최근 N일 + 신뢰 불가 + dirty 날짜를 재집계하고
    월 누적을 갱신한다. 다중 replica 에서는 lock 을 잡은 하나만 실행한다(전부 멱등)."""
    try:
        async with aggregation_lock(session_factory.kw["bind"]) as got:
            if not got:
                # 락 고착이면 이 로그가 매일 남는 유일한 신호다 — WARN.
                logger.warning("daily_aggregator.skipped_lock_held")
                return
            settings = get_settings()
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
    except Exception:
        logger.exception("daily_aggregator.failed")


async def backfill_if_empty(session: AsyncSession) -> int:
    """기동 시 롤업 행이 통째로 없는 날짜를 새 입도로 채운다(날짜당 commit, 재개 안전)."""
    tz_name = get_settings().reporting_timezone
    today_local = _today(tz_name)
    days = await missing_days(session, tz_name, today_local)
    await session.commit()
    if not days:
        return 0
    total = await recompute_days(
        session, tz_name, days, today_local=today_local,
        log_event="daily_aggregator.backfilled_missing_day",
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
    """기동 시 1회 — 야간 잡·백필 CLI 와 같은 advisory lock 아래에서만 돈다."""
    try:
        async with aggregation_lock(session_factory.kw["bind"]) as got:
            if not got:
                logger.info("daily_aggregator.startup_backfill_skipped_lock_held")
                return
            async with session_factory() as session:
                count = await backfill_if_empty(session)
                logger.info("daily_aggregator.startup_backfill_done", count=count)
    except Exception:
        logger.exception("daily_aggregator.startup_backfill_failed")
