# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""DegradationNotifier — 열화 전이 → notifications:system 발행 계약.

배경: notification-worker 는 이 채널을 구독하고 템플릿/시드가 있지만
생산자가 없어 degradation_mode/service_health_change 이벤트가 한 번도
전달되지 않았다.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.degradation.manager import FAIL_THRESHOLD, SUCCESS_THRESHOLD, DegradationManager
from app.schemas.domain import DegradationLevel
from app.services.degradation_notifier import CHANNEL, DegradationNotifier


class _Redis:
    def __init__(self) -> None:
        self.publish = AsyncMock()


async def _drain(notifier: DegradationNotifier) -> None:
    """fire-and-forget 발행 태스크를 모두 기다린다."""
    if notifier._tasks:
        await asyncio.wait(notifier._tasks.copy(), timeout=5)


def _published_events(redis: _Redis) -> list[dict]:
    return [
        json.loads(call.args[1])
        for call in redis.publish.await_args_list
        if call.args[0] == CHANNEL
    ]


@pytest.mark.asyncio
async def test_degradation_publishes_degradation_mode():
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    dm = DegradationManager()
    dm.set_transition_listener(notifier.on_transition)

    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED

    await _drain(notifier)
    events = _published_events(redis)
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "degradation_mode"
    assert evt["source"] == "gateway-proxy"
    assert evt["payload"]["mode"] == "REDIS_DEGRADED"
    assert evt["payload"]["service"] == "redis"
    assert evt["payload"]["affected_providers"] == "redis"


@pytest.mark.asyncio
async def test_recovery_publishes_service_health_change():
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    dm = DegradationManager()
    dm.set_transition_listener(notifier.on_transition)

    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    for _ in range(SUCCESS_THRESHOLD):
        dm.report_db_health(True)
    assert dm.level == DegradationLevel.HEALTHY

    await _drain(notifier)
    events = _published_events(redis)
    assert [e["type"] for e in events] == [
        "degradation_mode",
        "service_health_change",
    ]
    recovery = events[1]
    assert recovery["payload"]["status"] == "recovered"
    assert recovery["payload"]["previous_status"] == "DB_DEGRADED"
    assert recovery["payload"]["service_name"] == "db"


@pytest.mark.asyncio
async def test_partial_recovery_emits_health_change_not_degradation():
    """BOTH→REDIS(DB 복구)는 열화가 아니라 부분 복구 — health_change 여야."""
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    dm = DegradationManager()
    dm.set_transition_listener(notifier.on_transition)

    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.BOTH_DEGRADED

    # 발행은 백그라운드 태스크 — reset 전에 완료시켜 이전 전이 이벤트가 섞이지 않게 한다.
    await _drain(notifier)
    redis.publish.reset_mock()
    for _ in range(SUCCESS_THRESHOLD):
        dm.report_db_health(True)
    assert dm.level == DegradationLevel.REDIS_DEGRADED

    await _drain(notifier)
    events = _published_events(redis)
    assert len(events) == 1
    assert events[0]["type"] == "service_health_change"
    # 부분 복구 — 아직 열화 축이 남아 있으므로 status 는 recovered 가 아니다.
    assert events[0]["payload"]["status"] == "partially_recovered"
    assert events[0]["payload"]["service_name"] == "db"


@pytest.mark.asyncio
async def test_no_publish_without_transition():
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    dm = DegradationManager()
    dm.set_transition_listener(notifier.on_transition)

    dm.report_redis_health(True)  # healthy → healthy, 전이 없음
    await _drain(notifier)
    redis.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_transition():
    """발행 실패가 상태머신 전이를 방해하면 안 된다."""
    redis = _Redis()
    redis.publish.side_effect = ConnectionError("redis down")
    notifier = DegradationNotifier(redis)
    dm = DegradationManager()
    dm.set_transition_listener(notifier.on_transition)

    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED
    await _drain(notifier)  # 예외가 누수되지 않아야 한다


def test_listener_exception_does_not_break_manager():
    """콜백 자체가 죽어도 전이는 완료된다."""
    dm = DegradationManager()

    def _boom(old, new):
        raise RuntimeError("listener exploded")

    dm.set_transition_listener(_boom)
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED


# ─── provider_error (회로 OPEN 알림) ───


@pytest.mark.asyncio
async def test_provider_error_publishes_template_contract():
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    notifier.notify_provider_error(
        circuit_key="acct|anthropic.claude-x", pmid="anthropic.claude-x", status=503
    )
    await _drain(notifier)
    events = _published_events(redis)
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "provider_error"
    assert evt["source"] == "gateway-proxy"
    # provider_error.html 템플릿이 읽는 필드 계약.
    p = evt["payload"]
    assert p["provider_name"] == "anthropic.claude-x"
    assert p["error_type"] == "circuit_open"
    assert p["error_message"]
    assert p["occurred_at"]
    # worker 쿨다운 dedup 정체성 필드(_DEDUP_PAYLOAD_FIELDS 에 provider 존재).
    assert p["provider"] == "acct|anthropic.claude-x"


@pytest.mark.asyncio
async def test_provider_error_pod_local_dedup():
    """OPEN 지속 중 같은 회로의 재발행은 pod 로컬로 억제된다."""
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    for _ in range(5):
        notifier.notify_provider_error(
            circuit_key="pmid-a", pmid="pmid-a", status=502
        )
    await _drain(notifier)
    assert len(_published_events(redis)) == 1


@pytest.mark.asyncio
async def test_provider_error_dedup_is_per_circuit():
    redis = _Redis()
    notifier = DegradationNotifier(redis)
    notifier.notify_provider_error(circuit_key="pmid-a", pmid="pmid-a", status=502)
    notifier.notify_provider_error(
        circuit_key="acct|pmid-a", pmid="pmid-a", status=502
    )
    await _drain(notifier)
    # 회로 키가 다르면(다른 계정 스코프) 별개 알림.
    assert len(_published_events(redis)) == 2
