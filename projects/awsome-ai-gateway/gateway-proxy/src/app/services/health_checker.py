# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.degradation.manager import DegradationManager

logger = structlog.get_logger(__name__)


class HealthChecker:
    """주기적으로 DB/Redis 상태를 확인하고 DegradationManager에 보고한다.

    deepdive Q50 Phase 3: DB·Redis 체크를 **동시(asyncio.gather)** 실행해 한 틱의
    벽시계 시간을 둘 중 느린 쪽으로 줄인다(과거엔 순차 → 최대 6s 직렬). interval 도
    설정 가능(기본 15s 보존). 감지 지연 = interval × FAIL_THRESHOLD 이므로 interval↓
    이 진짜 장애 진입을 앞당긴다(히스테리시스가 플래핑 false-positive 를 막아준다).
    """

    def __init__(
        self,
        redis,
        db_engine: AsyncEngine,
        degradation_manager: DegradationManager,
        interval: int = 15,
        cost_stream_spool=None,
    ) -> None:
        self._redis = redis
        self._db_engine = db_engine
        self._degradation = degradation_manager
        self._interval = interval
        self._task: asyncio.Task | None = None
        # 백그라운드 태스크 강한 참조 — asyncio 는 약한 참조만 들고 있어, 참조
        # 없이 만든 태스크는 완료 전 GC 될 수 있다.
        self._bg_tasks: set[asyncio.Task] = set()
        # P0-②: drained on each healthy Redis check to re-publish cost:stream
        # payloads that failed XADD while Redis was down.
        self._cost_stream_spool = cost_stream_spool

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while True:
            # DB·Redis 동시 체크(상호 독립) → 한 틱 = max(두 체크), not 합.
            # return_exceptions=True 로 한쪽 예외가 다른쪽 보고를 막지 않게 한다
            # (각 _check_* 가 내부에서 예외를 잡아 보고하므로 추가 안전망).
            await asyncio.gather(
                self._check_db(),
                self._check_redis(),
                return_exceptions=True,
            )
            await asyncio.sleep(self._interval)

    async def _check_db(self) -> None:
        try:
            # ⚠️ wait_for 는 커넥션 획득까지 감싸야 한다 — 풀 포화라는 바로 그
            #    상황에서 connect() 가 pool_timeout 까지 블록해, 프로브가 측정해야
            #    할 지연이 틱 전체를 잡아먹는다. 타임아웃은 pool_timeout 보다 작게.
            async def _probe() -> None:
                async with self._db_engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))

            await asyncio.wait_for(_probe(), timeout=5.0)
            self._degradation.report_db_health(healthy=True)
        except Exception:
            logger.warning("db_health_check_failed")
            self._degradation.report_db_health(healthy=False)

    async def _check_redis(self) -> None:
        try:
            await asyncio.wait_for(
                self._redis.ping(),
                timeout=1.0,
            )
            # 복구 전이 감지는 manager.level 을 직접 비교한다 — checker 재시작으로
            # 플래그가 날아가도 정합(redis-degraded 집합에서 빠져나간 순간만 발동).
            from app.schemas.domain import DegradationLevel

            _REDIS_DEGRADED = (
                DegradationLevel.REDIS_DEGRADED,
                DegradationLevel.BOTH_DEGRADED,
            )
            was_redis_degraded = self._degradation.level in _REDIS_DEGRADED
            self._degradation.report_redis_health(healthy=True)
            if was_redis_degraded and self._degradation.level not in _REDIS_DEGRADED:
                # REDIS_DEGRADED 동안 budget_deduct(INCRBYFLOAT)가 스킵돼 usage
                # 카운터가 budget_usages(DB 진실)보다 얇다. 키는 "존재"하니
                # exists() 기반 재수화가 절대 안 돌고, 누락분만큼 월예산이 헐거워진다.
                # 복구 전이에 usage 카운터+pending 마커를 지우면 다음 check 이
                # budget_usages 로부터 lazy rebuild 한다. resvsum 은 건드리지
                # 않는다(버려진 예약만큼 과대집계 = 예산 방향으로 보수적).
                _inv = asyncio.create_task(self._invalidate_stale_budget_keys())
                self._bg_tasks.add(_inv)
                _inv.add_done_callback(self._bg_tasks.discard)
            # P0-②: Redis is up — re-publish any cost:stream payloads that were
            # spooled while it was down (best-effort; failures re-buffer).
            if self._cost_stream_spool is not None and self._cost_stream_spool.size:
                try:
                    # ⚠️ 상한 필수 — drain 은 XADD 를 순차 실행하는데, flapping
                    #    Redis 에선 각 XADD 가 socket_timeout+retry(≈4s)만큼
                    #    블록한다. 10k 스풀이면 틱 하나가 사실상 무한 — 이
                    #    태스크가 멈추면 degradation 전이 자체가 동결된다.
                    #    drain() 은 취소안전(미완료 항목은 버퍼로 복원, finally)
                    #    이므로 TimeoutError 로 끊어도 유실이 없고, 잔여분은
                    #    다음 healthy 틱에서 이어서 비운다.
                    await asyncio.wait_for(
                        self._cost_stream_spool.drain(self._redis),
                        timeout=10.0,
                    )
                except TimeoutError:
                    logger.warning(
                        "cost_stream_spool_drain_timed_out",
                        remaining=self._cost_stream_spool.size,
                    )
                except Exception:
                    logger.warning("cost_stream_spool_drain_failed")
        except Exception:
            logger.warning("redis_health_check_failed")
            self._degradation.report_redis_health(healthy=False)

    async def _invalidate_stale_budget_keys(self) -> None:
        """Redis 복구 전이에 얇아진 예산 카운터를 무효화한다 (틱 밖 백그라운드).

        삭제 대상:
          - ``budget:user:*`` / ``budget:team:*`` — usage 카운터(총합+per-app).
            다음 ``check_budget`` 이 ``exists()==False`` 를 보고 budget_usages
            에서 lazy rebuild 한다(DB 가 진실의 원천).
          - ``budget:pending:*`` — 복구 시점에 날아간 카운터 위의 예약 마커.
            지워야 settle 이 삭제된 카운터를 음수 델타로 재생성해 재수화
            (``exists``) 게이트를 오염시키지 않는다. settle 스킵된 예약은
            resvsum 에 남아 과대집계(보수적)로 수렴한다.
        건드리지 않는 것: ``budget:config:*``(설정, 소실 아님), ``budget:resvsum:*``
        (표시용 미정산 합계 — 지우면 settle 가 음수로 만들어 표시가 부풀어진다).
        """
        deleted = 0
        try:
            for pattern in ("budget:user:*", "budget:team:*", "budget:pending:*"):
                async for key in self._redis.scan_iter(match=pattern, count=500):
                    try:
                        await self._redis.unlink(key)
                        deleted += 1
                    except Exception:
                        logger.warning("budget_key_invalidate_failed", key=str(key)[:64])
            logger.info("budget_counters_invalidated_on_recovery", deleted=deleted)
        except Exception:
            logger.warning("budget_key_invalidation_scan_failed", deleted=deleted)
