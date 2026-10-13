# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""defer_redis_write_until_commit — 커밋 후에만 Redis SET 이 실행되는지 검증.

§6-6: 커밋 전 SET 은 롤백 시 미커밋 예산 설정을 TTL 동안 광고한다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.budget_cache import defer_redis_write_until_commit


def _session_double() -> AsyncMock:
    """AsyncSession 처럼 보이지만 실제 이벤트는 sync Session 이 발행하는 double."""
    sync_session = Session(create_engine("sqlite://"))
    session = AsyncMock()
    session.sync_session = sync_session
    return session


async def test_write_fires_only_after_commit():
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    await defer_redis_write_until_commit(session, write)
    assert calls == []  # 등록만 되고 실행되지 않는다

    session.sync_session.commit()
    await asyncio.sleep(0)  # create_task 된 쓰기가 돌 기회를 준다
    assert calls == ["ran"]


async def test_write_discarded_on_rollback():
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    await defer_redis_write_until_commit(session, write)
    # after_rollback 은 실제 트랜잭션이 시작된 뒤에만 발행된다 — begin 시킨다.
    session.sync_session.execute(text("SELECT 1"))
    session.sync_session.rollback()
    # 롤백 후 같은 세션이 다른 작업으로 커밋돼도 폐기된 쓰기는 실행되지 않는다.
    session.sync_session.commit()
    await asyncio.sleep(0)
    assert calls == []


async def test_mock_session_runs_immediately():
    """이벤트를 지원하지 않는 세션(일반 mock)은 즉시 실행 — 기존 동작 유지."""
    session = AsyncMock()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    await defer_redis_write_until_commit(session, write)
    assert calls == ["ran"]


async def test_write_survives_savepoint_lifecycle():
    """after_commit 은 SAVEPOINT release 에도 발화한다 — 지연 쓰기가 nested
    commit 에서 조기 실행되면 outer commit 전에 Redis 가 새어나간다(R3-8 부류).
    nested release/rollback 에는 발화하지 않고 outer commit 에서만 실행돼야 한다."""
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    sync = session.sync_session
    sync.execute(text("SELECT 1"))  # 트랜잭션 오픈
    await defer_redis_write_until_commit(session, write)

    # savepoint release — after_commit 이 울려도 쓰기는 실행되면 안 된다.
    with sync.begin_nested():
        sync.execute(text("SELECT 2"))
    await asyncio.sleep(0)
    assert calls == []

    # savepoint rollback — after_rollback 이 울려도 쓰기는 살아 있어야 한다.
    try:
        with sync.begin_nested():
            sync.execute(text("SELECT bogus FROM nonexistent"))
    except Exception:
        pass
    sync.commit()
    await asyncio.sleep(0)
    assert calls == ["ran"]


async def test_write_inside_rolled_back_savepoint_is_discarded():
    """savepoint 안에서 등록된 지연 쓰기는 그 savepoint 가 롤백되면 폐기된다.

    안 그러면 롤백된 DB 변경(per-user savepoint 안의 VK 폐기 실패 등)의
    Redis DEL 이 외부 커밋 때 실행돼 DB 는 ACTIVE 인데 캐시는 지워지는
    정합 붕괴가 생긴다."""
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    sync = session.sync_session
    sync.execute(text("SELECT 1"))  # 외부 트랜잭션 오픈
    nested = sync.begin_nested()
    await defer_redis_write_until_commit(session, write)
    nested.rollback()  # savepoint 만 롤백 — 외부 트랜잭션은 계속
    sync.commit()
    await asyncio.sleep(0)
    assert calls == []


async def test_write_inside_released_savepoint_runs_on_outer_commit():
    """savepoint 안에서 등록 → savepoint release → 외부 commit 이면 실행된다.

    release 시점엔 실행되면 안 되고(외부 롤백 가능성), 외부 commit 이후엔
    반드시 실행돼야 한다 — per-user savepoint 안의 정상 폐기 분."""
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    sync = session.sync_session
    sync.execute(text("SELECT 1"))
    nested = sync.begin_nested()
    await defer_redis_write_until_commit(session, write)
    nested.commit()  # RELEASE savepoint — 아직 외부 트랜잭션은 열려 있다
    await asyncio.sleep(0)
    assert calls == []
    sync.commit()
    await asyncio.sleep(0)
    assert calls == ["ran"]


async def test_write_survives_unrelated_savepoint_rollback():
    """등록 후 *다른* savepoint 가 롤백돼도 바깥에서 등록한 쓰기는 산다.

    위 폐기 테스트와 짝 — soft-rollback 폐기가 지연 쓰기 전체를 죽이는
    과잉 반응이 아니라 등록 스코프 기준인지 확인한다."""
    session = _session_double()
    calls: list[str] = []

    async def write() -> None:
        calls.append("ran")

    sync = session.sync_session
    sync.execute(text("SELECT 1"))
    await defer_redis_write_until_commit(session, write)  # 외부 스코프 등록
    nested = sync.begin_nested()
    nested.rollback()  # 관련 없는 savepoint 롤백
    sync.commit()
    await asyncio.sleep(0)
    assert calls == ["ran"]


async def test_write_failure_is_swallowed():
    """지연 쓰기 실패는 로그만 남기고 전파되지 않는다(best-effort warmer)."""
    session = _session_double()

    async def write() -> None:
        raise RuntimeError("redis down")

    await defer_redis_write_until_commit(session, write)
    session.sync_session.commit()
    await asyncio.sleep(0)  # 예외가 task 안에서 삼켜져야 한다
