# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""mixed per-app/app-group 정책의 실 Lua 집행 회귀."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[2] / "src" / "app" / "redis_scripts"
_CHECK = (_SCRIPTS / "budget_check.lua").read_text(encoding="utf-8")
_RESERVE = (_SCRIPTS / "budget_reserve.lua").read_text(encoding="utf-8")
_SETTLE = (_SCRIPTS / "budget_settle.lua").read_text(encoding="utf-8")


def _layer(source, amount, policy, thresholds=None):
    return {
        "source": source,
        "limit_usd": str(amount),
        "policy": policy,
        "soft_limit_pct": 110,
        "throttle_rpm_pct": 50,
        "thresholds": [80] if thresholds is None else thresholds,
    }


def _config(*layers):
    # 최상위는 구버전용 보수 폴백 — 실제 검증 대상은 layers 다중 평가다.
    return {
        "limit_usd": layers[0]["limit_usd"],
        "policy": layers[0]["policy"],
        "soft_limit_pct": 110,
        "throttle_rpm_pct": 50,
        "thresholds": [80],
        "layers": list(layers),
    }


async def _check(fake_redis, config, used):
    usage = "budget:user:{u1}:claude-code:2026-10"
    key = "budget:config:user:{u1}:claude-code"
    await fake_redis.set(usage, str(used))
    await fake_redis.set(key, json.dumps(config))
    return json.loads(await fake_redis.eval(_CHECK, 2, usage, key, "client", ""))


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_first", [True, False])
async def test_check_lower_throttle_never_erases_hard_block(fake_redis, hard_first):
    hard = _layer("app" if hard_first else "group", 10, "hard_block")
    throttle = _layer("group" if hard_first else "app", 5, "throttle")
    result = await _check(fake_redis, _config(hard, throttle), 10)
    assert result["allowed"] is False
    assert result["reason"] == "client_budget_exceeded"

    result = await _check(fake_redis, _config(hard, throttle), 4.5)
    assert result["allowed"] is True
    assert result["throttle_active"] is True
    assert result["throttle_rpm_pct"] == 50


@pytest.mark.asyncio
async def test_check_soft_layer_blocks_at_soft_cap_and_warns_before_it(fake_redis):
    cfg = _config(
        _layer("app", 10, "hard_block"),
        _layer("group", 5, "soft_warning"),
    )
    blocked = await _check(fake_redis, cfg, 5.5)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "client_soft_limit_exceeded"

    warning = await _check(fake_redis, cfg, 5.1)
    assert warning["allowed"] is True
    assert warning["soft_warning"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_source", ["app", "group"])
async def test_check_zero_cap_on_either_row_blocks(fake_redis, zero_source):
    zero = _layer(zero_source, 0, "throttle")
    hard = _layer("group" if zero_source == "app" else "app", 10, "hard_block")
    result = await _check(fake_redis, _config(zero, hard), 0)
    assert result["allowed"] is False
    assert result["reason"] == "client_budget_exceeded"


def _reserve_keys(uid="u1", req="r1"):
    return (
        f"budget:user:{{{uid}}}:claude-code:2026-10",
        f"budget:config:user:{{{uid}}}:claude-code",
        f"budget:pending:client:{uid}:claude-code:{req}",
        f"budget:resvsum:user:{{{uid}}}:claude-code:2026-10",
    )


@pytest.mark.asyncio
async def test_reserve_enforces_hard_and_earliest_soft_cap_with_mixed_rows(fake_redis):
    usage, key, marker, resvsum = _reserve_keys()
    cfg = _config(
        _layer("app", 10, "hard_block"),
        _layer("group", 5, "soft_warning"),
    )
    await fake_redis.set(key, json.dumps(cfg))

    # soft 5 의 실제 cap 5.5 소진 — hard 10 이 남아도 입장 불가.
    await fake_redis.set(usage, "5.5")
    denied = json.loads(await fake_redis.eval(
        _RESERVE, 4, usage, key, marker, resvsum,
        "client", "0.1", "", "14400", "",
    ))
    assert denied["allowed"] is False
    assert float(await fake_redis.get(usage)) == pytest.approx(5.5)

    # cap 직전에는 잔여만큼만 예약한다(마지막 요청 완주 의미론).
    await fake_redis.delete(usage, marker, resvsum, "budget:settled:" + marker[16:])
    await fake_redis.set(usage, "5.4")
    allowed = json.loads(await fake_redis.eval(
        _RESERVE, 4, usage, key, marker, resvsum,
        "client", "1.0", "", "14400", "",
    ))
    assert allowed["allowed"] is True
    assert float(allowed["reserved_usd"]) == pytest.approx(0.1)
    assert float(await fake_redis.get(usage)) == pytest.approx(5.5)


@pytest.mark.asyncio
async def test_reserve_hard_cap_survives_lower_throttle(fake_redis):
    usage, key, marker, resvsum = _reserve_keys(req="hard")
    cfg = _config(
        _layer("app", 10, "hard_block"),
        _layer("group", 5, "throttle"),
    )
    await fake_redis.set(key, json.dumps(cfg))
    await fake_redis.set(usage, "10")
    result = json.loads(await fake_redis.eval(
        _RESERVE, 4, usage, key, marker, resvsum,
        "client", "0.1", "", "14400", "",
    ))
    assert result["allowed"] is False
    assert float(await fake_redis.get(usage)) == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_settle_thresholds_are_evaluated_for_each_layer(fake_redis):
    usage, key, marker, resvsum = _reserve_keys(req="settle")
    cfg = _config(
        _layer("app", 10, "hard_block", thresholds=[90]),
        _layer("group", 5, "throttle", thresholds=[80]),
    )
    await fake_redis.set(key, json.dumps(cfg))
    await fake_redis.set(marker, "2")
    await fake_redis.set(usage, "5.9")
    await fake_redis.set(resvsum, "2")
    # logical old 3.9 → actual +4.2 = 8.1. group limit 5 의 80% 교차를 보존한다.
    result = json.loads(await fake_redis.eval(
        _SETTLE, 4, usage, key, marker, resvsum,
        "4.2", "", "", "2", "14400",
    ))
    assert result["settled"] is True
    assert result["threshold_triggered"] == 80
