# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""admin-api → gateway auth-cache invalidation (wave 1 hot-path isolation)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from app.core.cache_invalidation import (
    GATEWAY_AUTH_INVALIDATION_CHANNEL,
    CacheInvalidationManager,
    publish_gateway_auth_invalidation,
)


async def test_invalidate_publishes_vk_hashes_once():
    redis = MagicMock()
    redis.delete = AsyncMock()
    redis.publish = AsyncMock()
    await CacheInvalidationManager(redis).invalidate(
        ["key:vk:aaa", "key:cache:vk:aaa", "key:cache:vk:bbb", "user_context:u1"]
    )
    redis.publish.assert_awaited_once()
    channel, payload = redis.publish.await_args.args
    assert channel == GATEWAY_AUTH_INVALIDATION_CHANNEL
    assert json.loads(payload) == {"key_hashes": ["aaa", "bbb"], "user_ids": []}


async def test_no_publish_for_unrelated_keys():
    redis = MagicMock()
    redis.delete = AsyncMock()
    redis.publish = AsyncMock()
    await CacheInvalidationManager(redis).invalidate(["budget:downgrade:team:x"])
    redis.publish.assert_not_awaited()


async def test_publish_failure_is_swallowed():
    redis = MagicMock()
    redis.publish = AsyncMock(side_effect=ConnectionError("down"))
    await publish_gateway_auth_invalidation(redis, ["key:vk:h"], user_ids=["u1"])  # no raise
