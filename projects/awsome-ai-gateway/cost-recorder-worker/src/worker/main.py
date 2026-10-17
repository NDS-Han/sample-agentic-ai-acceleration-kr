# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Cost Recorder Worker 진입점.

초기화 순서:
  Settings → OTel → Redis → DB → Instrumentation → Metrics →
  BatchFlusher → StreamConsumer → APScheduler(daily_aggregator) → TaskSupervisor

종료 순서 (SIGTERM/SIGINT):
  Supervisor.stop_all(grace) → APScheduler.shutdown → Redis.aclose →
  DB.dispose → OTel.shutdown
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from opentelemetry.metrics import Observation
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker.batch_flusher import BatchFlusher
from worker.config import get_settings
from worker.daily_aggregator import run_daily_aggregation, run_startup_backfill
from worker.db import create_db_engine, create_session_factory
from worker.observability import WorkerMetrics, init_otel, shutdown_otel
from worker.redis_client import create_redis_client
from worker.stream_consumer import StreamConsumer
from worker.worker import TaskSupervisor

logger = structlog.get_logger(__name__)

# fire-and-forget 백그라운드 태스크 강한 참조 — bare create_task 결과는
# 약한 참조만 남아 GC 되면 조용히 죽는다.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _configure_logging(log_level: str, log_format: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    final_processors = (
        [*shared_processors, structlog.processors.JSONRenderer()]
        if log_format == "json"
        else [*shared_processors, structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=final_processors,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)

    logger.info("worker_starting", service=settings.otel_service_name)

    # 1. OTel
    init_otel(settings)

    # 2. Redis
    redis = await create_redis_client(settings)

    # 3. DB
    db_engine = create_db_engine(settings)
    session_factory = create_session_factory(db_engine)

    # 4. Auto-instrumentation (best-effort)
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        RedisInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
    except Exception:
        logger.warning("otel_instrumentation_unavailable")

    # 5. Metrics
    metrics = WorkerMetrics()

    # 6. BatchFlusher + StreamConsumer
    flusher = BatchFlusher(session_factory=session_factory, redis=redis, metrics=metrics)
    consumer = StreamConsumer(
        redis=redis, flusher=flusher, settings=settings, metrics=metrics
    )
    # Observable gauge 콜백 — StreamConsumer._sample_lag 이 reclaim 주기마다 갱신.
    # lag 상승 = usage_logs/예산 카운터가 뒤처지는 중 (워커 장애·DB 다운의 신호).
    metrics.register_stream_lag_callback(
        lambda _opts: [Observation(consumer.last_lag)]
    )
    metrics.register_stream_pending_callback(
        lambda _opts: [Observation(consumer.last_pending)]
    )

    # 7. APScheduler — daily aggregator cron + startup backfill
    scheduler = AsyncIOScheduler(timezone=settings.reporting_timezone)
    scheduler.add_job(
        run_daily_aggregation,
        CronTrigger.from_crontab(settings.daily_usage_agg_cron, timezone=settings.reporting_timezone),
        id="daily_usage_aggregation",
        args=[session_factory],
        replace_existing=True,
        # 기본 misfire grace 는 1s — 00:10 실행이 조금이라도 늦으면 그날 집계가
        # 통째로 빠진다. 1시간 유예로 늦은 실행도 살린다(작업 자체는 멱등).
        misfire_grace_time=3600,
    )
    scheduler.start()
    # Backfill은 비차단으로 이벤트 루프에 스케줄 — startup 블로킹 금지.
    # bare create_task 는 약한 참조라 GC 되면 backfill 이 조용히 죽으므로 강한 참조 유지.
    _BACKGROUND_TASKS.add(t := asyncio.create_task(run_startup_backfill(session_factory)))
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info(
        "daily_aggregator_scheduled",
        cron=settings.daily_usage_agg_cron,
        timezone=settings.reporting_timezone,
    )

    # 8. TaskSupervisor
    supervisor = TaskSupervisor()
    supervisor.register("stream_consumer", consumer.run)

    # 9. Signal handlers
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, OSError):
            pass

    # 10. Start
    await supervisor.start_all()
    logger.info("worker_started")

    # 11. Wait for shutdown
    await stop_event.wait()
    logger.info("worker_shutting_down")

    # 12. Graceful shutdown — 먼저 소비 루프에 협조 종료를 요청해 진행 중 배치의
    # flush+XACK 를 끝낸다. 즉시 cancel 하면 커밋~XACK 사이에 잘린 배치가
    # 재전달돼 전량 replay 스킵 → 그 배치의 일별 카운터와 임계 알림(비멱등)이
    # 재시도 수단 없이 영구 생략된다(R23 S1). 루프가 안 멈추면 grace 만료 후
    # supervisor 가 cancel 한다.
    consumer.request_stop()
    # 협조 창 = 배치 간격 + flush/XACK 여유 — 이 안에 멈추면 배치가 깨끗이
    # 커밋·ACK 된다. 넘기면 supervisor 가 cancel 한다.
    await supervisor.stop_all(
        grace_period=settings.shutdown_grace_period_sec,
        cooperative_window=min(
            settings.batch_max_interval_sec + 15.0,
            settings.shutdown_grace_period_sec,
        ),
    )

    scheduler.shutdown(wait=False)
    await redis.aclose()
    await db_engine.dispose()
    await shutdown_otel()

    logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
