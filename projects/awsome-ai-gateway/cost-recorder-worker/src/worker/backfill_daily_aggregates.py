# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""usage.daily_aggregates v2 백필 CLI — 과거 날짜를 새 입도로 다시 집계하고 월 누적을 만든다.

    python -m worker.backfill_daily_aggregates [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                                               [--force] [--wait-lock SEC] [--dry-run]

  --from    기본: usage_logs 최초 요청이 속한 **달의 1일**(월 누적이 그 달을 쓸 수 있게
            빈 날짜에도 레지스트리 행을 남긴다)
  --to      기본: 어제(리포팅 tz). 오늘 이후는 항상 제외(오늘은 admin-api 가 raw 로 읽는다)
  --force   이미 v2·clean·같은 tz 인 날짜도 다시 집계(기본은 건너뛴다)
  --wait-lock  야간 잡·다른 백필이 advisory lock 을 쥐고 있으면 기다릴 초(기본 600)

재개 가능: 날짜마다 commit 하고, 다시 돌리면 끝난 날짜(v2·clean·같은 tz)를 건너뛴다.
워커와 같은 env(DB_URL·REPORTING_TIMEZONE 등)를 읽는다 — 워커 pod 안에서 실행한다.
종료 코드: 0 성공 · 2 lock 을 못 잡음 · 1 오류.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta

import structlog
from sqlalchemy import text

from worker import daily_aggregator as da
from worker.config import get_settings
from worker.db import create_db_engine, create_session_factory

logger = structlog.get_logger(__name__)

#: [from, to] 에서 다시 집계할 날짜 — force 가 아니면 신뢰 가능한 날짜는 뺀다.
_TODO_SQL = """
SELECT g.d::date
FROM generate_series(CAST(:from_day AS date), CAST(:to_day AS date), interval '1 day') AS g(d)
LEFT JOIN usage.daily_aggregate_days r ON r.date = g.d::date
WHERE CAST(:force AS boolean) OR r.date IS NULL OR r.grain_version < :grain OR r.dirty
      OR r.tz IS DISTINCT FROM :tz
ORDER BY 1
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m worker.backfill_daily_aggregates")
    p.add_argument("--from", dest="from_day", type=date.fromisoformat)
    p.add_argument("--to", dest="to_day", type=date.fromisoformat)
    p.add_argument("--force", action="store_true")
    p.add_argument("--wait-lock", type=float, default=600.0)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


async def run(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    tz_name = settings.reporting_timezone
    engine = None
    if session_factory is None:
        engine = create_db_engine(settings)
        session_factory = create_session_factory(engine)
    try:
        today = da._today(tz_name)
        async with session_factory() as session:
            from_day = args.from_day
            if from_day is None:
                earliest = (
                    await session.execute(text(da._EARLIEST_DAY_SQL), {"tz": tz_name})
                ).scalar_one_or_none()
                if earliest is None:
                    print("usage_logs is empty — nothing to backfill")
                    return 0
                from_day = earliest.replace(day=1)
            to_day = min(args.to_day or today - timedelta(days=1), today - timedelta(days=1))
            if to_day < from_day:
                print(f"empty range {from_day}..{to_day}")
                return 0
            todo = [
                r[0]
                for r in await session.execute(
                    text(_TODO_SQL),
                    {"from_day": from_day, "to_day": to_day, "force": args.force,
                     "grain": da.GRAIN_VERSION, "tz": tz_name},
                )
            ]
            await session.commit()
        total_days = (to_day - from_day).days + 1
        print(f"timezone={tz_name} range={from_day}..{to_day} days={total_days} "
              f"to_aggregate={len(todo)} skipped_done={total_days - len(todo)}")
        if args.dry_run:
            return 0

        async with da.aggregation_lock(session_factory.kw["bind"], args.wait_lock) as got:
            if not got:
                print("could not take the aggregation lock (nightly job or another backfill "
                      "is running) — rerun later", file=sys.stderr)
                return 2
            started = time.monotonic()
            months: set[date] = {(today - timedelta(days=1)).replace(day=1)}
            async with session_factory() as session:
                for i, day in enumerate(todo, 1):
                    t0 = time.monotonic()
                    rows = await da.aggregate_day(session, tz_name, day)
                    months.add(day.replace(day=1))
                    print(f"[{i}/{len(todo)}] {day} rows={rows} "
                          f"{time.monotonic() - t0:.1f}s", flush=True)
                for period in sorted(months):
                    built = await da.rebuild_month(session, tz_name, period, today)
                    state = (f"rebuilt rows={built}" if built is not None
                             else "skipped (untrusted or no completed day)")
                    print(f"month {period:%Y-%m}: {state}", flush=True)
            print(f"done in {time.monotonic() - started:.1f}s")
        return 0
    finally:
        if engine is not None:
            await engine.dispose()


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except SystemExit:
        raise
    except Exception:
        logger.exception("backfill_daily_aggregates.failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
