# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""admin-api → gateway model/catalog cache invalidation (wave 1d)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.core.cache_invalidation import (
    GATEWAY_AUTH_INVALIDATION_CHANNEL,
    GATEWAY_MODEL_INVALIDATION_CHANNEL,
    CacheInvalidationManager,
    _model_invalidation_payload,
    publish_gateway_model_invalidation,
)


def _redis():
    r = MagicMock()
    r.delete = AsyncMock()
    r.publish = AsyncMock()

    async def _scan(match=None, count=None):
        for k in ():
            yield k

    r.scan_iter = _scan
    return r


def _model_msgs(redis):
    return [json.loads(c.args[1]) for c in redis.publish.await_args_list
            if c.args[0] == GATEWAY_MODEL_INVALIDATION_CHANNEL]


def test_payload_maps_model_keys():
    p = _model_invalidation_payload(
        ["model:claude-sonnet-4-6", "model:pmid:global.anthropic.x", "model:list",
         "model:nx:old-ref"], pattern="model:nx:*:new-ref")
    assert p == {"model_refs": ["claude-sonnet-4-6", "global.anthropic.x", "new-ref", "old-ref"],
                 "model_list": True, "routing_clients": []}


def test_payload_maps_routing_profile_keys():
    p = _model_invalidation_payload(["routing_profile:cowork", "routing_profile:cowork:stale"])
    assert p == {"model_refs": [], "model_list": False, "routing_clients": ["cowork"]}


def test_payload_ignores_unrelated_and_wildcards():
    assert _model_invalidation_payload(["key:vk:aaa", "budget:config:team:{t}"]) is None
    assert _model_invalidation_payload(pattern="model:*") is None
    assert _model_invalidation_payload(pattern="routing_profile:*") is None


async def test_invalidate_publishes_model_message_after_delete():
    redis = _redis()
    order = []
    redis.delete.side_effect = lambda k: order.append(("del", k))
    redis.publish.side_effect = lambda ch, _p: order.append(("pub", ch))
    await CacheInvalidationManager(redis).invalidate(
        ["model:claude-sonnet-4-6", "model:pmid:pm", "model:list"])
    assert order[-1] == ("pub", GATEWAY_MODEL_INVALIDATION_CHANNEL)
    assert all(op == "del" for op, _ in order[:3])
    assert _model_msgs(redis) == [{"model_refs": ["claude-sonnet-4-6", "pm"], "model_list": True,
                                   "routing_clients": []}]


async def test_invalidate_publishes_even_when_delete_fails():
    redis = _redis()
    redis.delete.side_effect = ConnectionError("down")
    await CacheInvalidationManager(redis).invalidate(["model:a"])
    assert _model_msgs(redis) == [{"model_refs": ["a"], "model_list": False,
                                   "routing_clients": []}]


async def test_auth_keys_still_go_to_auth_channel_only():
    redis = _redis()
    await CacheInvalidationManager(redis).invalidate(["key:vk:aaa"])
    channels = [c.args[0] for c in redis.publish.await_args_list]
    assert channels == [GATEWAY_AUTH_INVALIDATION_CHANNEL]


async def test_invalidate_pattern_publishes_nx_ref():
    redis = _redis()
    await CacheInvalidationManager(redis).invalidate_pattern("model:nx:*:new-model")
    assert _model_msgs(redis) == [{"model_refs": ["new-model"], "model_list": False,
                                   "routing_clients": []}]


async def test_retry_republishes():
    redis = _redis()
    mgr = CacheInvalidationManager(redis)
    await mgr._retry_one(SimpleNamespace(cache_key="model:list", context=None))
    await mgr._retry_one(SimpleNamespace(cache_key="pattern:model:nx:*:r", context=None))
    assert _model_msgs(redis) == [
        {"model_refs": [], "model_list": True, "routing_clients": []},
        {"model_refs": ["r"], "model_list": False, "routing_clients": []},
    ]


async def test_publish_failure_is_swallowed():
    redis = _redis()
    redis.publish.side_effect = ConnectionError("down")
    await publish_gateway_model_invalidation(redis, ["model:a"])  # no raise


async def test_routing_profile_service_invalidation_publishes():
    from app.services.routing_profile_service import RoutingProfileService

    redis = _redis()
    svc = RoutingProfileService.__new__(RoutingProfileService)
    svc._cache_mgr = CacheInvalidationManager(redis)
    svc._session = None
    await svc.invalidate_cache("codex")
    assert _model_msgs(redis) == [{"model_refs": [], "model_list": False,
                                   "routing_clients": ["codex"]}]
