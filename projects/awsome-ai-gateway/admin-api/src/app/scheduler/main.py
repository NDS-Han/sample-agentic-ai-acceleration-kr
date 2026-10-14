# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ROI Aggregation + VK Expiry Scheduler — separate process from Admin API server.

Run: python -m app.scheduler.main

``daily_usage_aggregation`` 잡은 cost-recorder-worker 로 이관됨
이 scheduler는 ROI/key-expiry 만 담당.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.usage_filters import current_kst_period
from app.scheduler.key_expirer import expire_virtual_keys
from app.scheduler.key_purger import purge_expired_keys
from app.scheduler.roi_aggregator import aggregate_usage

logger = structlog.get_logger()


async def _try_job_lock(session: AsyncSession, job_name: str) -> bool:
    """잡 단위 advisory lock — rolling update(구/신 pod 공존)나 실수로 늘린
    replica 에서 같은 잡이 동시에 두 번 돌지 않게 한다.

    **세션 레벨** lock — xact lock 은 첫 커밋에서 풀려 버려, 월초 재집계처럼
    잡 안에서 여러 트랜잭션을 커밋하는 경우 후반부가 무방비가 된다. 세션 락은
    커밋 후에도 유지되므로 잡 종료까지 커버한다. 커밋이 유지돼도 락은 남으므로
    finally 에서 반드시 명시 해제한다 — 안 하면 풀에 반납된 커넥션이 락을 들고
    다니는 유령 상태가 된다.
    """
    acquired = (
        await session.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
            {"k": f"scheduler-job:{job_name}"},
        )
    ).scalar()
    if not acquired:
        logger.info("scheduler.job_skipped_locked", job=job_name)
    return bool(acquired)


async def _release_job_lock(session: AsyncSession, job_name: str) -> None:
    """세션 레벨 advisory lock 명시 해제 — 같은 커넥션에서 실행해야 한다."""
    try:
        await session.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"),
            {"k": f"scheduler-job:{job_name}"},
        )
    except Exception:
        # 해제 실패해도 세션 종료(커넥션 close)가 락을 풀어준다 — 경고만.
        logger.warning("scheduler.job_lock_unlock_failed", job=job_name)


def _prev_period(period: str) -> str:
    """'YYYY-MM' 의 직전 달을 반환한다."""
    year, month = map(int, period.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


async def run_aggregation() -> None:
    # ⚠️ UTC 가 아니라 **KST** 월 — 이 잡이 ROI 행을 실제로 **쓰는** 곳이라 영향이 가장 크다.
    # 과거엔 datetime.now(timezone.utc).strftime("%Y-%m") 였다. 데이터는 KST 월로
    # 버킷되므로(§59) 매월 1일 KST 00:00~09:00 의 9시간 동안 이 잡은 지난달을 다시
    # 집계하고 **새 달 행은 아예 만들지 않는다**. 그동안 화면은 (current_kst_period 로
    # 고쳐진) 새 달을 조회하므로 roi_aggregations 에 행이 없어 전부 0 으로 보인다.
    # 즉 읽는 쪽만 KST 로 고치면 9시간짜리 "지표가 0" 창이 남는다 — 쓰는 쪽도 같아야 한다.
    period = current_kst_period()
    tz = ZoneInfo(get_settings().REPORTING_TIMEZONE)
    now_local = datetime.now(tz)

    # 매월 마지막 실행(~23:45) 이후의 말일 뒷부분 사용량과 스트림 지연분은
    # 지난달 행에 반영될 기회가 없다 — 새 달 1일 동안은 전월도 함께 재집계한다
    # (upsert 라 멱등). 2일 이후엔 current 만 집계해 월 스캔 비용을 1회로 유지.
    periods = [period]
    if now_local.day == 1:
        periods.append(_prev_period(period))
    logger.info("scheduler.trigger", period=period, periods=periods)

    async with AsyncSessionLocal() as session:
        try:
            if not await _try_job_lock(session, "roi_aggregation"):
                return
            try:
                for p in periods:
                    await aggregate_usage(session, p)
            finally:
                await _release_job_lock(session, "roi_aggregation")
        except Exception:
            logger.exception("scheduler.aggregation_failed", period=period)


async def run_key_expiry() -> None:
    async with AsyncSessionLocal() as session:
        try:
            if not await _try_job_lock(session, "key_expiry"):
                return
            try:
                await expire_virtual_keys(session)
            finally:
                await _release_job_lock(session, "key_expiry")
        except Exception:
            logger.exception("scheduler.key_expiry_failed")


async def run_key_purge() -> None:
    async with AsyncSessionLocal() as session:
        try:
            if not await _try_job_lock(session, "key_purge"):
                return
            try:
                await purge_expired_keys(
                    session, get_settings().KEY_PURGE_RETENTION_DAYS
                )
            finally:
                await _release_job_lock(session, "key_purge")
        except Exception:
            logger.exception("scheduler.key_purge_failed")


# APScheduler 기본값은 동일 잡 중첩 실행 허용 + misfire 시 무제한 재실행이다.
# 잡이 cron 간격(15분/1시간)보다 오래 걸리는 이상 상태에서 중첩 실행은 같은
# 집계/UPDATE 를 두 트랜잭션이 동시에 수행하게 하므로 max_instances=1 로 막고,
# pod 재시작 등으로 밀린 misfire 는 grace 시간 안의 것만 합쳐서(coalesce)
# 한 번 실행한다 — 다음 정기 실행이 곧 오므로 오래된 회차 재실행 가치가 없다.
_JOB_DEFAULTS = {
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 300,
}


def main() -> None:
    settings = get_settings()
    logger.info(
        "scheduler.starting",
        roi_cron=settings.ROI_AGGREGATION_CRON,
        key_expiry_cron=settings.KEY_EXPIRY_CRON,
        key_purge_cron=settings.KEY_PURGE_CRON,
        key_purge_retention_days=settings.KEY_PURGE_RETENTION_DAYS,
        key_purge_enabled=settings.KEY_PURGE_ENABLED,
        timezone=settings.REPORTING_TIMEZONE,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # cron 은 리포팅 타임존 기준으로 해석한다 — REPORTING_TIMEZONE 미지정 시
    # 컨테이너 TZ(UTC) 기준이 돼 "새벽 3시반" 잡이 KST 점심에 돌았다
    # (cost-recorder 의 AsyncIOScheduler 와 동일 규약).
    tz = settings.REPORTING_TIMEZONE
    scheduler = AsyncIOScheduler(event_loop=loop, timezone=tz)
    scheduler.add_job(
        run_aggregation,
        CronTrigger.from_crontab(settings.ROI_AGGREGATION_CRON, timezone=tz),
        id="roi_aggregation",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        run_key_expiry,
        CronTrigger.from_crontab(settings.KEY_EXPIRY_CRON, timezone=tz),
        id="key_expiry",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    if settings.KEY_PURGE_ENABLED:
        scheduler.add_job(
            run_key_purge,
            CronTrigger.from_crontab(settings.KEY_PURGE_CRON, timezone=tz),
            id="key_purge",
            replace_existing=True,
            **_JOB_DEFAULTS,
        )
    scheduler.start()

    # SIGTERM(k8s pod 종료) 은 KeyboardInterrupt 를 일으키지 않아 기존엔
    # 실행 중인 잡을 그대로 죽였다. loop.stop 을 신호 핸들러에 바로 걸면
    # AsyncIOScheduler.shutdown 이 실제 종료를 루프에 예약(call_soon)만 하고
    # 실행되지 못한 채 루프가 멈춘다 — 코루틴으로 종료를 예약한 뒤 잡이 커밋할
    # 짧은 유예를 두고 루프를 멈춘다.
    def _on_signal() -> None:
        async def _shutdown() -> None:
            logger.info("scheduler.signal_received")
            scheduler.shutdown(wait=False)
            await asyncio.sleep(5)
            loop.stop()

        asyncio.ensure_future(_shutdown(), loop=loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            pass  # 비-POSIX 플랫폼 — KeyboardInterrupt 경로에 의존

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("scheduler.shutdown")
        scheduler.shutdown()
        loop.close()


if __name__ == "__main__":
    main()
