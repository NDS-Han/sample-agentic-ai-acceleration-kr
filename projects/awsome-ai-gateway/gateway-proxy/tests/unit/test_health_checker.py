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
async def test_redis_recovery_reconciles_stale_budget_counters():
    """REDIS_DEGRADED 복구 시 usage 카운터를 budget_usages 로 **교정**한다.

    열화 중엔 budget_deduct(INCRBYFLOAT)가 스킵돼 Redis 카운터가 DB 진실보다
    얇다. DEL 로 지우면 안 된다 — 복구 직후 finalize 의 INCRBYFLOAT 가 얇은
    키를 재생성해 exists() 재수화 게이트를 중독시킨다. 대신 additive
    budget_rebuild 를 base=(db+pending)-cur 로 호출해 최종값을 db+pending 에
    맞춘다(동시 델타 보존 + NX 락). 워커 지연은 창을 열어 재시도로 커버한다.
    """
    import asyncio

    from app.periods import request_period
    from app.services.lua_loader import LuaScriptLoader

    period = request_period()
    dm = DegradationManager()

    for _ in range(5):
        dm.report_redis_health(False)
    from app.schemas.domain import DegradationLevel

    assert dm.level == DegradationLevel.REDIS_DEGRADED

    # DB 스냅샷: u1 총합 50.0(실은 카운터엔 30.0 만 들어있다 = 20 누락).
    engine = MagicMock()

    class _Result:
        def all(self):
            return [("USER", "u1", None, 50.0)]

    @asynccontextmanager
    async def _connect():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=_Result())
        yield conn

    engine.connect = _connect

    class _Redis:
        def __init__(self):
            self.eval_calls: list = []

        async def ping(self):
            return True

        async def set(self, *args, **kwargs):
            return True  # 리더 게이트 항상 획득

        async def get(self, key):
            return {
                f"budget:user:{{u1}}:{period}": "30.0",  # 얇은 카운터
                f"budget:resvsum:user:{{u1}}:{period}": "5.0",
            }.get(key)

        async def scan_iter(self, match=None, count=None):
            if match == "budget:pending:user:{u1}:*":
                yield "budget:pending:user:{u1}:r1"

        async def eval(self, script, numkeys, *args):
            self.eval_calls.append((numkeys, args))

        async def unlink(self, key):  # 교정 방식은 어떤 키도 지우지 않는다
            raise AssertionError("reconcile must not delete keys")

    redis = _Redis()
    LuaScriptLoader._scripts.setdefault("budget_rebuild", "-- mock")
    hc = HealthChecker(redis, engine, dm)

    for _ in range(3):
        await hc._check_redis()
    assert dm.level == DegradationLevel.HEALTHY

    await asyncio.gather(*hc._bg_tasks, return_exceptions=True)

    # 카운터가 아직 5.0 인 pending 1건(15.0-? 아니라 마커 값 없이 0 취급 아님 —
    # _pending_reservation_sum 은 get() 이 None 을 돌려 0 으로 합산한다).
    assert len(redis.eval_calls) == 1
    numkeys, args = redis.eval_calls[0]
    assert numkeys == 3
    assert args[0] == f"budget:user:{{u1}}:{period}"
    # base = used(50) + pending(0) - cur(30) = 20 → 최종 카운터 = 50.0
    assert float(args[3]) == pytest.approx(20.0)
    # resvsum base = pending(0) - cursum(5) = -5 → resvsum = 0
    assert float(args[4]) == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_reconcile_never_lowers_usage_counter():
    """cur > db(파이프라인 깊이)일 때 usage 교정은 0 하한 — 카운터를 깎지 않는다.

    정상 운영 중 활성 유저는 finalize 즉시 Redis 에 계상되고 워커 flush 까지
    budget_usages 가 뒤처져 항상 cur > db 다. 음수 교정을 허용하면 매 틱
    정당하게 계상된 사용분이 깎여 fail-open 예산 창이 반복적으로 열린다.
    resvsum 은 양방향 유지(죽은 예약의 stale over-hold 해소가 목적).
    """
    import asyncio

    from app.periods import request_period
    from app.services.lua_loader import LuaScriptLoader

    period = request_period()
    dm = DegradationManager()
    for _ in range(5):
        dm.report_redis_health(False)
    from app.schemas.domain import DegradationLevel

    assert dm.level == DegradationLevel.REDIS_DEGRADED

    # DB 스냅샷 80 — 하지만 Redis 카운터는 100(미flush 파이프라인 사용분 포함).
    engine = MagicMock()

    class _Result:
        def all(self):
            return [("USER", "u1", None, 80.0)]

    @asynccontextmanager
    async def _connect():
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=_Result())
        yield conn

    engine.connect = _connect

    class _Redis:
        def __init__(self):
            self.eval_calls: list = []

        async def ping(self):
            return True

        async def set(self, *args, **kwargs):
            return True  # 리더 게이트 항상 획득

        async def get(self, key):
            return {
                f"budget:user:{{u1}}:{period}": "100.0",  # 앞선 카운터
                f"budget:resvsum:user:{{u1}}:{period}": "7.0",
            }.get(key)

        async def scan_iter(self, match=None, count=None):
            if match == "budget:pending:user:{u1}:*":
                yield "budget:pending:user:{u1}:r1"  # get()→None → 0 합산

        async def eval(self, script, numkeys, *args):
            self.eval_calls.append((numkeys, args))

    redis = _Redis()
    LuaScriptLoader._scripts.setdefault("budget_rebuild", "-- mock")
    hc = HealthChecker(redis, engine, dm)
    for _ in range(3):
        await hc._check_redis()
    assert dm.level == DegradationLevel.HEALTHY
    await asyncio.gather(*hc._bg_tasks, return_exceptions=True)

    assert len(redis.eval_calls) == 1
    _, args = redis.eval_calls[0]
    # usage_delta = 80 + 0 - 100 = -20 → 0 클램프(카운터를 깎지 않는다)
    assert float(args[3]) == pytest.approx(0.0)
    # resvsum = pending(0) - cursum(7) = -7 → 양방향 유지
    assert float(args[4]) == pytest.approx(-7.0)
