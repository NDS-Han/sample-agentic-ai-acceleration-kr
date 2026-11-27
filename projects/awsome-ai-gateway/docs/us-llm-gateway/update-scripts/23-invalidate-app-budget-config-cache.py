#!/usr/bin/env python3
"""Targeted rollout invalidation for app budget policy cache.

WHAT: delete only the legacy per-client budget config keys for ``claude-code`` and
      ``cowork`` after every gateway writer runs the layered app-group enforcement
      image. The next gateway request rehydrates those keys from PostgreSQL.
WHY:  mixed per-app/app-group policy enforcement changed the meaning of
      ``budget:config:user:{<user-id>}:<client>``. A pre-rollout key can contain the
      old single-row min() result (for example a lower THROTTLE row) and hide a
      higher HARD_BLOCK row until its 300-second TTL expires.

Safety properties:
  * dry-run is the default; deletion requires ``--apply``;
  * SCAN batches are bounded (default COUNT 200) — no KEYS command and no FLUSHDB/FLUSHALL;
  * every deletion candidate must match the exact shape
    ``budget:config:user:{<uuid>}:claude-code|cowork``;
  * spend counters (``budget:user:...``), reservation keys (``budget:pending*`` /
    ``budget:resvsum*``), negative-cache markers (``...:<client>:none``), team/user
    total configs, and unrelated clients such as ``codex`` are outside the selection;
  * Redis Cluster mode scans every primary node; standalone mode scans the single instance;
  * PostgreSQL is queried before any Redis write so missing schema/readiness fails closed.

Run inside an admin-api pod (see ops/8-U-update.md). The pod environment already has
REDIS_URL, REDIS_CLUSTER_MODE and DB_URL/DATABASE_URL. Never print those URLs: they can
contain credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

CLIENTS = ("claude-code", "cowork")
SCAN_COUNT_DEFAULT = 200
DB_TIMEOUT_SECONDS = 5.0
# Gateway/admin hydration writes these config keys with ex=300 (admin-api
# BUDGET_CONFIG_CACHE_TTL). The invalidation is still required because 300 seconds
# is long enough for requests to keep using a legacy single-policy value.
EXPECTED_CONFIG_TTL_SECONDS = 300

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
EXACT_KEY_RE = re.compile(rf"^budget:config:user:\{{{_UUID}\}}:(claude-code|cowork)$")


def _event(**fields) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


def _bool_env(raw: str | None) -> bool | None:
    if raw is None or raw == "" or raw.lower() == "auto":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _safe_error_message(exc: Exception) -> str:
    # Connector errors can echo a DSN. Never emit credentials or even the full URL:
    # the pod/operator already knows which environment this script is running against.
    msg = str(exc)
    msg = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", "<redacted-url>", msg)
    return msg[:300]


async def _db_readiness() -> dict[str, int]:
    """Prove PostgreSQL is readable and the two rehydration source tables exist."""
    db_url = os.environ.get("DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DB_URL or DATABASE_URL is required for the readiness check")
    engine = create_async_engine(db_url)
    try:
        async def _query():
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM budget.budget_configs "
                            " WHERE scope = 'USER' AND client IN ('claude-code', 'cowork')"
                            "   AND is_active), "
                            "(SELECT count(*) FROM budget.user_app_group_budgets "
                            " WHERE app_group IN ('claude-code', 'cowork'))"
                        )
                    )
                ).one()
                return {"active_per_app_rows": int(row[0]), "app_group_rows": int(row[1])}

        return await asyncio.wait_for(_query(), timeout=DB_TIMEOUT_SECONDS)
    finally:
        await engine.dispose()


async def _redis_client():
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is required")
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "socket_timeout": 5.0,
        "socket_connect_timeout": 3.0,
    }
    configured = _bool_env(os.environ.get("REDIS_CLUSTER_MODE"))
    if configured is True:
        return RedisCluster.from_url(redis_url, **kwargs), "cluster"
    if configured is False:
        return aioredis.from_url(redis_url, **kwargs), "standalone"

    probe = aioredis.from_url(redis_url, socket_timeout=3.0, socket_connect_timeout=2.0)
    try:
        mode = (await probe.info("server")).get("redis_mode", "standalone")
    finally:
        await probe.aclose()
    if mode == "cluster":
        return RedisCluster.from_url(redis_url, **kwargs), "cluster"
    return aioredis.from_url(redis_url, **kwargs), "standalone"


def _scan_cursor_value(next_cursor: Any, target_node) -> int:
    if not isinstance(next_cursor, dict):
        return int(next_cursor)
    node_name = getattr(target_node, "name", None) or str(target_node)
    if node_name not in next_cursor:
        raise RuntimeError(f"Redis Cluster SCAN cursor response did not include {node_name}")
    return int(next_cursor[node_name])


async def _scan_batch(redis, *, cursor: int, pattern: str, count: int, target_node=None):
    kwargs = {"match": pattern, "count": count}
    if target_node is not None:
        kwargs["target_nodes"] = target_node
    next_cursor, keys = await redis.scan(cursor=cursor, **kwargs)
    return _scan_cursor_value(next_cursor, target_node), list(keys)


async def _pttls(redis, keys: list[str]) -> list[int]:
    if not keys:
        return []
    pipe = redis.pipeline(transaction=False)
    for key in keys:
        pipe.pttl(key)
    return [int(v) for v in await pipe.execute()]


async def _scan_targets(redis, *, mode: str, apply: bool, scan_count: int) -> dict[str, Any]:
    """SCAN exact rollout keys in bounded batches; optionally DEL only validated keys."""
    if mode == "cluster":
        await redis.initialize()
        primaries = redis.get_primaries()
        if not primaries:
            raise RuntimeError("Redis Cluster scan found no primary nodes")
        targets = list(primaries)
    else:
        targets = [None]

    summary: dict[str, Any] = {
        "mode": mode,
        "primary_count": len(targets),
        "clients": {},
        "invalid_shape": [],
        "deleted": 0,
        "selected": 0,
        "glob_matches": 0,
    }

    for client in CLIENTS:
        # Braces are literal in Redis glob patterns. The '*' is bounded by the
        # literal ':<client>' suffix and the Python regex below is the exact gate.
        pattern = f"budget:config:user:{{*}}:{client}"
        cstats: dict[str, Any] = {
            "glob_matches": 0,
            "selected": 0,
            "deleted": 0,
            "invalid_shape": 0,
            "pttl_min_ms": None,
            "pttl_max_ms": None,
            "persistent_or_missing_in_batch": 0,
            "samples": [],
        }
        for target in targets:
            cursor = 0
            while True:
                cursor, keys = await _scan_batch(
                    redis, cursor=cursor, pattern=pattern, count=scan_count, target_node=target
                )
                if keys:
                    exact = [k for k in keys if EXACT_KEY_RE.match(k)]
                    invalid = [k for k in keys if not EXACT_KEY_RE.match(k)]
                    cstats["glob_matches"] += len(keys)
                    cstats["selected"] += len(exact)
                    cstats["invalid_shape"] += len(invalid)
                    cstats["samples"].extend(k for k in exact if len(cstats["samples"]) < 3)
                    summary["invalid_shape"].extend(invalid)

                    ttls = await _pttls(redis, exact)
                    positive = [v for v in ttls if v > 0]
                    if positive:
                        lo, hi = min(positive), max(positive)
                        cstats["pttl_min_ms"] = (
                            lo if cstats["pttl_min_ms"] is None
                            else min(cstats["pttl_min_ms"], lo)
                        )
                        cstats["pttl_max_ms"] = (
                            hi if cstats["pttl_max_ms"] is None
                            else max(cstats["pttl_max_ms"], hi)
                        )
                    cstats["persistent_or_missing_in_batch"] += sum(1 for v in ttls if v <= 0)

                    if apply and exact:
                        # DEL one key at a time. This is deliberate: in cluster mode a
                        # multi-key DEL can span hash slots, while single-key DEL is routed
                        # to the owning primary. Only the regex-validated exact key is sent.
                        for key in exact:
                            cstats["deleted"] += int(await redis.delete(key))
                if cursor == 0:
                    break
        summary["clients"][client] = cstats
        summary["selected"] += cstats["selected"]
        summary["deleted"] += cstats["deleted"]
        summary["glob_matches"] += cstats["glob_matches"]
    return summary


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply targeted invalidation of legacy per-client "
            "budget config cache keys."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="scan and report only (default)")
    mode.add_argument("--apply", action="store_true", help="delete validated keys")
    parser.add_argument(
        "--scan-count", type=int, default=SCAN_COUNT_DEFAULT,
        help="SCAN COUNT batch hint (1..1000)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.scan_count <= 1000:
        parser.error("--scan-count must be between 1 and 1000")

    try:
        db = await _db_readiness()
        _event(event="db_ready", **db)

        redis, mode = await _redis_client()
        try:
            result = await _scan_targets(
                redis, mode=mode, apply=args.apply, scan_count=args.scan_count
            )
        finally:
            await redis.aclose()
    except Exception as exc:  # operator-facing failure; do not print URLs or credentials
        _event(
            event="error", error_type=type(exc).__name__,
            message=_safe_error_message(exc),
        )
        return 2

    _event(
        event="applied" if args.apply else "dry_run",
        expected_config_ttl_seconds=EXPECTED_CONFIG_TTL_SECONDS,
        **result,
    )
    if result["invalid_shape"]:
        _event(
            event="warning",
            message="glob matched non-canonical keys; they were not selected or deleted",
            invalid_shape=result["invalid_shape"][:10],
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
