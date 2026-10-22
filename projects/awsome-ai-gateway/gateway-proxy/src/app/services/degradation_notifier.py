# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Degradation 전이 → ``notifications:system`` 채널 이벤트 발행.

왜 존재하는가. notification-worker 는 이 채널을 구독하고 SystemHandler·
템플릿(degradation_mode/service_health_change/provider_error)·시드
(notification_configs, recipient "admin")가 전부 준비돼 있지만, 생산자가
없어 관리자는 열화 진입을 메일로 받지 못했다. DegradationManager._transition_to
가 전이의 단일 통로이므로 여기서만 발행하면 전이 감지가 빠지는 경로가 없다.

매핑:
  - 열화 심화(열화 축 수 증가)          → ``degradation_mode``
  - 열화 완화/복구(열화 축 수 감소)    → ``service_health_change``

⚠️ 전이는 HEALTHY↔DB↔REDIS↔BOTH 조합으로만 일어난다 — 축 수가 같고 다른
   전이(예: DB→REDIS 직행)는 상태머신상 도달 불가.

발행은 fire-and-forget 백그라운드 태스크 — _transition_to 가 동기 컨텍스트라
await 할 수 없고, 발행 실패로 전이 자체를 지연/실패시키면 안 된다. 알림 유실
허용(pub/sub 채널 특성상 원래 lossy)이며, 실패는 로그로 남긴다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.schemas.domain import DegradationLevel

logger = logging.getLogger(__name__)

CHANNEL = "notifications:system"

#: 같은 회로(provider_model_id+scope)의 provider_error 알림 재발행 최소 간격.
#: OPEN 상태에서 실패가 계속되면 record_failure 가 요청마다 True 를 돌려주므로
#: 여기서 pod 로컬로 한 번 자르고, worker 쿨다운(600s, payload.provider dedup)이
#: 멀티 파드 재발행을 최종 억제한다.
_PROVIDER_NOTIFY_MIN_INTERVAL_SEC = 300.0

# DegradationLevel → 열화된 축 집합. 열화 정도는 이 집합의 크기로 비교한다.
_DEGRADED_AXES: dict[DegradationLevel, frozenset[str]] = {
    DegradationLevel.HEALTHY: frozenset(),
    DegradationLevel.DB_DEGRADED: frozenset({"db"}),
    DegradationLevel.REDIS_DEGRADED: frozenset({"redis"}),
    DegradationLevel.BOTH_DEGRADED: frozenset({"db", "redis"}),
}

_AXIS_LABEL = {
    "db": "database",
    "redis": "redis",
}


class DegradationNotifier:
    """DegradationManager 전이 콜백 — Redis Pub/Sub 알림 발행자."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        # fire-and-forget 태스크 강한 참조 — bare create_task 는 GC 되면
        # 조용히 죽는다(health_checker._bg_tasks 와 같은 이유).
        self._tasks: set[asyncio.Task] = set()
        # 회로별 마지막 provider_error 발행 시각(pod 로컬 선제 dedup).
        self._provider_notify_at: dict[str, float] = {}

    def on_transition(self, old: DegradationLevel, new: DegradationLevel) -> None:
        """DegradationManager.set_transition_listener 에 등록되는 동기 콜백.

        실행 컨텍스트는 health checker 코루틴 안이라 항상 running loop 가
        있다. 혹시 루프 밖(테스트/도구)에서 전이가 일어나면 알림만 건너뛴다 —
        전이 자체에는 영향을 주지 않는다.
        """
        try:
            task = asyncio.get_running_loop().create_task(self._publish(old, new))
        except RuntimeError:
            logger.warning("degradation_notify_no_event_loop")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def notify_provider_error(
        self, *, circuit_key: str, pmid: str, status: int | str
    ) -> None:
        """provider 회로가 OPEN 으로 넘어간 시점의 알림 (provider_error 이벤트).

        fallback_loop 에서 ``cb.record_failure`` 가 회로를 연 반환(True)일 때
        호출된다. pod 로컬 dedup 으로 OPEN 지속 중 요청당 1건 발행을 막고,
        payload.provider 로 worker 쿨다운이 멀티 파드 재발행을 억제한다.
        """
        now = time.monotonic()
        last = self._provider_notify_at.get(circuit_key)
        if last is not None and now - last < _PROVIDER_NOTIFY_MIN_INTERVAL_SEC:
            return
        self._provider_notify_at[circuit_key] = now

        payload = {
            "provider_name": pmid,
            "error_type": "circuit_open",
            "error_message": f"upstream failures opened circuit (status={status})",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            # worker 쿨다운 dedup 정체성 필드.
            "provider": circuit_key,
            "circuit_key": circuit_key,
        }
        try:
            task = asyncio.get_running_loop().create_task(
                self._publish_event("provider_error", payload)
            )
        except RuntimeError:
            logger.warning("provider_error_notify_no_event_loop")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish_event(self, event_type: str, payload: dict) -> None:
        event = {
            "event_id": str(uuid4()),
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "gateway-proxy",
            "payload": payload,
        }
        try:
            await self._redis.publish(CHANNEL, json.dumps(event))
            logger.info("notification_event_published", event_type=event_type)
        except Exception:
            logger.warning("notification_event_publish_failed", event_type=event_type)

    async def _publish(self, old: DegradationLevel, new: DegradationLevel) -> None:
        old_axes = _DEGRADED_AXES[old]
        new_axes = _DEGRADED_AXES[new]
        worsened = len(new_axes) > len(old_axes)

        # 새로 열화된 축(worsened) 또는 복구된 축(recovered) — 템플릿이
        # 읽는 service/affected_providers 필드의 출처.
        delta_axes = (new_axes - old_axes) if worsened else (old_axes - new_axes)
        service = "+".join(sorted(delta_axes)) or "+".join(sorted(new_axes or old_axes))
        affected = "+".join(_AXIS_LABEL[a] for a in sorted(new_axes)) or "none"

        if worsened:
            event_type = "degradation_mode"
            payload: dict[str, Any] = {
                "mode": new.value,
                "reason": f"{service} health check failures exceeded threshold",
                "affected_providers": affected,
                # 쿨다운 dedup 정체성(notification-worker base.py _DEDUP_PAYLOAD_FIELDS) —
                # 플래핑 재전이가 같은 축의 알림을 cooldown 창 안에서 억제하게 한다.
                "service": service,
                "previous_level": old.value,
            }
        else:
            event_type = "service_health_change"
            payload = {
                "service_name": service,
                "status": (
                    "recovered"
                    if new is DegradationLevel.HEALTHY
                    else "partially_recovered"
                ),
                "previous_status": old.value,
                "detail": f"degradation level {old.value} → {new.value}",
                "service": service,
            }

        # notification-worker NotificationEvent 봉투 — envelope 4필드 + payload
        # (SecurityEvent.to_envelope/batch_flusher threshold 발행과 같은 규약).
        event = {
            "event_id": str(uuid4()),
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "gateway-proxy",
            "payload": payload,
        }
        try:
            await self._redis.publish(CHANNEL, json.dumps(event))
            logger.info(
                "degradation_event_published",
                event_type=event_type,
                from_level=old.value,
                to_level=new.value,
            )
        except Exception:
            # 알림 유실 허용 — 발행 실패가 전이 루프를 해치면 안 된다.
            logger.warning(
                "degradation_event_publish_failed",
                event_type=event_type,
            )
