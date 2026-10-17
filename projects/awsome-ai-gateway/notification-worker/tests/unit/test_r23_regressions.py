# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R23 r1 회귀 — 버퍼 드레인 유실·감독 백오프·오류 마스킹.

S2  ``drain`` 이 중간 실패 시 실패 이벤트만 아니라 **배치 잔여 전부**를
    재큐한다 — dequeue 만 하고 버리면 Pub/Sub 재생도 행도 없어 sweeper 가
    복구하지 못하는 영구 유실이었다.

S3  TaskSupervisor 백오프는 오래 살았던 실행의 실패에서 초기화된다 —
    누적만 하는 백오프는 파드 수명 후반의 짧은 blip 마다 60s 구독 해제
    (알림 유실) 창을 만든다.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.services.notification_buffer import NotificationBufferQueue


def _event(event_id: str) -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        type=EventType.BUDGET_THRESHOLD,
        timestamp="2026-04-10T12:00:00+00:00",  # type: ignore[arg-type]
        source=ServiceSource.GATEWAY_PROXY,
        payload={"user_id": "u1"},
    )


async def test_drain_requeues_unprocessed_tail_on_failure() -> None:
    """5건 중 3번째가 실패하면 3·4·5번째가 전부 큐로 돌아간다."""
    buf = NotificationBufferQueue(max_size=10)
    for i in range(5):
        await buf.enqueue(_event(f"e{i}"))

    calls: list[str] = []

    async def process(ev: NotificationEvent) -> None:
        calls.append(ev.event_id)
        if ev.event_id == "e2":
            raise RuntimeError("db flapping")

    await buf.drain(process)

    assert calls == ["e0", "e1", "e2"]
    # e2(실패)·e3·e4 가 큐로 되돌아갔다 — 유실이 없다.
    assert buf.size == 3
    remaining = [await asyncio.wait_for(buf._queue.get(), 1) for _ in range(3)]
    assert [e.event_id for e in remaining] == ["e2", "e3", "e4"]


async def test_supervisor_backoff_resets_after_stable_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300s 이상 살았던 실행의 실패는 백오프를 1s 로 리셋한다."""
    import time as _t

    from worker.worker import TaskSupervisor

    supervisor = TaskSupervisor()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep
    real_monotonic = _t.monotonic

    # coro 내부에서 오프셋을 400s 씩 밀어 "오래 산 실행" 을 시뮬레이션한다.
    # 시계 자체는 계속 진행하므로 이벤트 루프 타이머는 정상 동작한다.
    offset = [0.0]
    monkeypatch.setattr(_t, "monotonic", lambda: real_monotonic() + offset[0])

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def flaky() -> None:
        offset[0] += 400.0  # 실행이 400s 살아있던 것처럼
        raise RuntimeError("blip")

    task = asyncio.ensure_future(supervisor._supervised_run("t", flaky))
    for _ in range(20):
        await real_sleep(0.01)
        if len(sleeps) >= 3:
            break
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # 매 실행이 400s 후 실패 → 리셋이 발화해 백오프가 누적되지 않는다.
    assert len(sleeps) >= 3
    assert all(s == 1.0 for s in sleeps)


async def test_supervisor_backoff_grows_for_crash_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대조군 — 즉시 실패하는 태스크는 백오프가 지수적으로 누적된다."""
    from worker.worker import TaskSupervisor

    supervisor = TaskSupervisor()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def crashy() -> None:
        raise RuntimeError("instant")

    task = asyncio.ensure_future(supervisor._supervised_run("t", crashy))
    for _ in range(20):
        await real_sleep(0.01)
        if len(sleeps) >= 4:
            break
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]


class _DictRedis:
    """클레임/해제 Lua 의미론의 최소 구현 — 쿨다운 클레임 경쟁을 실제로 재현한다."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    async def eval(self, script, _nkeys, *args):
        if "nx" in script:  # _CLAIM_COOLDOWN_LUA
            key, event_id = args[0], args[1]
            if key not in self._d:
                self._d[key] = event_id
                return 1
            if self._d[key] == event_id:
                return 2
            return 0
        key, event_id = args  # _RELEASE_COOLDOWN_LUA — compare-and-del
        if self._d.get(key) == event_id:
            del self._d[key]
            return 1
        return 0


async def test_cooldown_claim_suppresses_concurrent_burst() -> None:
    """같은 정체성·다른 event_id 4건이 동시에 오면 발송은 1회다(R23 r2 S-4).

    확인→처리→기록 방식은 발송 완료(SES 왕복)까지 창이 열려 4통이 나갔다.
    SET NX 선점은 첫 처리자만 통과시키고 나머지는 다른 event_id 의 키를
    보고 억제한다.
    """
    from unittest.mock import AsyncMock, MagicMock

    from worker.handlers.budget_handler import BudgetHandler
    from worker.schemas.recipients import Recipient, RecipientRole

    config = MagicMock(enabled=True, recipient_roles=[RecipientRole.ADMIN])
    config_cache = MagicMock()
    config_cache.get.return_value = config
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        return_value=[
            Recipient(
                email="admin@x.com", name="A", user_id="u9",
                role=RecipientRole.ADMIN,
            )
        ]
    )
    engine = MagicMock()
    engine.render.return_value = ("s", "<h/>")

    sends = 0

    async def _send(*_a, **_kw):
        nonlocal sends
        await asyncio.sleep(0.05)  # SES 왕복 흉내 — 선점 없으면 이 창에 전부 통과
        sends += 1

    async def _execute(fn, event_type=None):
        return await fn()

    retry_executor = MagicMock()
    retry_executor.execute = AsyncMock(side_effect=_execute)

    sender = MagicMock()
    sender.send = AsyncMock(side_effect=_send)
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    handler = BudgetHandler(
        session_factory=MagicMock(return_value=ctx),
        config_cache=config_cache,
        recipient_resolver=resolver,
        template_engine=engine,
        email_sender=sender,
        retry_executor=retry_executor,
        metrics=None,
        redis_client=_DictRedis(),
        cooldown_seconds=600,
    )

    burst = [
        _event(f"e{i}") for i in range(4)
    ]  # 같은 정체성(user_id=u1), 다른 event_id
    await asyncio.gather(*(handler.handle(e) for e in burst))
    assert sends == 1


def test_mask_emails_in_text() -> None:
    """오류 문자열 속 수신자 주소를 마스킹한다 — 로그/DB 에 PII 평문 금지."""
    from worker.services.email_mask import mask_emails_in_text

    raw = "SMTPRecipientsRefused: {'alice@corp.com': (550, b'blocked')}"
    masked = mask_emails_in_text(raw)
    assert "alice@corp.com" not in masked
    assert "a***@corp.com" in masked
