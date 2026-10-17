# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from worker.handlers.base import BaseHandler
    from worker.observability.metrics import WorkerMetrics

logger = structlog.get_logger(__name__)

# base.py 의 dead-claim 나이와 같은 기준 — 발송+재시도 최악의 창(~130s)의
# 두 배보다 오래 'pending' 인 행만 dead 로 본다.
_PENDING_STALE_SEC = 300
# 'failed' 재시도 전 백오프 — SES/SMTP 장애가 retry_executor(3회, 수 초)를
# 넘긴 경우를 커버하되, 재시도 폭주는 막는다.
_FAILED_RETRY_AGE_SEC = 600
# 클레임 상한 — 계속 실패하는 행이 영원히 회수·재발송되지 않게. 상한에 닿은
# 'pending' 행은 'abandoned' 로 확정해 로그/감사에만 남긴다.
_MAX_ATTEMPTS = 6
# 배포 직후 과거 행 전체를 재발송하는 폭주 방지 — 24h 이상 된 행은 회수하지
# 않는다(그 시점의 알림은 이미 무의미하거나 운영자가 별도 확인한 상태).
_MAX_ROW_AGE_SEC = 24 * 3600
# 스윕 1회의 시간 예산 — 한 번의 스윕이 _PENDING_STALE_SEC 를 넘기면 아직
# 발송 중인 행의 하트비트가 stale 해져 다른 replica 의 스위퍼가 재클레임한다.
_SWEEP_BUDGET_SEC = 240.0


class PendingSweepTask:
    """stuck notification_logs 를 주기적으로 회수해 재발송한다 (RP-03).

    Pub/Sub 은 재전송이 없어 아래 행들은 방치하면 영구 미발송/미처리다:

    - ``pending`` + 활동 정지: INSERT 후 send 전 crash·SIGKILL·배포 취소로
      슬롯만 잡힌 dead-claim. dedup 쿼리가 이 행을 보고 재발행도 스킵한다.
    - ``failed``: 발송 자체는 끝났으나 SES/SMTP 장애가 retry_executor 를 넘긴
      경우. 한 번 failed 는 최종 상태라 같은 이벤트의 재발행도 중복 스킵된다.

    행은 **한 번에 하나씩** 클레임한다 — 배치로 잡으면 50행의 하트비트가 전부
    클레임 시각으로 찍히고, 느린 발송(최악 ~130s/행) 중 앞쪽 행이 다시 stale
    해져 다른 replica 의 스위퍼가 같은 행을 인수·이중 발송할 수 있다.
    클레임 자체는 ``UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)``
    단일문으로 원자화하고, 재발송 직전에는 클레임이 찍은 last_attempt_at 를
    펜싱 토큰으로 재검증해(base.py resend_log_entry) 경쟁 클레임을 한 번 더
    걸러낸다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_type_handler_map: dict[str, "BaseHandler"],
        sweep_interval: int = 120,
        metrics: "WorkerMetrics | None" = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = event_type_handler_map
        self._interval = sweep_interval
        self._metrics = metrics

    async def run(self) -> None:
        logger.info("pending_sweeper_started", interval=self._interval)
        while True:
            try:
                resent = await self._sweep()
                if resent:
                    logger.info("pending_sweep_completed", resent=resent)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pending_sweep_failed")
                if self._metrics:
                    self._metrics.errors_total.add(1, {"error_type": "sweeper"})
            await asyncio.sleep(self._interval)

    async def _claim_one(self) -> dict | None:
        """가장 오래된 stale 행 1개를 'pending' 으로 원자 클레임한다.

        RETURNING last_attempt_at 은 재발송 직전 재검증에 쓰는 펜싱 토큰 —
        클레임~재발송 사이 다른 워커가 행을 인수하면 토큰이 달라져 스킵된다.
        'abandoned' 확정 UPDATE 도 함께 수행한다.
        """
        async with self._session_factory() as session:
            # attempt 상한에 닿은 채 남은 'pending' 행을 터미널로 확정 — 이
            # UPDATE 가 없으면 상한 행이 'pending' 상태로 영원히 남는다.
            abandoned = (
                await session.execute(
                    text(
                        """
                        UPDATE notification.notification_logs
                        SET status = 'abandoned'
                        WHERE status = 'pending' AND attempt_count >= :max_attempts
                        """
                    ),
                    {"max_attempts": _MAX_ATTEMPTS},
                )
            ).rowcount
            if abandoned:
                # 재시도 상한에 닿아 최종 유실된 알림 — 그냥 지나가면 영구
                # 미발송이 아무 신호 없이 일어난다.
                logger.warning(
                    "notification_abandoned_max_attempts", count=abandoned
                )
                if self._metrics:
                    self._metrics.errors_total.add(
                        abandoned, {"error_type": "abandoned"}
                    )
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE notification.notification_logs
                        SET status = 'pending',
                            last_attempt_at = now(),
                            attempt_count = attempt_count + 1,
                            resolved_at = NULL,
                            error_message = NULL
                        WHERE id = (
                            SELECT id FROM notification.notification_logs
                            WHERE attempt_count < :max_attempts
                              AND created_at > now() - make_interval(secs => :max_age)
                              AND coalesce(error_message, '') NOT LIKE 'resend_skipped_%'
                              AND (
                                  (status = 'pending'
                                   AND coalesce(last_attempt_at, created_at)
                                       < now() - make_interval(secs => :pending_age))
                               OR (status = 'failed'
                                   AND coalesce(last_attempt_at, created_at)
                                       < now() - make_interval(secs => :failed_age))
                              )
                            ORDER BY created_at
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING id, event_type, last_attempt_at
                        """
                    ),
                    {
                        "pending_age": _PENDING_STALE_SEC,
                        "failed_age": _FAILED_RETRY_AGE_SEC,
                        "max_attempts": _MAX_ATTEMPTS,
                        "max_age": _MAX_ROW_AGE_SEC,
                    },
                )
            ).mappings().first()
            await session.commit()
            return dict(row) if row else None

    async def _sweep(self) -> int:
        resent = 0
        deadline = time.monotonic() + _SWEEP_BUDGET_SEC
        while time.monotonic() < deadline:
            row = await self._claim_one()
            if row is None:
                break

            handler = self._handlers.get(row["event_type"])
            if handler is None:
                logger.warning(
                    "sweeper_no_handler", event_type=row["event_type"]
                )
                continue
            try:
                await handler.resend_log_entry(
                    str(row["id"]), claimed_at=row["last_attempt_at"]
                )
                resent += 1
            except Exception:
                # 행은 'pending' + 갱신된 last_attempt_at — 다음 스윕이
                # stale 조건을 다시 만족하면 재클레임한다(attempts 상한 내).
                logger.exception("sweeper_resend_failed", log_id=str(row["id"]))
                if self._metrics:
                    self._metrics.errors_total.add(1, {"error_type": "sweeper"})
        return resent
