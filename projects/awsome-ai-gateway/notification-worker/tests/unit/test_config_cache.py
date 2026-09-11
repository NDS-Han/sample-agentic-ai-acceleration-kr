# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ConfigCache 단위 테스트."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.models.notification import NotificationConfig
from worker.services.config_cache import ConfigCache


def _make_config(event_type: str, enabled: bool = True) -> NotificationConfig:
    cfg = MagicMock(spec=NotificationConfig)
    cfg.event_type = event_type
    cfg.enabled = enabled
    cfg.recipient_roles = ["affected_user"]
    return cfg


async def test_load_populates_cache() -> None:
    """load()는 DB에서 모든 NotificationConfig를 로드한다."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [
        _make_config("budget_threshold"),
        _make_config("key_expiring"),
    ]
    session.execute = AsyncMock(return_value=result_mock)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    cache = ConfigCache(factory)
    await cache.load()

    assert cache.get("budget_threshold") is not None
    assert cache.get("key_expiring") is not None
    assert cache.get("nonexistent") is None


async def test_get_returns_none_before_load() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    assert cache.get("budget_threshold") is None


async def test_reload_updates_existing_cache() -> None:
    session = AsyncMock()

    # 첫 번째 load: budget_threshold만 존재
    result1 = MagicMock()
    result1.scalars.return_value.all.return_value = [_make_config("budget_threshold")]

    # reload (두 번째 load): key_expiring 추가
    result2 = MagicMock()
    result2.scalars.return_value.all.return_value = [
        _make_config("budget_threshold"),
        _make_config("key_expiring"),
    ]

    session.execute = AsyncMock(side_effect=[result1, result2])
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    cache = ConfigCache(factory)
    await cache.load()
    assert cache.get("key_expiring") is None

    await cache.reload()
    assert cache.get("key_expiring") is not None


def test_needs_poll_returns_true_initially() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    # 로드 이력이 없으면 무조건 True — 시계와 무관해야 한다.
    assert cache.needs_poll() is True


def test_needs_poll_is_true_right_after_boot() -> None:
    """⚠️ 회귀 가드: 호스트 uptime 이 5분 미만이어도 True 여야 한다.

    예전 구현은 sentinel 을 0.0 으로 두고 `time.monotonic() - 0.0 > 300` 을 봤다.
    monotonic 의 기준점은 임의(리눅스에서는 부팅 시각)이므로, 방금 뜬 노드에서는
    `monotonic()` 자체가 300 미만이라 **한 번도 로드하지 않은 캐시가 "폴링 불필요"**
    를 반환했다. 그러면 Pub/Sub 갱신이 오기 전까지 빈 설정으로 돈다.

    이 결함은 uptime 이 긴 개발 머신에서는 재현되지 않는다(실측: 로컬 94,643s 통과,
    CI 러너에서 실패). 그래서 monotonic 을 부팅 직후 값으로 고정해 재현한다.
    """
    factory = MagicMock()
    cache = ConfigCache(factory)
    with patch.object(time, "monotonic", return_value=12.0):  # 부팅 12초 후
        assert cache.needs_poll() is True, (
            "부팅 직후 uptime 이 짧으면 폴링이 꺼진다 — sentinel 을 monotonic 과 "
            "같은 축에서 비교하면 안 된다(None 을 쓸 것)"
        )


def test_needs_poll_is_false_right_after_boot_when_just_loaded() -> None:
    """대조군 — 위 가드가 '항상 True' 로 통과하지 않는지."""
    factory = MagicMock()
    cache = ConfigCache(factory)
    with patch.object(time, "monotonic", return_value=12.0):
        cache._last_loaded = 12.0
        assert cache.needs_poll() is False


def test_needs_poll_returns_false_after_recent_load() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    cache._last_loaded = time.monotonic()
    assert cache.needs_poll() is False
