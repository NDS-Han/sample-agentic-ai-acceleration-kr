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

    async def stop_all(
        self, grace_period: float = 30.0, cooperative_window: float = 0.0
    ) -> None:
        """종료 — (선택) 협조 종료 창 → cancel → 잔여 grace 대기.

        ⚠️ cancel 을 먼저 보내면 ``request_stop()`` 같은 협조 플래그를 코루틴이
           볼 기회가 없다 — 배치 경계의 정리 로직은 죽은 채로 다음 await 에서
           잘린다. ``cooperative_window`` 만큼 자연 종료를 먼저 기다리고,
           살아남은 태스크만 cancel 한다. 총 소요는 ``grace_period`` 를 넘지
           않는다.
        """
        tasks = [t for t in self._tasks.values() if not t.done()]
        done: set[asyncio.Task] = set()
        pending = set(tasks)
        if cooperative_window > 0 and tasks:
            done, pending = await asyncio.wait(tasks, timeout=cooperative_window)

        for task in pending:
            task.cancel()
            logger.info("task_cancellation_requested", name=task.get_name())

        if pending:
            remaining = max(grace_period - cooperative_window, 0.0)
            done2, still = await asyncio.wait(pending, timeout=remaining)
            done |= done2
            if still:
                logger.warning("tasks_not_cancelled_in_time", count=len(still))
        for task in done:
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.error("task_exited_with_exception", error=str(exc))

        logger.info("supervisor_stopped")
