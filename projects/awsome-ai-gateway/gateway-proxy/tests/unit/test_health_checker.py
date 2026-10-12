# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""HealthChecker 동시 체크 검증 (deepdive Q50 Phase 3).

DB·Redis 체크를 asyncio.gather 로 동시 실행하도록 바꿨다(과거 순차 → 최대 6s 직렬).
한 틱이 둘 다 보고하는지, 한쪽 예외가 다른쪽 보고를 막지 않는지 잠근다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.degradation.manager import DegradationManager
from app.services.health_checker import HealthChecker


def _fake_db_engine(ok: bool):
    """SELECT 1 이 성공/실패하는 가짜 AsyncEngine.connect() 컨텍스트."""
    engine = MagicMock()

    @asynccontextmanager
    async def _connect():
        conn = MagicMock()
        if ok:
            conn.execute = AsyncMock(return_value=None)
        else:
            conn.execute = AsyncMock(side_effect=Exception("db down"))
        yield conn

    engine.connect = _connect
    return engine


@pytest.mark.asyncio
async def test_single_tick_reports_both_db_and_redis():
    dm = DegradationManager()
    dm.report_db_health = MagicMock()
    dm.report_redis_health = MagicMock()

    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    hc = HealthChecker(redis, _fake_db_engine(ok=True), dm)

    # 한 틱만 수동 실행(루프 없이 _check_* 동시 호출).
    import asyncio

    await asyncio.gather(hc._check_db(), hc._check_redis())

    dm.report_db_health.assert_called_once_with(healthy=True)
    dm.report_redis_health.assert_called_once_with(healthy=True)


@pytest.mark.asyncio
async def test_redis_failure_does_not_block_db_report():
    dm = DegradationManager()
    dm.report_db_health = MagicMock()
    dm.report_redis_health = MagicMock()

    redis = MagicMock()
    redis.ping = AsyncMock(side_effect=Exception("redis down"))
    hc = HealthChecker(redis, _fake_db_engine(ok=True), dm)

    import asyncio

    await asyncio.gather(hc._check_db(), hc._check_redis(), return_exceptions=True)

    # DB 는 정상 보고, Redis 는 실패 보고 — 한쪽 예외가 다른쪽을 막지 않는다.
    dm.report_db_health.assert_called_once_with(healthy=True)
    dm.report_redis_health.assert_called_once_with(healthy=False)


@pytest.mark.asyncio
async def test_spool_drain_has_time_bound():
    """flapping Redis 에서 drain 이 hang 해도 체커 틱이 멈추면 안 된다.

    drain() 은 XADD 를 순차 실행한다 — 각 호출이 socket_timeout+retry(≈4s)까지
    블록할 수 있어, 10k 스풀은 사실상 무한. 이 태스크가 멈추면 degradation 전이
    자체가 동결되므로 wait_for 상한을 둔다. drain() 은 취소안전(미완료 항목
    복원)이므로 타임아웃으로 끊어도 유실은 없다.
    """
    import asyncio

    dm = DegradationManager()
    dm.report_redis_health = MagicMock()

    class _HangingSpool:
        size = 5

        async def drain(self, _redis):
            await asyncio.sleep(3600)  # flapping Redis 에서 XADD 가 hang 하는 모사

    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    hc = HealthChecker(
        redis, _fake_db_engine(ok=True), dm, cost_stream_spool=_HangingSpool()
    )

    # wait_for 상한(10s)은 테스트에서 기다리기엔 길다 — wait_for 자체를
    # 짧은 상한으로 패치해 계약(상한 존재)을 검증한다.
    real_wait_for = asyncio.wait_for

    async def _short(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=min(timeout, 0.1))

    orig = asyncio.wait_for
    asyncio.wait_for = _short
    try:
        # outer 계측은 패치 전의 실제 wait_for 를 쓴다 — asyncio.wait_for 를
        # 호출 시점 조회하면 패치된 _short 가 외부 타임아웃까지 줄인다.
        await real_wait_for(hc._check_redis(), timeout=5.0)
    finally:
        asyncio.wait_for = orig

    # Redis 정상 보고까지 도달해야 한다 — drain hang 이 틱을 데려가지 않는다.
    dm.report_redis_health.assert_called_once_with(healthy=True)


@pytest.mark.asyncio
async def test_redis_recovery_invalidates_stale_budget_counters():
    """REDIS_DEGRADED 복구 전이 시 usage 카운터를 무효화해 lazy rebuild 시킨다.

    열화 중엔 budget_deduct(INCRBYFLOAT)가 스킵돼 Redis 카운터가 budget_usages
    보다 얇다. 키는 "존재"하므로 exists() 재수화가 절대 안 돌고 열화 중 사용분이
    영구 누락된다 — 복구 전이에 카운터를 지워 DB 진실로 재구축시킨다.
    """
    import asyncio

    dm = DegradationManager()  # 실제 상태머신 — 전이를 실제로 태운다.

    # Redis 강제 강등: FAIL_THRESHOLD(5) 연속 실패.
    for _ in range(5):
        dm.report_redis_health(False)
    from app.schemas.domain import DegradationLevel

    assert dm.level == DegradationLevel.REDIS_DEGRADED

    class _Redis:
        def __init__(self):
            self.unlinked: list = []
            self.scanned_patterns: list = []

        async def ping(self):
            return True

        async def scan_iter(self, match=None, count=None):
            self.scanned_patterns.append(match)
            if match == "budget:user:*":
                yield b"budget:user:{u1}:2026-02"
                yield b"budget:user:{u1}:codex:2026-02"

        async def unlink(self, key):
            self.unlinked.append(key)

    redis = _Redis()
    hc = HealthChecker(redis, _fake_db_engine(ok=True), dm)

    # SUCCESS_THRESHOLD(3) 연속 성공으로 복구 — 마지막 틱에 무효화가 발동한다.
    for _ in range(3):
        await hc._check_redis()
    assert dm.level == DegradationLevel.HEALTHY

    # 백그라운드 태스크가 스케줄됐다 — 완료를 기다린다.
    await asyncio.gather(*hc._bg_tasks, return_exceptions=True)

    assert redis.unlinked == [
        b"budget:user:{u1}:2026-02",
        b"budget:user:{u1}:codex:2026-02",
    ]
    # config/resvsum 은 절대 건드리지 않는다 — 스캔 패턴 계약.
    assert set(redis.scanned_patterns) == {
        "budget:user:*",
        "budget:team:*",
        "budget:pending:*",
    }
