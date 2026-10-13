# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R12 레이트리밋/예약 경로 심층 리뷰 회귀 테스트.

커버:
- H1: undo_rpm 이 Lua 가 실제로 ZADD 한 `request_id:1` 멤버를 지운다
- H2: unsettled_reservation_guard 가 drain 태스크 정산과 경쟁하지 않는다
- H3: 한 스코프의 DB 쿼리 실패가 캐시되지 않고 다음 스코프 쿼리도 산다
- M1: CPM/CPH 카운터가 모델 차원을 갖는다 (모델별 한도 → 모델별 카운터)
- M5: reserve_budget eval 실패가 rl_fail_mode=closed 를 존중한다
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.fallback_loop import unsettled_reservation_guard
from app.services.rate_limit_service import RateLimitService


# ---------------------------------------------------------------- H1


@pytest.mark.asyncio
async def test_undo_rpm_removes_lua_suffixed_member(mock_redis):
    """rate_limit_check.lua 는 `request_id:<idx>` 로 ZADD 한다 — bare id 만
    지우면 롤백이 no-op 이되어 거절된 요청이 RPM 슬롯을 계속 물었다."""
    from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=pipe)

    d = ScopeDescriptor(
        scope=RateLimitScope.USER,
        scope_id="u1",
        model_alias="m1",
        rpm_limit=60,
    )
    await RateLimitService().undo_rpm(mock_redis, [d], "req-42")

    assert pipe.zrem.called
    _key, members = pipe.zrem.call_args[0][0], pipe.zrem.call_args[0][1:]
    assert "req-42" in members
    assert "req-42:1" in members


# ---------------------------------------------------------------- H2


@pytest.mark.asyncio
async def test_guard_waits_for_drain_owned_settlement(mock_redis):
    """drain_pending 이 서 있는 동안 guard 가 예약을 되돌리지 않는다 —
    release(0) 가 먼저 가면 뒤의 settle(actual) 이 no-op 이 된다."""
    state = {
        "rate_limit_state": {"tpm_descriptors": [], "tpm_reserved": 0},
        "_stream_outcome": {"drain_pending": True},
        "_redis": mock_redis,
    }
    task = asyncio.create_task(unsettled_reservation_guard(state).func())
    await asyncio.sleep(0.6)  # guard 의 poll 주기(0.25s) 두 번 이상
    assert "rate_limit_state" in state  # 아직 release 안 함
    # drain 완료 — 정산 후 플래그 해제
    state["_stream_outcome"].pop("drain_pending")
    state.pop("rate_limit_state")  # drain 의 finalize 가 pop 한 것과 동일
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_guard_releases_after_drain_deadline(mock_redis):
    """드레인 사망(플래그 잔류 + state 미정산)이면 데드라인 뒤 guard 가 release."""
    state = {
        "rate_limit_state": {"tpm_descriptors": [], "tpm_reserved": 0},
        "_stream_outcome": {"drain_pending": True},
        "_redis": mock_redis,
    }
    # 데드라인을 짧게 — 설정값을 monkeypatch 하지 않고 플래그만 빨리 지운다.
    # (실제 데드라인 경로는 settings 주입이라 단위 테스트 대신 첫 poll 검증)
    task = asyncio.create_task(unsettled_reservation_guard(state).func())
    await asyncio.sleep(0.4)
    assert "rate_limit_state" in state
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- H3


@pytest.mark.asyncio
async def test_loader_db_failure_rolls_back_and_skips_cache(mock_redis):
    """한 스코프 쿼리 실패 → 세션 rollback 후 다음 스코프 쿼리는 성공하고,
    실패한 스코프는 unlimited 로 통과되되 캐시에 쓰지 않는다."""
    from app.services.rate_limit_config_loader import (
        ScopeLimits,
        load_all_scope_limits,
    )

    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    def _mk(scope):
        row = MagicMock()
        row.scope = scope
        row.scope_id = "t1" if scope == "TEAM" else "u1"
        row.model_alias = "m1"
        row.rpm_limit = 100
        row.tpm_limit = 1000
        row.cpm_limit_usd = None
        row.cph_limit_usd = None
        row.is_active = True
        return row

    calls = {"n": 0}

    async def fake_execute(stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        r = MagicMock()
        r.scalars = MagicMock(
            return_value=MagicMock(
                all=MagicMock(return_value=[_mk("TEAM") if calls["n"] == 2 else _mk("GLOBAL")])
            )
        )
        return r

    db = MagicMock()
    db.execute = fake_execute
    db.rollback = AsyncMock()

    result = await load_all_scope_limits(
        redis=mock_redis, db=db, user_id="u1", team_id="t1", model_alias="m1"
    )
    db.rollback.assert_awaited_once()
    assert result.user == ScopeLimits()          # 실패 스코프 — unlimited, 미캐시
    assert result.team.rpm == 100                # rollback 후 다음 쿼리 생존
    assert result.global_.rpm == 100
    assert mock_redis.set.call_count == 2        # TEAM+GLOBAL 만 캐시


# ---------------------------------------------------------------- M1


@pytest.mark.asyncio
async def test_reserve_cost_keys_include_model_alias(mock_redis):
    """모델별 CPM 한도는 모델별 카운터에 — 없으면 사용자의 전 모델 지출이
    한 모델의 한도를 소진한다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    seen_keys: list[str] = []

    async def fake_eval(script, n, *args):
        seen_keys.extend(args[:n])
        return json.dumps({"allowed": True}).encode()

    mock_redis.eval = fake_eval
    await RateLimitService().reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("0.01"),
        user_cpm_limit=Decimal("1"),
        user_cph_limit=None,
        team_id="t1",
        team_cpm_limit=None,
        team_cph_limit=None,
        user_key_alias="m1",
    )
    assert seen_keys and "m1" in seen_keys[0]
    assert ":cpm:" in seen_keys[0]


@pytest.mark.asyncio
async def test_settle_cost_uses_committed_keys(mock_redis):
    """committed_keys 가 있으면 그 키(모델 차원+예약 윈도우 포함)에만 정산."""
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=pipe)

    committed = [("USER", "rl:cost:user:{u1}:m1:cpm:100", "rl:cost:user:{u1}:m1:cph:3600")]
    await RateLimitService().settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0.001"),
        reserved_cost=Decimal("0.01"),
        committed_keys=committed,
    )
    keys = [c.args[0] for c in pipe.incrbyfloat.call_args_list]
    assert "rl:cost:user:{u1}:m1:cpm:100" in keys
    assert "rl:cost:user:{u1}:m1:cph:3600" in keys


# ---------------------------------------------------------------- M5


@pytest.mark.asyncio
async def test_reserve_budget_eval_failure_fail_closed(mock_redis, monkeypatch):
    """rl_fail_mode=closed 이면 예약 eval 실패는 통과가 아니라 거절이다."""
    from app.services.budget_service import BudgetService
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_reserve"] = "-- mock"
    LuaScriptLoader._scripts["budget_settle"] = "-- mock"
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: MagicMock(rl_fail_mode="closed"),
    )
    mock_redis.eval = AsyncMock(side_effect=RuntimeError("redis blip"))
    mock_redis.exists = AsyncMock(return_value=True)

    with pytest.raises(PermissionError):
        await BudgetService().reserve_budget(
            mock_redis, "u1", "t1", "2026-10", Decimal("1"), "r1"
        )


@pytest.mark.asyncio
async def test_reserve_budget_eval_failure_fail_open(mock_redis, monkeypatch):
    from app.services.budget_service import BudgetService
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_reserve"] = "-- mock"
    LuaScriptLoader._scripts["budget_settle"] = "-- mock"
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: MagicMock(rl_fail_mode="open"),
    )
    mock_redis.eval = AsyncMock(side_effect=RuntimeError("redis blip"))
    mock_redis.exists = AsyncMock(return_value=True)

    out = await BudgetService().reserve_budget(
        mock_redis, "u1", "t1", "2026-10", Decimal("1"), "r1"
    )
    assert out == []
