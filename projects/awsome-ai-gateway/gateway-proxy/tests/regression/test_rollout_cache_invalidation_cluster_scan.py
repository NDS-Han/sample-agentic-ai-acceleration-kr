"""Redis Cluster SCAN 커서 회귀 — rollout 캐시 무효화 스크립트."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from redis.cluster import parse_scan_result

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "us-llm-gateway"
    / "update-scripts"
    / "23-invalidate-app-budget-config-cache.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("_rollout_cache_invalidation", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Pipeline:
    def __init__(self, redis):
        self._redis = redis
        self._keys = []

    def pttl(self, key):
        self._keys.append(key)

    async def execute(self):
        return [self._redis.pttl_values.get(key, 300_000) for key in self._keys]


class _FakeClusterRedis:
    def __init__(self, batches, data):
        self._primaries = [SimpleNamespace(name="node-a"), SimpleNamespace(name="node-b")]
        self._batches = batches
        self.data = dict(data)
        self.scan_calls = []
        self.deleted = []
        self.pttl_values = {}

    async def initialize(self):
        return self

    def get_primaries(self):
        return list(self._primaries)

    async def scan(self, cursor=0, match=None, count=None, target_nodes=None):
        node = target_nodes
        client = match.rsplit(":", 1)[-1]
        self.scan_calls.append((node.name, client, cursor, count))
        next_cursor, keys = self._batches[(node.name, client, cursor)]
        return parse_scan_result("SCAN", {node.name: [next_cursor, list(keys)]})

    def pipeline(self, transaction=False):
        return _Pipeline(self)

    async def delete(self, key):
        self.deleted.append(key)
        if key in self.data:
            del self.data[key]
            return 1
        return 0


class _FakeStandaloneRedis:
    async def scan(self, cursor=0, match=None, count=None):
        return 4, ["next-key"]


class _WrongNodeCursorRedis:
    async def scan(self, cursor=0, match=None, count=None, target_nodes=None):
        return ({"other-node": 0}, [])


def _exact(user_id, client):
    return f"budget:config:user:{{{user_id}}}:{client}"


def _cluster_fixture():
    user_a = "11111111-1111-4111-8111-111111111111"
    user_b = "22222222-2222-4222-8222-222222222222"
    user_c = "33333333-3333-4333-8333-333333333333"
    exact_a = _exact(user_a, "claude-code")
    exact_a2 = _exact(user_b, "claude-code")
    exact_b = _exact(user_c, "claude-code")
    cowork_a = _exact(user_a, "cowork")
    cowork_b = _exact(user_b, "cowork")
    malformed = "budget:config:user:{not-a-uuid}:claude-code"
    retained = {
        f"budget:user:{{{user_a}}}:claude-code:2026-11": "10",
        f"budget:pending:client:{{{user_a}}}:claude-code:req-1": "1.25",
        f"budget:resvsum:user:{{{user_a}}}:claude-code:2026-11": "1.25",
        f"budget:config:user:{{{user_a}}}:claude-code:none": "1",
        f"budget:config:user:{{{user_a}}}:codex": "keep-codex",
        malformed: "keep-malformed",
    }
    data = {
        exact_a: "config-a",
        exact_a2: "config-a2",
        exact_b: "config-b",
        cowork_a: "cowork-a",
        cowork_b: "cowork-b",
        **retained,
    }
    batches = {
        ("node-a", "claude-code", 0): (17, [exact_a, malformed]),
        ("node-a", "claude-code", 17): (0, [exact_a2]),
        ("node-b", "claude-code", 0): (11, [exact_b]),
        ("node-b", "claude-code", 11): (0, []),
        ("node-a", "cowork", 0): (5, [cowork_a]),
        ("node-a", "cowork", 5): (0, []),
        ("node-b", "cowork", 0): (0, [cowork_b]),
    }
    exact_keys = {exact_a, exact_a2, exact_b, cowork_a, cowork_b}
    return _FakeClusterRedis(batches, data), exact_keys, retained


def test_cluster_cursor_selector_uses_requested_node_entry():
    module = _load_script()

    assert module._scan_cursor_value(
        {"node-a": 17, "node-b": 23}, SimpleNamespace(name="node-b")
    ) == 23


@pytest.mark.asyncio
async def test_cluster_scan_batch_reads_requested_node_cursor_from_redis_py_dict_shape():
    module = _load_script()
    redis, _, _ = _cluster_fixture()
    node = redis.get_primaries()[0]

    cursor, keys = await module._scan_batch(
        redis,
        cursor=0,
        pattern="budget:config:user:{*}:claude-code",
        count=37,
        target_node=node,
    )

    assert cursor == 17
    assert keys[0] == _exact("11111111-1111-4111-8111-111111111111", "claude-code")


@pytest.mark.asyncio
async def test_cluster_scan_continues_all_primaries_and_dry_run_writes_nothing():
    module = _load_script()
    redis, exact_keys, retained = _cluster_fixture()

    summary = await module._scan_targets(redis, mode="cluster", apply=False, scan_count=37)

    assert summary["selected"] == 5
    assert summary["deleted"] == 0
    assert summary["clients"]["claude-code"]["selected"] == 3
    assert summary["clients"]["claude-code"]["invalid_shape"] == 1
    assert summary["clients"]["cowork"]["selected"] == 2
    assert redis.scan_calls == [
        ("node-a", "claude-code", 0, 37),
        ("node-a", "claude-code", 17, 37),
        ("node-b", "claude-code", 0, 37),
        ("node-b", "claude-code", 11, 37),
        ("node-a", "cowork", 0, 37),
        ("node-a", "cowork", 5, 37),
        ("node-b", "cowork", 0, 37),
    ]
    assert redis.deleted == []
    assert exact_keys | set(retained) <= set(redis.data)


@pytest.mark.asyncio
async def test_cluster_apply_deletes_only_validated_config_keys():
    module = _load_script()
    redis, exact_keys, retained = _cluster_fixture()

    summary = await module._scan_targets(redis, mode="cluster", apply=True, scan_count=37)

    assert summary["selected"] == 5
    assert summary["deleted"] == 5
    assert set(redis.deleted) == exact_keys
    assert exact_keys.isdisjoint(redis.data)
    for key, value in retained.items():
        assert redis.data[key] == value


@pytest.mark.asyncio
async def test_cluster_scan_cursor_missing_requested_node_fails_closed():
    module = _load_script()
    node = SimpleNamespace(name="requested-node")

    with pytest.raises(RuntimeError, match="requested-node"):
        await module._scan_batch(
            _WrongNodeCursorRedis(),
            cursor=0,
            pattern="budget:config:user:{*}:claude-code",
            count=200,
            target_node=node,
        )


@pytest.mark.asyncio
async def test_standalone_scan_scalar_cursor_shape_is_still_accepted():
    module = _load_script()

    cursor, keys = await module._scan_batch(
        _FakeStandaloneRedis(), cursor=0, pattern="budget:config:user:{*}:cowork", count=200
    )

    assert cursor == 4
    assert keys == ["next-key"]
