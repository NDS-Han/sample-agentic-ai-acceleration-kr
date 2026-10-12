# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import time

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
        # 예산 카운터 교정 창 — 열화 해제 전이 시점부터 이 시각까지 매 healthy
        # 틱마다 재시도한다. 스풀 재발행→워커 flush→budget_usages 반영에 지연이
        # 있어 단발 교정은 열화 중 사용분을 놓칠 수 있다(워커 지연).
        self._budget_reconcile_until: float = 0.0
        self._budget_reconcile_running = False
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
                # 복구 전이에 교정 창을 열고, 창이 닫힐 때까지 매 틱 재교정한다.
                self._budget_reconcile_until = time.monotonic() + 600.0
            # 창이 열려 있는 동안 매 healthy 틱 재교정 — 단발은 워커 지연(스풀
            # 재발행→budget_usages 반영)을 놓친다. 교정 자체는 멱등.
            if (
                time.monotonic() < self._budget_reconcile_until
                and not self._budget_reconcile_running
            ):
                self._budget_reconcile_running = True

                async def _reconcile_once() -> None:
                    try:
                        await self._reconcile_budget_counters()
                    finally:
                        self._budget_reconcile_running = False

                _rec = asyncio.create_task(_reconcile_once())
                self._bg_tasks.add(_rec)
                _rec.add_done_callback(self._bg_tasks.discard)
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

    async def _reconcile_budget_counters(self) -> None:
        """Redis 복구 후 usage 카운터를 budget_usages(DB 진실)로 교정한다.

        ⚠️ DEL 방식이면 안 되는 이유:
          1. ``budget_deduct``(finalize)는 exists 무관하게 INCRBYFLOAT 로 키를
             **생성**한다 — DEL 직후 finalize 가 얇은 키를 만들면 그 키가
             ``exists()==True`` 를 만족해 lazy rebuild 가 영구 안 돌고 전체
             이력이 누락된 채로 남는다(원래 고치려던 갭 그대로).
          2. 복구 시점의 budget_usages 스냅샷은 스풀 재발행+워커 flush 가 끝나기
             전이라 열화 중 사용분을 아직 모른다 — 그래서 창을 열어 재시도한다.

        ``budget_rebuild.lua`` 는 additive(`SET = base + cur`) + NX 락이다 —
        base 에 ``(db + pending) - cur`` 를 넘기면 최종값이 정확히
        ``db + pending`` 이 되면서, 호출 사이에 끼어든 reserve/settle 델타도
        더해져 보존된다(check 경로의 동일 경합 계약). 단 usage 베이스는 0
        으로 하한 클램프한다 — 활성 유저는 스트림 파이프라인 깊이만큼 항상
        cur > db 이므로, 음수 교정은 정당하게 계상된 사용분을 깎아 매 틱
        fail-open 창을 연다. resvsum 은 마커 스캔이 진실이라 양방향으로
        ``pending`` 에 맞춘다.

        행 없는 카운터(예약만 있는 유저 등)는 건드리지 않는다 — 그 카운터는
        이미 db(0)+pending 과 일치한다.
        """
        from decimal import Decimal

        from app.periods import request_period
        from app.services.lua_loader import LuaScriptLoader

        period = request_period()
        # ⚠️ 리더 게이트 — 모든 파드의 health checker 가 같은 창에 같은 스캔을
        #    돌리면 N× 배수의 Redis/DB 부하다. NX+TTL 로 한 파드만 교정하고,
        #    그 파드가 죽으면 TTL 만료로 다음 틱에 다른 파드가 잇는다.
        #    TTL 은 루프 안에서 주기적으로 갱신한다 — 안 하면 실행이 45s 를
        #    넘는 순간 다른 파드가 리더를 잡아 병렬 실행이 다시 시작된다.
        #    (교정은 멱등이라 리더가 아니어도 안전하지만 비용이 크다.)
        try:
            if not await self._redis.set(
                "budget:reconcile-leader", "1", nx=True, ex=45
            ):
                return
        except Exception:
            return  # Redis 방금 플랩 — 다음 틱이 재시도
        try:
            async with self._db_engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT scope, scope_id, client, used_usd "
                            "FROM budget.budget_usages WHERE period = :period"
                        ),
                        {"period": period},
                    )
                ).all()
        except Exception:
            # DB 도 아직 열화 중 — 창 안의 다음 틱이 재시도한다.
            logger.warning("budget_reconcile_snapshot_failed")
            return

        script = LuaScriptLoader.get("budget_rebuild")

        # ⚠️ pending 마커는 **한 번의 키스페이스 스캔**으로 전부 모은다 —
        #    행마다 SCAN MATCH 를 돌리면 MATCH 가 순회량을 줄여주지 않아
        #    행 수 × 전체 키스페이스(Cluster 면 전 primary)만큼 왕복한다.
        #    마커 형식:
        #      budget:pending:user:{uid}:{req_id}
        #      budget:pending:team:{tid}:{req_id}
        #      budget:pending:client:{uid}:{client}:{req_id}
        pending_sums: dict[tuple[str, str, str], Decimal] = {}
        try:
            async for key in self._redis.scan_iter(
                match="budget:pending:*", count=500
            ):
                val = await self._redis.get(key)
                if val is None:
                    continue
                try:
                    amount = Decimal(
                        val.decode() if isinstance(val, bytes) else str(val)
                    )
                    kstr = key.decode() if isinstance(key, bytes) else key
                    # "budget:pending:" 이후 — [0]=user|team|client,
                    # [1]={sid}, client 이면 [2]=client 이름, 마지막=req_id.
                    parts = kstr.split("budget:pending:", 1)[1].split(":")
                    if parts[0] in ("user", "team") and len(parts) >= 3:
                        sid = parts[1].strip("{}")
                        if sid:
                            k = (parts[0].upper(), sid, "")
                            pending_sums[k] = pending_sums.get(k, Decimal("0")) + amount
                    elif parts[0] == "client" and len(parts) >= 4:
                        sid = parts[1].strip("{}")
                        if sid:
                            k = ("USER", sid, parts[2])
                            pending_sums[k] = pending_sums.get(k, Decimal("0")) + amount
                except Exception:
                    continue
        except Exception:
            logger.warning("budget_reconcile_pending_scan_failed")
            pending_sums = {}

        reconciled = 0
        for i, (scope, scope_id, client, used_usd) in enumerate(rows):
            scope = str(scope).upper()
            sid = str(scope_id)
            try:
                # 리더 락 주기 갱신 — 실행이 TTL(45s)을 넘으면 다른 파드가
                # 병렬 실행을 시작해 게이트가 무의미해진다.
                if i and i % 200 == 0:
                    try:
                        await self._redis.expire("budget:reconcile-leader", 45)
                    except Exception:
                        pass
                if scope == "USER" and client:
                    c = str(client)
                    usage_key = f"budget:user:{{{sid}}}:{c}:{period}"
                    resvsum_key = f"budget:resvsum:user:{{{sid}}}:{c}:{period}"
                    lock_key = f"budget:rebuild-lock:{{{sid}}}:{c}"
                    pending = pending_sums.get(("USER", sid, c), Decimal("0"))
                elif scope == "USER" and not client:
                    usage_key = f"budget:user:{{{sid}}}:{period}"
                    resvsum_key = f"budget:resvsum:user:{{{sid}}}:{period}"
                    lock_key = f"budget:rebuild-lock:{{{sid}}}"
                    pending = pending_sums.get(("USER", sid, ""), Decimal("0"))
                elif scope == "TEAM" and not client:
                    # ⚠️ TEAM+client 행은 스키마상 만들어지지 않는다 — 있으면
                    #    같은 총합 키를 하위집합 값으로 반복 교정해 오염시키므로
                    #    명시적으로 걸러낸다.
                    usage_key = f"budget:team:{{{sid}}}:{period}"
                    resvsum_key = f"budget:resvsum:team:{{{sid}}}:{period}"
                    lock_key = f"budget:rebuild-lock:{{{sid}}}"
                    pending = pending_sums.get(("TEAM", sid, ""), Decimal("0"))
                else:
                    continue

                cur_raw = await self._redis.get(usage_key)
                cursum_raw = await self._redis.get(resvsum_key)
                # ⚠️ bytes 디코딩 — decode_responses=False 라 get() 은 bytes.
                #    str(b'30.0') 은 InvalidOperation → 이 키가 조용히 스킵된다.
                cur = (
                    Decimal(cur_raw.decode() if isinstance(cur_raw, bytes) else str(cur_raw))
                    if cur_raw
                    else Decimal("0")
                )
                cursum = (
                    Decimal(cursum_raw.decode() if isinstance(cursum_raw, bytes) else str(cursum_raw))
                    if cursum_raw
                    else Decimal("0")
                )
                # ⚠️ usage 교정은 **양수 방향만** 허용한다. 정상 운영 중에도
                #    cur 은 finalize 즉시 올라가지만 db(budget_usages)는 워커
                #    flush 까지 뒤처져, 활성 유저는 항상 cur > db 다 — 음수
                #    교정을 허용하면 매 틱 카운터를 stale 스냅샷으로 끌어내려
                #    미flush 파이프라인 깊이만큼 반복적 fail-open 창이 열린다.
                #    반대 방향(cur>db 잔류, 스트림 유실)은 과대계상=fail-closed
                #    라 보수적으로 그대로 둔다.
                #    resvsum 은 양방향 — 마커 스캔이 곧 진실이라 stale over-hold
                #    (죽은 예약의 잔여)를 풀어줘야 한다.
                usage_delta = Decimal(str(used_usd)) + pending - cur
                if usage_delta < 0:
                    usage_delta = Decimal("0")
                await self._redis.eval(
                    script,
                    3,
                    usage_key,
                    resvsum_key,
                    lock_key,
                    str(usage_delta),
                    str(pending - cursum),
                )
                reconciled += 1
            except Exception:
                logger.warning(
                    "budget_reconcile_key_failed", scope=scope, scope_id=sid
                )
        if reconciled:
            logger.info(
                "budget_counters_reconciled",
                period=period,
                keys=reconciled,
            )
