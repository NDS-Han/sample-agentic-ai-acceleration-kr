# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Daily Usage Aggregator — usage_logs → usage.daily_aggregates 일일 roll-up.

admin-api/scheduler/daily_usage_aggregator.py 로부터 이관 (2026-04-21).
이관 이유: cost-recorder-worker 가 usage_logs 쓰기를 소유하므로, 집계 읽기도
같은 프로세스가 담당하여 usage_logs 관련 작업을 단일 서비스에 집중.

Granularity: (date, user_id, model_alias) per row — date 는 settings.reporting_timezone
(기본 "Asia/Seoul"/KST) 기준 캘린더 날짜. admin-api 의 REPORTING_TIMEZONE 과 같은
값으로 맞춰야 대시보드(usage_logs 실시간 집계)와 이 테이블의 날짜 경계가 일치한다.

Idempotent: 어제 윈도우를 **전체 재집계 후 ON CONFLICT DO UPDATE**로 덮어쓴다 —
예전엔 DO NOTHING 이라 크론(KST 00:10) 이후 스트림 백로그/재처리로 늦게 INSERT 된
어제분 usage_logs 가 재실행돼도 합산되지 않아 영구 누락됐다. DO UPDATE 덮어쓰기로
늦게 온 행도 반영된다.

⚠️ 멀티-replica 동시 실행 안전: DELETE+INSERT 로는 N개 pod 의 동일 크론이 같은
   윈도우를 지우고 넣다가 유니크 키에 깨졌다(N-1 pod 매일 UniqueViolation).
   UPSERT 는 같은 키에 대해 행 잠금으로 직렬화되고, 양쪽이 결정적으로 같은 값을
   계산하므로 마지막 커밋이 이겨도 결과가 같다. usage_logs 행은 삭제되지 않으므로
   DELETE 없이도 집계가 그룹을 놓치지 않는다.

⚠️ GROUP BY 는 충돌키 (date, user_id, model_alias) 와 같은 입도로 맞춘다 —
   team_id/dept_id 를 GROUP BY 에 넣으면 팀 이동한 유저가 같은 날 같은 모델로
   두 그룹을 만들어 유니크 키에 자체 충돌한다(한 행이 조용히 유실). 대신 그 날
   **마지막 요청 시점의** team/dept 를 귀속시킨다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import get_settings

logger = structlog.get_logger(__name__)


def _day_window(tz: ZoneInfo, local_day: date) -> tuple[datetime, datetime]:
    """로컬 날짜 하루의 [start, end) UTC 구간."""
    start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


def _yesterday_window(tz_name: str) -> tuple[datetime, datetime, date]:
    """[어제 00:00, 오늘 00:00) UTC 구간 + 어제 로컬 날짜를 반환."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    yesterday_local_date = (now_local - timedelta(days=1)).date()
    start, end = _day_window(tz, yesterday_local_date)
    return (start, end, yesterday_local_date)


_AGG_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count)
SELECT
  DATE((requested_at AT TIME ZONE :tz)::timestamp) AS date,
  user_id,
  (array_agg(team_id ORDER BY requested_at DESC))[1],
  (array_agg(dept_id ORDER BY requested_at DESC))[1],
  model_alias,
  SUM(input_tokens),
  SUM(output_tokens),
  SUM(cache_creation_tokens),
  SUM(cache_read_tokens),
  SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens),
  SUM(cost_usd),
  COUNT(*)
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :end
GROUP BY
  DATE((requested_at AT TIME ZONE :tz)::timestamp),
  user_id, model_alias
ON CONFLICT (date, user_id, model_alias) DO UPDATE SET
  team_id = EXCLUDED.team_id,
  dept_id = EXCLUDED.dept_id,
  input_tokens = EXCLUDED.input_tokens,
  output_tokens = EXCLUDED.output_tokens,
  cache_creation_tokens = EXCLUDED.cache_creation_tokens,
  cache_read_tokens = EXCLUDED.cache_read_tokens,
  total_tokens = EXCLUDED.total_tokens,
  total_cost_usd = EXCLUDED.total_cost_usd,
  request_count = EXCLUDED.request_count;
"""


_BACKFILL_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count)
SELECT
  DATE((requested_at AT TIME ZONE :tz)::timestamp) AS date,
  user_id,
  (array_agg(team_id ORDER BY requested_at DESC))[1],
  (array_agg(dept_id ORDER BY requested_at DESC))[1],
  model_alias,
  SUM(input_tokens),
  SUM(output_tokens),
  SUM(cache_creation_tokens),
  SUM(cache_read_tokens),
  SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens),
  SUM(cost_usd),
  COUNT(*)
FROM usage.usage_logs
WHERE (requested_at AT TIME ZONE :tz)::date < :today_local
GROUP BY
  DATE((requested_at AT TIME ZONE :tz)::timestamp),
  user_id, model_alias
ON CONFLICT (date, user_id, model_alias) DO NOTHING;
"""


async def _is_empty(session: AsyncSession) -> bool:
    result = await session.execute(text("SELECT 1 FROM usage.daily_aggregates LIMIT 1"))
    return result.scalar_one_or_none() is None


async def _aggregate_window(
    session: AsyncSession, start: datetime, end: datetime, tz_name: str
) -> int:
    """[start, end) UTC 구간 전체 재집계 + UPSERT + commit."""
    result = await session.execute(
        text(_AGG_SQL), {"start": start, "end": end, "tz": tz_name}
    )
    count: int = result.rowcount
    await session.commit()
    return count


async def aggregate_yesterday(session: AsyncSession) -> int:
    tz_name = get_settings().reporting_timezone
    start, end, day_local = _yesterday_window(tz_name)
    # 어제 윈도우 전체를 재집계해 UPSERT — 늦게 도착한 행도 재실행 때마다 반영되고,
    # 멀티-replica 크론 동시 실행도 유니크 키 행 잠금으로 직렬화돼 안전하다.
    count = await _aggregate_window(session, start, end, tz_name)
    logger.info(
        "daily_aggregator.ran",
        start=start.isoformat(),
        end=end.isoformat(),
        timezone=tz_name,
        inserted=count,
    )
    return count


#: daily_aggregates 행이 통째로 없는 과거 날짜 탐지 — 크론 실행이 실패하면
#: 다음 실행은 어제만 집계하므로 그 날짜는 영구 누락된다. lookback 창 안에서
#: usage_logs 에는 있는데 daily_aggregates 에는 없는 날짜를 찾아 재집계한다.
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


async def backfill_missing_days(
    session: AsyncSession, lookback_days: int = 30
) -> int:
    """집계가 통째로 누락된 과거 날짜(크론 실패·워커 장기 다운)를 재집계한다.

    어제 윈도우만 매일 돌기 때문에 실패한 실행의 날짜는 그대로 두면 영구 누락이다.
    기존 키의 늦도착 행은 이 경로가 아니라 `run_daily_aggregation` 의 최근 N일
    재집계(UPSERT)가 잡는다 — 여기서는 "날짜 행 자체가 없는" 갭만 채운다.
    """
    tz_name = get_settings().reporting_timezone
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz).date()
    lookback_start_local = today_local - timedelta(days=lookback_days)
    lookback_start, today_start = (
        datetime.combine(d, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        for d in (lookback_start_local, today_local)
    )
    rows = await session.execute(
        text(_MISSING_DATES_SQL),
        {
            "tz": tz_name,
            "lookback_start": lookback_start,
            "today_start": today_start,
            "lookback_start_date": lookback_start_local,
        },
    )
    missing = [r[0] for r in rows]
    total = 0
    for d in missing:
        start, end = _day_window(tz, d)
        count = await _aggregate_window(session, start, end, tz_name)
        total += count
        logger.info(
            "daily_aggregator.backfilled_missing_day",
            date=str(d),
            inserted=count,
        )
    if missing:
        logger.warning(
            "daily_aggregator.gap_days_recovered",
            days=[str(d) for d in missing],
            inserted=total,
        )
    return total


async def backfill_if_empty(session: AsyncSession) -> int:
    """첫 기동 시 daily_aggregates 비어있으면 전체 과거 집계. 이미 값 있으면 -1."""
    if not await _is_empty(session):
        return -1
    tz_name = get_settings().reporting_timezone
    now_local_date: date = datetime.now(ZoneInfo(tz_name)).date()
    result = await session.execute(
        text(_BACKFILL_SQL), {"today_local": now_local_date, "tz": tz_name}
    )
    count: int = result.rowcount
    await session.commit()
    logger.info(
        "daily_aggregator.backfilled",
        inserted=count,
        cutoff=now_local_date.isoformat(),
        timezone=tz_name,
    )
    return count


async def run_daily_aggregation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """APScheduler가 매일 호출하는 엔트리포인트.

    단계:
      1. 어제 윈도우 UPSERT (기본 경로)
      2. 최근 재집계 창 — 스트림 백로그/재처리로 늦게 INSERT 된 그제~어제 행이
         기존 집계 키에 영구 누락되지 않도록 어제+그제를 재집계한다(UPSERT 라
         멱등, 비용은 하루분 추가 스캔).
      3. 통째로 누락된 과거 날짜 백필 — 크론 실패분 복구.

    한 단계가 실패해도 다음 스케줄 실행이 전체를 다시 시도한다(모두 멱등).
    """
    try:
        async with session_factory() as session:
            settings = get_settings()
            tz_name = settings.reporting_timezone
            tz = ZoneInfo(tz_name)
            today_local = datetime.now(tz).date()
            for offset in range(settings.daily_usage_agg_recent_days, 0, -1):
                day = today_local - timedelta(days=offset)
                start, end = _day_window(tz, day)
                count = await _aggregate_window(session, start, end, tz_name)
                logger.info(
                    "daily_aggregator.ran",
                    date=str(day),
                    timezone=tz_name,
                    inserted=count,
                )
            await backfill_missing_days(
                session, settings.daily_usage_agg_backfill_lookback_days
            )
    except Exception:
        logger.exception("daily_aggregator.failed")


async def run_startup_backfill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """기동 시 1회 backfill. 이미 데이터 있으면 no-op."""
    try:
        async with session_factory() as session:
            count = await backfill_if_empty(session)
            if count >= 0:
                logger.info("daily_aggregator.startup_backfill_done", count=count)
            else:
                logger.info("daily_aggregator.startup_backfill_skipped")
    except Exception:
        logger.exception("daily_aggregator.startup_backfill_failed")
