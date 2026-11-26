# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Real Redis/Postgres proof for business-alert repeat dedupe.

The test self-provisions throwaway containers with unique names and always removes them with
``docker rm -f -v``. Redis is used as the release barrier so two independent worker handler
instances race into the same dedupe claim; PostgreSQL's real unique slot is the persistence
authority. No fake session is involved.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from worker.handlers.business_alert_handler import SLACK_RECIPIENT_SLOT, BusinessAlertHandler
from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.services.config_cache import ConfigCache
from worker.services.retry_executor import RetryExecutor

pytestmark = pytest.mark.integration

_PG_IMAGE = "pgvector/pgvector:pg16"
_REDIS_IMAGE = "redis:7.4-alpine"


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def _docker_port(name: str, container_port: str) -> int:
    out = _run([
        "docker", "inspect", "-f",
        f"{{{{(index (index .NetworkSettings.Ports \"{container_port}\") 0).HostPort}}}}",
        name,
    ]).stdout.strip()
    return int(out)


def _wait_for_container(name: str, probe: list[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "exec", name, *probe], capture_output=True, text=True)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"container {name} did not become ready")


def _wait_for_postgres_container(name: str, timeout: float = 30.0) -> None:
    """Wait for the final postgres server, not the image's temporary initdb server."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        ready_markers = (logs.stdout + logs.stderr).count(
            "database system is ready to accept connections"
        )
        probe = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "gateway", "-d", "gateway"],
            capture_output=True,
            text=True,
        )
        if ready_markers >= 2 and probe.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"container {name} did not become ready")


@pytest.fixture(scope="module")
def throwaway_pg_redis():
    suffix = uuid.uuid4().hex[:10]
    pg_name = f"bizalerts-fix-pg-{suffix}"
    redis_name = f"bizalerts-fix-redis-{suffix}"
    started: list[str] = []
    try:
        _run([
            "docker", "run", "-d", "--name", pg_name,
            "-e", "POSTGRES_USER=gateway",
            "-e", "POSTGRES_PASSWORD=gateway_dev_password",
            "-e", "POSTGRES_DB=gateway",
            "-p", "127.0.0.1::5432", _PG_IMAGE,
        ])
        started.append(pg_name)
        _run(["docker", "run", "-d", "--name", redis_name, "-p", "127.0.0.1::6379", _REDIS_IMAGE])
        started.append(redis_name)
        _wait_for_postgres_container(pg_name)
        _wait_for_container(redis_name, ["redis-cli", "ping"])
        yield {
            "pg_port": _docker_port(pg_name, "5432/tcp"),
            "redis_port": _docker_port(redis_name, "6379/tcp"),
            "suffix": suffix,
        }
    finally:
        for name in reversed(started):
            _run(["docker", "rm", "-f", "-v", name], check=False)


async def _create_notification_schema(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(text("CREATE SCHEMA IF NOT EXISTS notification"))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS notification.notification_configs (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                event_type      VARCHAR(50)  NOT NULL UNIQUE,
                recipient_roles JSONB        NOT NULL DEFAULT '[]',
                enabled         BOOLEAN      NOT NULL DEFAULT true,
                config          JSONB        NOT NULL DEFAULT '{}',
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
            )
        """))
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS notification.notification_logs (
                id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                event_id           VARCHAR(100) NOT NULL,
                event_type         VARCHAR(50)  NOT NULL,
                channel            VARCHAR(20)  NOT NULL DEFAULT 'email',
                recipient_email    VARCHAR(255) NOT NULL,
                recipient_user_id  VARCHAR(100),
                subject            VARCHAR(500) NOT NULL,
                status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
                attempt_count      INTEGER      NOT NULL DEFAULT 0,
                last_attempt_at    TIMESTAMPTZ,
                error_message      TEXT,
                event_payload      JSONB        NOT NULL,
                created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
                resolved_at        TIMESTAMPTZ
            )
        """))
        await session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_logs_event_recipient
            ON notification.notification_logs (event_id, event_type, recipient_email)
        """))
        await session.execute(text("""
            INSERT INTO notification.notification_configs
                (event_type, recipient_roles, enabled, config)
            VALUES ('pricing_missing', '[]', true, '{}')
        """))
        await session.commit()


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


class _RecordingSlackSender:
    def __init__(self) -> None:
        self.messages: list = []

    async def send(self, message) -> None:
        self.messages.append(message)


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="integration-poll-event",
        type=EventType.PRICING_MISSING,
        timestamp=datetime.now(UTC),
        source=ServiceSource.NOTIFICATION_WORKER,
        payload={
            "check": "pricing_missing",
            "period": "2026-11",
            "dedupe_key": "pricing_missing:2026-11",
            "environment": "test",
            "severity": "warning",
            "title": "Pricing missing for usage rows",
            "summary": "2 usage rows are waiting for model pricing.",
            "facts": [{"label": "Open rows", "value": "2"}],
        },
    )


async def test_concurrent_workers_have_exactly_one_send_and_repeat_is_relative_to_first_fire(
    throwaway_pg_redis,
) -> None:
    db_url = (
        "postgresql+asyncpg://gateway:gateway_dev_password@"
        f"127.0.0.1:{throwaway_pg_redis['pg_port']}/gateway"
    )
    redis_url = f"redis://127.0.0.1:{throwaway_pg_redis['redis_port']}/0"
    engine = create_async_engine(db_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    try:
        assert await redis.ping() is True
        await _create_notification_schema(session_factory)

        config_cache_1 = ConfigCache(session_factory)
        config_cache_2 = ConfigCache(session_factory)
        await asyncio.gather(config_cache_1.load(), config_cache_2.load())

        # 05:58 plus a one-hour interval intentionally crosses the fixed 06:00 epoch bucket
        # boundary. The old implementation could fire again immediately after 06:00.
        first_fire = datetime(2026, 11, 25, 5, 58, 0, tzinfo=UTC)
        clock = _MutableClock(first_fire)
        senders = [_RecordingSlackSender(), _RecordingSlackSender()]
        handlers = [
            BusinessAlertHandler(
                session_factory=session_factory,
                config_cache=cache,
                slack_sender=sender,
                retry_executor=RetryExecutor(),
                metrics=None,
                repeat_interval_seconds=3600,
                clock=clock,
            )
            for cache, sender in zip((config_cache_1, config_cache_2), senders, strict=True)
        ]
        event = _event()

        barrier_key = f"bizalerts-fix-test:{throwaway_pg_redis['suffix']}:release"
        async def handle_when_released(handler):
            while not await redis.get(barrier_key):
                await asyncio.sleep(0.005)
            await handler.handle(event)

        first_attempts = [asyncio.create_task(handle_when_released(h)) for h in handlers]
        await asyncio.sleep(0.05)
        await redis.set(barrier_key, "1")
        await asyncio.gather(*first_attempts)

        assert sum(len(s.messages) for s in senders) == 1
        async with session_factory() as session:
            rows = (await session.execute(text("""
                SELECT event_id, status FROM notification.notification_logs
                WHERE event_type='pricing_missing' AND recipient_email=:recipient
                ORDER BY created_at
            """), {"recipient": SLACK_RECIPIENT_SLOT})).all()
        assert len(rows) == 1
        assert rows[0].status == "sent"

        # t + interval - epsilon: across the fixed epoch boundary, but still inside the
        # first-fire-relative repeat interval — neither worker may send.
        clock.set(first_fire + timedelta(seconds=3600 - 1))
        await asyncio.gather(*(h.handle(event) for h in handlers))
        assert sum(len(s.messages) for s in senders) == 1
        async with session_factory() as session:
            count_before = (await session.execute(text(
                "SELECT count(*) FROM notification.notification_logs "
                "WHERE event_type='pricing_missing'"
            ))).scalar_one()
        assert count_before == 1

        # At exactly t + interval the same active condition gets one new slot and exactly
        # one worker wins the concurrent claim.
        clock.set(first_fire + timedelta(seconds=3600))
        await asyncio.gather(*(h.handle(event) for h in handlers))
        assert sum(len(s.messages) for s in senders) == 2
        async with session_factory() as session:
            rows = (await session.execute(text("""
                SELECT event_id, status FROM notification.notification_logs
                WHERE event_type='pricing_missing' AND recipient_email=:recipient
                ORDER BY event_id
            """), {"recipient": SLACK_RECIPIENT_SLOT})).all()
        assert len(rows) == 2
        assert len({r.event_id for r in rows}) == 2
        assert all(r.status == "sent" for r in rows)
    finally:
        await redis.aclose()
        await engine.dispose()
