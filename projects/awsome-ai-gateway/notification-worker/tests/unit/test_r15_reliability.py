# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R15 알림 워커 신뢰성 회귀 — 버퍼 배선, 쿨다운 dedup, 스위퍼, 템플릿 폴백.

Kiro R15 리뷰가 확인한 실물 결함들:
- NotificationBufferQueue.enqueue 를 부르는 코드가 없어 DB 장애 시 이벤트가
  조용히 유실됐다 (리졸버가 DB 오류를 삼키고 [] 반환 → "수신자 없음" 폐기).
- 카운터형 보안 이벤트 폭주를 막는 쿨다운이 없었다.
- 'pending' dead-claim / 'failed' 행은 재발행 없이는 영구 미발송이었다.
- locale=en 에서 로케일 템플릿 없는 타입이 한국어 비로케 템플릿으로 나갔다.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from worker.handlers.base import NotificationInfraError
from worker.handlers.budget_handler import BudgetHandler
from worker.listeners.pending_sweeper import PendingSweepTask
from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.schemas.recipients import Recipient, RecipientRole
from worker.services.template_engine import TemplateEngine


def _event(payload: dict | None = None, event_id: str = "e1") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        type=EventType.BUDGET_THRESHOLD,
        timestamp=datetime.now(UTC),
        source=ServiceSource.COST_RECORDER_WORKER,
        payload=payload if payload is not None else {"user_id": "u1", "threshold_pct": 80},
    )


def _make_handler(
    *,
    session_factory=None,
    resolver=None,
    sender=None,
    redis_client=None,
    cooldown_seconds: int = 0,
    config_enabled: bool = True,
):
    config = MagicMock()
    config.enabled = config_enabled
    config.recipient_roles = [RecipientRole.ADMIN]
    config_cache = MagicMock()
    config_cache.get.return_value = config

    if resolver is None:
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

    if sender is None:
        sender = MagicMock()
        sender.send = AsyncMock()

    async def _execute(fn, event_type=None):
        return await fn()

    retry_executor = MagicMock()
    retry_executor.execute = AsyncMock(side_effect=_execute)

    if session_factory is None:
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx)

    handler = BudgetHandler(
        session_factory=session_factory,
        config_cache=config_cache,
        recipient_resolver=resolver,
        template_engine=template_engine,
        email_sender=sender,
        retry_executor=retry_executor,
        metrics=None,
        redis_client=redis_client,
        cooldown_seconds=cooldown_seconds,
    )
    return handler, sender


# ── 버퍼 배선 (F1) ──────────────────────────────────────────────────────────


async def test_resolver_db_error_raises_infra_error():
    """수신자 조회 중 DB 장애는 NotificationInfraError — 리스너가 버퍼에 넣는다.

    예전엔 리졸버가 역할 단위로 삼켜 resolve() 가 [] 를 반환 → 핸들러가
    "수신자 없음"으로 이벤트를 폐기 → Pub/Sub 재전송 없으므로 영구 유실.
    """
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        side_effect=OperationalError("conn", {}, Exception())
    )
    handler, _ = _make_handler(resolver=resolver)

    with pytest.raises(NotificationInfraError):
        await handler.handle(_event())


async def test_all_recipients_infra_failure_raises():
    """전 수신자가 DB 오류로 실패하면 이벤트 전체를 버퍼로 되돌린다.

    일부만 실패면 성공분은 발송됐고 실패분은 pending 행으로 남아 sweeper 가
    회수하므로, raise 는 "아무것도 전달 못 했을 때"만이다.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(
        side_effect=OperationalError("conn", {}, Exception())
    )
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    handler, sender = _make_handler(session_factory=factory)

    with pytest.raises(NotificationInfraError):
        await handler.handle(_event())
    sender.send.assert_not_called()


async def test_partial_non_infra_failure_does_not_raise():
    """수신자 한 명의 일반 실패는 이벤트를 버퍼로 돌리지 않는다."""
    recipients = [
        Recipient(email="a@x.com", name="A", user_id="u1", role=RecipientRole.ADMIN),
        Recipient(email="b@x.com", name="B", user_id="u2", role=RecipientRole.ADMIN),
    ]
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=recipients)

    ok_ctx = MagicMock()
    ok_session = MagicMock()
    ok_session.add = MagicMock()
    ok_session.commit = AsyncMock()
    ok_ctx.__aenter__ = AsyncMock(return_value=ok_session)
    ok_ctx.__aexit__ = AsyncMock(return_value=False)

    bad_ctx = MagicMock()
    bad_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
    bad_ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(side_effect=[bad_ctx, ok_ctx])
    handler, sender = _make_handler(session_factory=factory, resolver=resolver)

    await handler.handle(_event())  # raise 없음
    sender.send.assert_awaited_once()


# ── 쿨다운 dedup (F2) ────────────────────────────────────────────────────────


async def test_cooldown_suppresses_same_identity_burst():
    """같은 정체성(payload 필드 동일) 이벤트는 쿨다운 창 안에서 1회만 발송한다."""
    redis = MagicMock()
    # 클레임 Lua: 1=점유, 0=억제 (R23 r3 N-8 — SET NX+GET 을 단일 eval 로
    # 묶어 만료 마이크로 창을 닫았다).
    redis.eval = AsyncMock(side_effect=[1, 0])

    handler, sender = _make_handler(
        redis_client=redis, cooldown_seconds=600
    )
    await handler.handle(_event(event_id="e1"))
    await handler.handle(_event(event_id="e2"))  # 새 event_id 지만 같은 정체성

    sender.send.assert_awaited_once()  # 두 번째는 쿨다운 스킵
    assert redis.eval.await_count == 2  # 둘 다 선점을 시도했다


async def test_cooldown_fanout_same_event_id_proceeds():
    """같은 event_id 의 fan-out 복사본은 억제하지 않는다 — 슬롯 유니크가 막는다."""
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=[1, 2])  # 1=점유, 2=같은 event_id 통과

    handler, sender = _make_handler(
        redis_client=redis, cooldown_seconds=600
    )
    await handler.handle(_event(event_id="e1"))
    await handler.handle(_event(event_id="e1"))  # fan-out 복사본

    assert sender.send.await_count == 2  # 둘 다 진행 — dedup 은 슬롯의 몫


async def test_cooldown_distinct_identity_not_suppressed():
    """threshold 가 다르면 다른 알림 — 각각 발송된다."""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)  # 둘 다 점유 성공

    handler, sender = _make_handler(
        redis_client=redis, cooldown_seconds=600
    )
    await handler.handle(_event({"user_id": "u1", "threshold_pct": 80}, "e1"))
    await handler.handle(_event({"user_id": "u1", "threshold_pct": 90}, "e2"))

    assert sender.send.await_count == 2
    # 서로 다른 dedup 키로 선점됐는지 확인 — eval(script, nkeys, key, ...)
    keys = [c.args[2] for c in redis.eval.await_args_list]
    assert keys[0] != keys[1]


async def test_cooldown_key_released_on_infra_failure():
    """인프라 실패 시 선점 키를 CAS 해제한다 — 안 놓으면 버퍼 드레인의 재처리가
    창 내내 억제돼 유실된다."""
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=[1, 1])  # 클레임=점유, 해제 CAS=삭제됨

    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        side_effect=OperationalError("conn", {}, Exception())
    )
    handler, _ = _make_handler(
        resolver=resolver, redis_client=redis, cooldown_seconds=600
    )

    with pytest.raises(NotificationInfraError):
        await handler.handle(_event())
    # 두 eval — 첫째는 클레임(nx), 둘째는 compare-and-del(del)+event_id
    assert redis.eval.await_count == 2
    release = redis.eval.await_args_list[-1].args
    assert "del" in release[0] and "e1" in release


async def test_cooldown_redis_error_fails_open():
    """dedup 체크 자체가 실패해도 발송은 진행 — 중복 알림이 유실보다 낫다."""
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))

    handler, sender = _make_handler(
        redis_client=redis, cooldown_seconds=600
    )
    await handler.handle(_event())
    sender.send.assert_awaited_once()


async def test_no_dedup_fields_means_no_cooldown():
    """정체성 필드가 하나도 없는 이벤트는 dedup 불가 — 억제하지 않는다."""
    redis = MagicMock()
    redis.eval = AsyncMock()

    handler, sender = _make_handler(
        redis_client=redis, cooldown_seconds=600
    )
    await handler.handle(_event({"note": "first"}, "e1"))
    await handler.handle(_event({"note": "second"}, "e2"))

    redis.eval.assert_not_called()
    assert sender.send.await_count == 2


# ── 스위퍼 (F3/F5) ──────────────────────────────────────────────────────────


def _claim_factory(rows: list[dict | None]):
    """_claim_one 용 세션 팩토리 — 호출마다 'abandoned' UPDATE + 클레임
    UPDATE(mappings().first() 순차 반환) 순으로 execute 가 호출된다."""
    session = MagicMock()
    effects: list = []
    for r in rows:
        effects.append(MagicMock())  # abandoned UPDATE
        first = MagicMock(return_value=r)
        result = MagicMock()
        result.mappings.return_value = MagicMock(first=first)
        effects.append(result)
    session.execute = AsyncMock(side_effect=effects)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


async def test_sweeper_routes_claimed_rows_to_handler():
    """클레임된 행은 event_type 에 맞는 핸들러의 resend_log_entry 로 간다."""
    factory = _claim_factory(
        [
            {"id": "log-1", "event_type": "budget_threshold", "last_attempt_at": None},
            None,
        ]
    )

    handler = MagicMock()
    handler.resend_log_entry = AsyncMock()

    sweeper = PendingSweepTask(
        session_factory=factory,
        event_type_handler_map={"budget_threshold": handler},
        sweep_interval=9999,
    )
    resent = await sweeper._sweep()

    assert resent == 1
    handler.resend_log_entry.assert_awaited_once()


async def test_sweeper_claims_nothing_when_empty():
    factory = _claim_factory([None])
    sweeper = PendingSweepTask(
        session_factory=factory, event_type_handler_map={}, sweep_interval=9999
    )
    assert await sweeper._sweep() == 0


async def test_resend_log_entry_sends_and_marks_sent():
    """스위퍼가 'pending' 으로 클레임한 행은 새 슬롯 없이 재발송·확정된다."""
    log_row = MagicMock()
    log_row.status = "pending"
    log_row.event_id = "e1"
    log_row.event_type = "budget_threshold"
    log_row.recipient_email = "admin@x.com"
    log_row.recipient_user_id = None
    log_row.event_payload = {"user_id": "u1", "threshold_pct": 80}
    log_row.attempt_count = 1

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = log_row
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    handler, sender = _make_handler(session_factory=factory)
    await handler.resend_log_entry("log-1")

    sender.send.assert_awaited_once()
    assert log_row.status == "sent"


async def test_resend_log_entry_fencing_token_blocks_stolen_claim():
    """클레임 토큰이 어긋나면(다른 워커가 인수) 발송하지 않는다 — 이중 발송 방지."""
    session = MagicMock()
    fence_result = MagicMock()
    fence_result.scalar_one_or_none.return_value = None  # 토큰 불일치 → 인수 실패
    session.execute = AsyncMock(return_value=fence_result)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    handler, sender = _make_handler(session_factory=factory)
    await handler.resend_log_entry("log-1", claimed_at=datetime.now(UTC))

    sender.send.assert_not_called()


async def test_resend_log_entry_disabled_config_marks_skipped():
    """재발송 시점에 config 가 꺼져 있으면 'skipped' 터미널로 확정해
    sweeper 의 반복 회수를 멈춘다."""
    log_row = MagicMock()
    log_row.status = "pending"
    log_row.event_type = "budget_threshold"

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = log_row
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)

    handler, sender = _make_handler(
        session_factory=factory, config_enabled=False
    )
    await handler.resend_log_entry("log-1")

    sender.send.assert_not_called()
    assert log_row.status == "skipped"


# ── 리스너 버퍼 enqueue (F1) ────────────────────────────────────────────────


async def test_listener_buffers_infra_error():
    """핸들러의 NotificationInfraError 는 버퍼 enqueue 로 간다."""
    import json

    from worker.listeners.channel_listener import ChannelListener

    handler = MagicMock()
    handler.handle = AsyncMock(side_effect=NotificationInfraError("db down"))

    pubsub = AsyncMock()
    msgs = iter([
        {
            "type": "message",
            "data": json.dumps(
                {
                    "event_id": "e1",
                    "type": "budget_threshold",
                    "timestamp": "2026-10-13T00:00:00Z",
                    "source": "gateway-proxy",
                    "payload": {"user_id": "u1"},
                }
            ),
        }
    ])

    async def get_message(**_kw):
        try:
            return next(msgs)
        except StopIteration:
            raise asyncio.CancelledError()

    pubsub.get_message = get_message
    pubsub.ping = AsyncMock()
    mock_redis = MagicMock()
    mock_redis.pubsub.return_value = pubsub

    buffer = MagicMock()
    buffer.enqueue = AsyncMock(return_value=True)

    with (
        patch(
            "worker.listeners.channel_listener.get_redis_client",
            return_value=mock_redis,
        ),
        patch(
            "worker.listeners.channel_listener.get_notification_buffer",
            return_value=buffer,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        listener = ChannelListener("notifications:budget", handler)
        await listener.run()

    buffer.enqueue.assert_awaited_once()


# ── 템플릿 폴백 순서 (F11) ──────────────────────────────────────────────────


def _template_dir(templates: dict[str, str]) -> Path:
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    for name, content in templates.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def test_locale_default_beats_non_locale_specific():
    """locale=en + {type}.en 없음 → 한국어 비로케가 아니라 default.en 을 쓴다.

    예전 순서 {type}.en → {type}(한국어) → default.en 이라 en 설정 워커가
    로케일 템플릿 없는 이벤트를 한국어로 발송했다.
    """
    tdir = _template_dir(
        {
            "degradation_mode.subject.txt": "한국어 특정 제목",
            "degradation_mode.html": "<p>한국어 본문</p>",
            "default.en.subject.txt": "Default EN Subject",
            "default.en.html": "<p>Default EN Body</p>",
            "default.subject.txt": "비로케 기본",
            "default.html": "<p>비로케 기본</p>",
        }
    )
    engine = TemplateEngine(tdir)
    engine._locale = "en"

    subject, html = engine.render(
        "degradation_mode",
        {"event": None, "payload": {}, "recipient_name": "A",
         "recipient_email": "a@x", "gateway_name": "G",
         "timestamp_kr": "t", "locale": "en"},
    )
    assert subject == "Default EN Subject"
    assert "Default EN Body" in html


def test_ko_locale_non_locale_specific_beats_default_ko():
    """locale=ko(기본값) + {type}.ko 없음 → 비로케(한국어) 특정이 default.ko 보다 우선.

    폴백 순서를 en 관점으로만 고치면 ko 기본 환경에서 .ko 없는 7개 타입이
    범용 템플릿으로 뭉개지는 회귀 — 비로케 파일 자체가 한국어라 ko 에서는
    {type} 이 default.ko 보다 앞에 와야 한다.
    """
    tdir = _template_dir(
        {
            "degradation_mode.subject.txt": "한국어 특정 제목",
            "degradation_mode.html": "<p>한국어 특정 본문</p>",
            "default.ko.subject.txt": "기본 KO 제목",
            "default.ko.html": "<p>기본 KO 본문</p>",
            "default.subject.txt": "비로케 기본",
            "default.html": "<p>비로케 기본</p>",
        }
    )
    engine = TemplateEngine(tdir)
    engine._locale = "ko"

    subject, html = engine.render(
        "degradation_mode",
        {"event": None, "payload": {}, "recipient_name": "A",
         "recipient_email": "a@x", "gateway_name": "G",
         "timestamp_kr": "t", "locale": "ko"},
    )
    assert subject == "한국어 특정 제목"
    assert "한국어 특정 본문" in html


def test_locale_specific_still_wins():
    """{type}.en 이 있으면 그게 최우선 — 폴백 순서 변경이 정상 경로를 깨면 안 된다."""
    tdir = _template_dir(
        {
            "budget_threshold.en.subject.txt": "EN Specific",
            "budget_threshold.en.html": "<p>EN Specific Body</p>",
            "budget_threshold.subject.txt": "비로케 특정",
            "budget_threshold.html": "<p>비로케</p>",
            "default.en.subject.txt": "Default EN",
            "default.en.html": "<p>Default EN</p>",
        }
    )
    engine = TemplateEngine(tdir)
    engine._locale = "en"

    subject, html = engine.render(
        "budget_threshold",
        {"event": None, "payload": {}, "recipient_name": "A",
         "recipient_email": "a@x", "gateway_name": "G",
         "timestamp_kr": "t", "locale": "en"},
    )
    assert subject == "EN Specific"
    assert "EN Specific Body" in html
