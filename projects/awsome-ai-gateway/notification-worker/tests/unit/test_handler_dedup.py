# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""notification-worker 이벤트 재배송 멱등 회귀 테스트 (F3).

cost-recorder 부분 replay 가 threshold 이벤트를 재발행하거나, pub/sub 다중
구독자가 같은 이벤트를 동시에 받아도, 같은 (event_id, recipient) 에 메일이
두 번 나가면 안 된다. 게이트는 notification.notification_logs 의 유니크 인덱스
ux_notification_logs_event_recipient — INSERT 시점에 슬롯을 선점하고
IntegrityError 를 "이미 배송됨"으로 처리해 스킵한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from worker.handlers.budget_handler import BudgetHandler
from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.schemas.recipients import Recipient, RecipientRole


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="evt-dup-1",
        type=EventType.BUDGET_THRESHOLD,
        timestamp=datetime.now(UTC),
        source=ServiceSource.COST_RECORDER_WORKER,
        payload={"user_id": "u1", "threshold": 80},
    )


class _Session:
    """commit()이 설정된 side-effect를 내는 최소 AsyncSession 대역.

    ``commit_exc`` 는 매번 던지는 예외, ``fail_first_n_commits`` 는 처음 N회만
    던지고 이후 성공하는 일시 장애 시뮬레이션이다.
    """

    def __init__(
        self,
        commit_exc: Exception | None = None,
        fail_first_n_commits: int = 0,
    ):
        self._commit_exc = commit_exc
        self._fail_left = fail_first_n_commits
        self.added: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("transient db error")
        if self._commit_exc is not None:
            raise self._commit_exc

    async def rollback(self):
        pass


def _session_factory_with(
    commit_exc: Exception | None = None, fail_first_n_commits: int = 0
):
    sessions: list[_Session] = []

    def factory() -> _Session:
        s = _Session(commit_exc, fail_first_n_commits)
        sessions.append(s)
        return s

    factory.sessions = sessions  # type: ignore[attr-defined]
    return factory


def _handler(commit_exc=None, send_fail=False, fail_first_n_commits=0):
    config = MagicMock()
    config.enabled = True
    config.recipient_roles = [RecipientRole.ADMIN]

    config_cache = MagicMock()
    config_cache.get.return_value = config

    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        return_value=[
            Recipient(
                email="admin@x.com",
                name="Admin",
                user_id="u9",
                role=RecipientRole.ADMIN,
            )
        ]
    )

    template_engine = MagicMock()
    template_engine.render.return_value = ("subject", "<html/>")

    sender = MagicMock()
    sender.send = AsyncMock(
        side_effect=RuntimeError("smtp down") if send_fail else None
    )

    async def _execute(fn, event_type=None):
        return await fn()

    retry_executor = MagicMock()
    retry_executor.execute = AsyncMock(side_effect=_execute)

    factory = _session_factory_with(commit_exc, fail_first_n_commits)
    handler = BudgetHandler(
        session_factory=factory,
        config_cache=config_cache,
        recipient_resolver=resolver,
        template_engine=template_engine,
        email_sender=sender,
        retry_executor=retry_executor,
        metrics=None,
    )
    return handler, sender, factory


@pytest.mark.asyncio
async def test_duplicate_event_is_skipped_without_resending():
    """같은 (event_id, recipient) 의 두 번째 배송 시도는 send 없이 스킵된다.

    유니크 인덱스가 있는 실 DB 에서는 두 번째 INSERT commit 이 IntegrityError —
    핸들러는 이를 잡아 continue 해야 한다 (메일 재발송 방지가 수정의 본체).
    """
    handler, sender, _ = _handler(commit_exc=IntegrityError("dup", {}, Exception()))
    await handler.handle(_event())
    sender.send.assert_not_called()


@pytest.mark.asyncio
async def test_first_delivery_still_sends_and_logs():
    """대조군 — dedup 게이트가 정상 배송까지 막으면 알림이 0건이 된다."""
    handler, sender, factory = _handler()
    await handler.handle(_event())
    sender.send.assert_awaited_once()
    # 로그 행이 만들어지고 commit 됐어야 한다.
    assert factory.sessions and factory.sessions[0].added


@pytest.mark.asyncio
async def test_generic_db_error_does_not_block_send():
    """슬롯 선점 재시도까지 실패하는 지속 DB 장애 — 발송을 우선한다(유실<중복)."""
    handler, sender, _ = _handler(commit_exc=RuntimeError("db down"))
    await handler.handle(_event())
    sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_slot_claim_retried_then_sends():
    """일시 DB 실패는 슬롯 선점을 1회 재시도한다 (R2-6).

    첫 commit 만 실패하면 두 번째 시도에서 슬롯이 잡혀, 후속 재배송 시
    IntegrityError dedup 이 그대로 동작한다 — 예전엔 슬롯 없이 발송해
    재배송 때 중복 메일이 나갔다.
    """
    handler, sender, factory = _handler(fail_first_n_commits=1)
    await handler.handle(_event())
    sender.send.assert_awaited_once()
    # 슬롯 선점 재시도 + 최종 상태 commit — 세션 하나에서 commit 2회 이상 시도.
    assert factory.sessions[0].commits >= 2
