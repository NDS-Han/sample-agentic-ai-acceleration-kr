# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from worker.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict = {}
    if settings.db_ssl_mode != "disable":
        connect_args["ssl"] = settings.db_ssl_mode
    connect_args["statement_cache_size"] = settings.db_statement_cache_size
    # 블랙홀 장애(무응답)에서 asyncpg 기본값(connect 60s·명령 무제한) 대기를 막는다.
    connect_args["timeout"] = settings.db_connect_timeout
    connect_args["command_timeout"] = settings.db_command_timeout

    return create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_aggregator_engine(settings: Settings) -> AsyncEngine:
    """롤업 집계 전용 엔진 — flush 엔진(command_timeout 60s)과 풀·타임아웃을 나눈다.

    REPEATABLE READ: 한 날짜/시간 재집계 트랜잭션의 집계 SELECT 와 dirty 표시 DELETE 가 같은
    스냅샷을 봐야 한다(daily_aggregator 모듈 주석). advisory lock 커넥션은 AUTOCOMMIT 으로
    따로 연다.
    """
    connect_args: dict = {}
    if settings.db_ssl_mode != "disable":
        connect_args["ssl"] = settings.db_ssl_mode
    connect_args["statement_cache_size"] = settings.db_statement_cache_size
    connect_args["timeout"] = settings.db_connect_timeout
    connect_args["command_timeout"] = settings.daily_usage_agg_command_timeout
    return create_async_engine(
        settings.db_url,
        pool_size=settings.daily_usage_agg_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        isolation_level="REPEATABLE READ",
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
