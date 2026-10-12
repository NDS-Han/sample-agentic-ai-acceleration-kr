# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


class TaskSupervisor:
    """asyncio 태스크 수명 주기 관리자.

    등록된 태스크가 예외로 죽으면 지수 백오프(1s → 60s)로 재시작.
    ``stop_all()`` 시 모든 태스크 취소 + grace_period 초 내 완료 대기.
    """

    def __init__(self, max_backoff: float = 60.0) -> None:
        self.max_backoff = max_backoff
        self._registry: dict[str, Callable[[], Awaitable[None]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, name: str, coro_fn: Callable[[], Awaitable[None]]) -> None:
        self._registry[name] = coro_fn

    async def start_all(self) -> None:
        for name, coro_fn in self._registry.items():
            task: asyncio.Task[None] = asyncio.create_task(
                self._supervised_run(name, coro_fn), name=name
            )
            self._tasks[name] = task
        logger.info("supervisor_started", task_count=len(self._registry))

    #: 이 시간 이상 안정적으로 돈 태스크가 죽으면 백오프를 초기화한다 — 파드
    #: 수명 동안 일시 오류가 몇 번 누적되면 그 뒤엔 매 실패가 60s 씩 소비를
    #: 멈추게 돼, 짧은 blip 도 정지에 가까운 지연을 누적시킨다.
    _STABLE_RESET_SEC = 300.0

    async def _supervised_run(
        self, name: str, coro_fn: Callable[[], Awaitable[None]]
    ) -> None:
        import time as _time

        backoff: float = 1.0
        while True:
            started_at = _time.monotonic()
            try:
                await coro_fn()
                logger.warning("task_exited_normally", name=name)
                break
            except asyncio.CancelledError:
                logger.info("task_cancelled", name=name)
                raise
            except Exception as exc:
                if _time.monotonic() - started_at > self._STABLE_RESET_SEC:
                    backoff = 1.0  # 오래 살았던 실행의 실패 — 과거 누적과 무관하게 리셋
                logger.error(
                    "task_failed_restarting", name=name, error=str(exc), backoff=backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def stop_all(self, grace_period: float = 30.0) -> None:
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                logger.info("task_cancellation_requested", name=name)

        if self._tasks:
            done, pending = await asyncio.wait(
                list(self._tasks.values()), timeout=grace_period
            )
            if pending:
                logger.warning("tasks_not_cancelled_in_time", count=len(pending))
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        logger.error("task_exited_with_exception", error=str(exc))

        logger.info("supervisor_stopped")
