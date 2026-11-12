"""월예산 잔여 클램프(부분 예약) 회귀 — budget_reserve.lua 실실행.

배경: hard_block 은 ``used + estimate > limit`` 이면 거절했는데, 비싼 모델의
worst-case 선예약이 소액 예산의 잔여를 통째로 넘어 실비 $0.5 호출이
worst $7 평가로 차단됐다. 잔여가 있으면 ``reserved = min(estimate, 잔여)``
부분 예약으로 입장하고, 실비는 settle 이 카운터를 cap 위로 올려 다음 요청이
차단되는 방향으로 수렴한다 — "마지막 요청 완주" 의미론.
"""

import json
from pathlib import Path

import fakeredis.aioredis as far
import pytest

from app.services.lua_loader import LuaScriptLoader


@pytest.fixture()
async def redis():
    r = far.FakeRedis(decode_responses=True)
    LuaScriptLoader.load_all(
        Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts"
    )
    yield r
    await r.aclose()


KEYS = (
    "budget:team:{t1}:2026-11",          # usage
    "budget:config:team:{t1}",           # config
    "budget:pending:team:{t1}:req-1",    # marker
    "budget:resvsum:team:{t1}:2026-11",  # resvsum
    "budget:pendingidx:team:{t1}",       # idx
)
CFG = {"limit_usd": "10", "policy": "hard_block"}


async def _reserve(r, script, req_id="req-1", estimate="7.0"):
    keys = list(KEYS)
    keys[2] = f"budget:pending:team:{{t1}}:{req_id}"
    raw = await r.eval(
        script, 5, *keys, "team", estimate, "", "14400", req_id
    )
    return json.loads(raw), keys


@pytest.mark.asyncio
async def test_partial_reservation_admits_with_remaining_clamp(redis):
    """used=9, worst=7, limit=10 — 예전엔 거절, 이제 잔여 1 만큼 예약 입장."""
    script = LuaScriptLoader.get("budget_reserve")
    usage, config, marker, resvsum, idx = KEYS
    await redis.set(config, json.dumps(CFG))
    await redis.set(usage, "9")

    res, keys = await _reserve(redis, script, estimate="7")
    assert res["allowed"] and res["reserved"]
    assert res["partial"] is True
    assert float(res["reserved_usd"]) == 1.0
    # 카운터·마커·인덱스 모두 clamped 값
    assert float(await redis.get(usage)) == 10.0
    assert float(await redis.get(resvsum)) == 1.0
    assert await redis.get(marker) == "1" or float(await redis.get(marker)) == 1.0
    assert float(await redis.hget(idx, "req-1")) == 1.0


@pytest.mark.asyncio
async def test_full_reservation_when_estimate_fits(redis):
    """잔여가 worst-case 를 덮으면 전액 예약 — partial 아님."""
    script = LuaScriptLoader.get("budget_reserve")
    await redis.set(KEYS[1], json.dumps(CFG))
    await redis.set(KEYS[0], "5")

    res, _ = await _reserve(redis, script, estimate="3")
    assert res["allowed"] and res["reserved"]
    assert res["partial"] is False
    assert float(res["reserved_usd"]) == 3.0
    assert float(await redis.get(KEYS[0])) == 8.0


@pytest.mark.asyncio
async def test_exhausted_cap_still_rejects(redis):
    """used >= limit = 실제 소진 — 입장 없이 차단."""
    script = LuaScriptLoader.get("budget_reserve")
    await redis.set(KEYS[1], json.dumps(CFG))
    await redis.set(KEYS[0], "10")

    res, keys = await _reserve(redis, script, estimate="0.5")
    assert not res["allowed"]
    assert res["reason"] == "team_budget_exceeded"
    assert await redis.get(keys[2]) is None  # 마커 미생성


@pytest.mark.asyncio
async def test_settle_after_partial_pushes_over_cap(redis):
    """부분 예약 입장 후 settle 실비가 잔여를 넘으면 카운터가 cap 위로 —
    다음 요청이 차단되는 수렴 방향."""
    script = LuaScriptLoader.get("budget_reserve")
    settle = LuaScriptLoader.get("budget_settle")
    await redis.set(KEYS[1], json.dumps(CFG))
    await redis.set(KEYS[0], "9.9")

    res, keys = await _reserve(redis, script, estimate="7")
    assert res["allowed"] and res["partial"] is True
    assert float(res["reserved_usd"]) == pytest.approx(0.1)

    # 실비 $0.8 정산 — 예약 0.1 차감 + 실비 0.8 가산 → 10.7 (cap 위).
    s = json.loads(await redis.eval(
        settle, 5, keys[0], keys[1], keys[2], keys[3], keys[4],
        "0.8", "", "req-1", "0.1", "14400"))
    assert s["settled"] is True
    assert float(await redis.get(keys[0])) == pytest.approx(10.7)

    # 다음 요청은 used >= cap 이라 차단.
    res2, _ = await _reserve(redis, script, req_id="req-2", estimate="0.5")
    assert not res2["allowed"] and res2["reason"] == "team_budget_exceeded"


@pytest.mark.asyncio
async def test_soft_warning_partial_clamps_to_soft_cap(redis):
    """soft_warning 은 limit×soft_limit_pct 가 cap — 그 잔여로 클램프."""
    script = LuaScriptLoader.get("budget_reserve")
    await redis.set(KEYS[1], json.dumps(
        {"limit_usd": "10", "policy": "soft_warning", "soft_limit_pct": 110}))
    await redis.set(KEYS[0], "10.5")  # soft cap = 11, 잔여 0.5

    res, _ = await _reserve(redis, script, estimate="3")
    assert res["allowed"] and res["partial"] is True
    assert float(res["reserved_usd"]) == pytest.approx(0.5)

    # soft cap 소진 상태는 차단
    await redis.set(KEYS[0], "11")
    res2, _ = await _reserve(redis, script, req_id="req-2", estimate="0.1")
    assert not res2["allowed"]


@pytest.mark.asyncio
async def test_throttle_unaffected_by_clamp(redis):
    """throttle 은 차단 없음 — 예약도 전액(정산 정확도)."""
    script = LuaScriptLoader.get("budget_reserve")
    await redis.set(KEYS[1], json.dumps(
        {"limit_usd": "10", "policy": "throttle"}))
    await redis.set(KEYS[0], "50")  # 한도 훨씬 초과해도

    res, _ = await _reserve(redis, script, estimate="7")
    assert res["allowed"] and res["partial"] is False
    assert float(await redis.get(KEYS[0])) == 57.0
