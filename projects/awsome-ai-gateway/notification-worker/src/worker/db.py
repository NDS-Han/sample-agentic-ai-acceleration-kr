# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from collections.abc import AsyncGenerator

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

    pool_kwargs: dict = {}
    # 명시 override 일 때만 — 미설정이면 SQLAlchemy 기본(30s, recycle 없음) 유지.
    if settings.worker_db_pool_timeout is not None:
        pool_kwargs["pool_timeout"] = settings.worker_db_pool_timeout
    if settings.worker_db_pool_recycle is not None:
        pool_kwargs["pool_recycle"] = settings.worker_db_pool_recycle

    return create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.effective_pool_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
        **pool_kwargs,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


# Module singleton
_session_factory: async_sessionmaker[AsyncSession] | None = None


def set_db_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    global _session_factory
    _session_factory = factory


def get_db_session_factory() -> async_sessionmaker[AsyncSession]:
    assert _session_factory is not None, "DB session factory not initialised. Call set_db_session_factory() first."
    return _session_factory
