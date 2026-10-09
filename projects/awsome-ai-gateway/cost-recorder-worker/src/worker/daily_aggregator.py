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


def _yesterday_window(tz_name: str) -> tuple[datetime, datetime, date]:
    """[어제 00:00, 오늘 00:00) UTC 구간 + 어제 로컬 날짜를 반환."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    yesterday_local_date = (now_local - timedelta(days=1)).date()
    start_local = datetime.combine(yesterday_local_date, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        yesterday_local_date,
    )


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


async def aggregate_yesterday(session: AsyncSession) -> int:
    tz_name = get_settings().reporting_timezone
    start, end, day_local = _yesterday_window(tz_name)
    # 어제 윈도우 전체를 재집계해 UPSERT — 늦게 도착한 행도 재실행 때마다 반영되고,
    # 멀티-replica 크론 동시 실행도 유니크 키 행 잠금으로 직렬화돼 안전하다.
    result = await session.execute(text(_AGG_SQL), {"start": start, "end": end, "tz": tz_name})
    count: int = result.rowcount
    await session.commit()
    logger.info(
        "daily_aggregator.ran",
        start=start.isoformat(),
        end=end.isoformat(),
        timezone=tz_name,
        inserted=count,
    )
    return count


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
    """APScheduler가 매일 호출하는 엔트리포인트."""
    try:
        async with session_factory() as session:
            await aggregate_yesterday(session)
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
