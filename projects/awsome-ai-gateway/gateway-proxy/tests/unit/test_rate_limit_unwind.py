# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""다중 스코프 pre-reserve 의 부분 커밋 해제(unwind) 회귀 테스트.

스코프별 eval 이 체크+예약을 한 번에 커밋하므로, 앞 스코프 통과·뒤 스코프 거절이면
앞 스코프 예약이 누수됐다. 거절 시점까지 커밋된 스코프는 즉시 되돌려야 한다.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_scope import build_scope_descriptors
from app.services.rate_limit_service import RateLimitService


def _ok() -> bytes:
    return json.dumps(
        {
            "allowed": True,
            "scope": None,
            "limit_type": None,
            "limit": -1,
            "remaining": -1,
            "retry_after": None,
            "window_reset": None,
        }
    ).encode()


def _deny(scope: str, limit_type: str) -> bytes:
    return json.dumps(
        {
            "allowed": False,
            "scope": scope,
            "limit_type": limit_type,
            "limit": 100,
            "remaining": 0,
            "retry_after": 30,
            "window_reset": 1713574000,
        }
    ).encode()


@pytest.mark.asyncio
async def test_tpm_team_reject_refunds_user_reservation(mock_redis):
    """USER TPM 통과(커밋) 후 TEAM 거절 → USER 예약이 되돌려져야 한다."""
    mock_redis.eval = AsyncMock(side_effect=[_ok(), _deny("TEAM", "tpm"), _ok()])
    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=None,
    )

    result = await RateLimitService().check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=5000
    )
    assert result.allowed is False

    pipe = mock_redis.pipeline.return_value
    # USER 스코프 1개만 환불 (-5000). TEAM 은 거절이라 커밋되지 않았다.
    assert pipe.incrby.call_count == 1
    key, delta = pipe.incrby.call_args.args
    assert "USER" in key and ":tpm:cur" in key
    assert delta == -5000
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_tpm_committed_out_reports_committed_scopes(mock_redis):
    """committed_out 에는 실제로 커밋된 스코프만 담긴다."""
    mock_redis.eval = AsyncMock(side_effect=[_ok(), _deny("TEAM", "tpm")])
    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )
    committed: list = []

    result = await RateLimitService().check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=1000, committed_out=committed
    )
    assert result.allowed is False
    # eval 2회만 실행(USER, TEAM) — USER 만 커밋됐다.
    assert len(committed) == 1
    assert committed[0].scope.value == "USER"


@pytest.mark.asyncio
async def test_tpm_all_pass_reports_all_committed(mock_redis):
    mock_redis.eval = AsyncMock(side_effect=[_ok(), _ok(), _ok()])
    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )
    committed: list = []

    result = await RateLimitService().check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=1000, committed_out=committed
    )
    assert result.allowed is True
    assert len(committed) == 3
    # 통과 경로는 환불하지 않는다.
    mock_redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_cost_team_reject_refunds_user_reservation(mock_redis):
    """USER 비용 예약 커밋 후 TEAM 거절 → USER cpm/cph 키가 되돌려져야 한다."""
    mock_redis.eval = AsyncMock(side_effect=[_ok(), _deny("TEAM", "cpm")])
    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"

    result = await RateLimitService().reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("1.25"),
        user_cpm_limit=Decimal("10"),
        user_cph_limit=None,
        team_id="t1",
        team_cpm_limit=Decimal("5"),
        team_cph_limit=None,
    )
    assert result.allowed is False
    assert result.scope == "TEAM"

    pipe = mock_redis.pipeline.return_value
    # USER 스코프 cpm+cph 2키에 -1.25 환불.
    assert pipe.incrbyfloat.call_count == 2
    for call in pipe.incrbyfloat.call_args_list:
        key, delta = call.args
        assert "user" in key and "u1" in key
        assert delta == -1.25
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_cost_user_reject_no_refund(mock_redis):
    """첫 스코프(USER)가 거절되면 커밋된 것이 없어 환불이 없다."""
    mock_redis.eval = AsyncMock(side_effect=[_deny("USER", "cph")])
    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"

    result = await RateLimitService().reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("1.25"),
        user_cpm_limit=Decimal("10"),
        user_cph_limit=Decimal("1"),
        team_id="t1",
        team_cpm_limit=Decimal("5"),
        team_cph_limit=None,
    )
    assert result.allowed is False
    mock_redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_enforce_cost_reject_unwinds_tpm(mock_redis, mock_session_factory):
    """TPM 전 스코프 통과 후 비용 거절 → TPM 예약이 전부 되돌려져야 한다."""
    from app.schemas.domain import (
        ApiFormat,
        AuthContext,
        AuthType,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )
    from app.services import rate_limit_enforcement as enf

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"
    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"
    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"

    # RPM 통과 ×1 → TPM 통과 ×2 → 비용 거절 ×1
    mock_redis.eval = AsyncMock(
        side_effect=[
            _ok(),                       # RPM (num_scopes=1 per call — limit 있는 스코프만)
            _ok(),                       # TPM USER
            _ok(),                       # TPM TEAM
            _deny("USER", "cpm"),        # cost USER 거절
        ]
    )

    async def _load(*, redis, db, user_id, team_id, model_alias):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(rpm=100, tpm=100000, cpm=Decimal("1"), cph=None),
            team=ScopeLimits(rpm=None, tpm=1000000, cpm=None, cph=None),
            global_=ScopeLimits(rpm=None, tpm=None, cpm=None, cph=None),
        )

    import app.services.rate_limit_enforcement as enf_mod

    orig_load = enf_mod.load_all_scope_limits
    enf_mod.load_all_scope_limits = _load
    try:
        auth = AuthContext(
            user_id="u1",
            team_id="t1",
            dept_id="d1",
            roles=[],
            auth_type=AuthType.VIRTUAL_KEY,
            key_id="k1",
        )
        model = ModelConfigSchema(
            alias="m1",
            provider_model_id="pmid",
            provider=ProviderType.BEDROCK,
            api_format=ApiFormat.BEDROCK_NATIVE,
            status=ModelStatus.ACTIVE,
            pricing=ModelPricingSchema(
                input_per_1k=Decimal("1"), output_per_1k=Decimal("1")
            ),
        )
        state: dict = {}
        resp = await enf_mod.enforce_rate_limits(
            redis=mock_redis,
            auth_context=auth,
            model_config=model,
            body={"max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            state=state,
            request_id="r1",
        )
    finally:
        enf_mod.load_all_scope_limits = orig_load

    assert resp is not None and resp.status_code == 429
    # TPM 환불: USER+TEAM 2 스코프에 음수 INCRBY.
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrby.call_count == 2
    for call in pipe.incrby.call_args_list:
        assert call.args[1] < 0
    assert "rate_limit_state" not in state


# ── settle 의 윈도우·스코프 정확도 (M2/M7) ─────────────────────────────


@pytest.mark.asyncio
async def test_settle_cost_writes_only_committed_scopes(mock_redis):
    """팀 한도가 없어 예약이 USER 에만 커밋됐으면, settle 은 팀 키를 만들지 않는다."""
    await RateLimitService().settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0.10"),
        reserved_cost=Decimal("0.50"),
        team_id="t1",
        committed_scopes=["USER"],
        cpm_window_ts=1700000000,
        cph_window_ts=1700000000,
    )
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrbyfloat.call_count == 2
    for call in pipe.incrbyfloat.call_args_list:
        key, delta = call.args
        assert "user" in key
        assert delta == -0.4


@pytest.mark.asyncio
async def test_settle_cost_uses_reserved_window_not_now(mock_redis):
    """분 경계를 넘어 끝난 요청의 정산은 *예약된* 윈도우 키에 쓴다."""
    await RateLimitService().settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0.10"),
        reserved_cost=Decimal("0.50"),
        team_id=None,
        committed_scopes=["USER"],
        cpm_window_ts=1700000000,  # 예약 시점 분 버킷
        cph_window_ts=1699992000,  # 예약 시점 시 버킷
    )
    pipe = mock_redis.pipeline.return_value
    keys = [call.args[0] for call in pipe.incrbyfloat.call_args_list]
    assert any(k.endswith(":cpm:1700000000") for k in keys)
    assert any(k.endswith(":cph:1699992000") for k in keys)


@pytest.mark.asyncio
async def test_settle_tpm_refunds_rotated_bucket_via_prev(mock_redis):
    """분 경계 후 정산 — 예약이 prev 로 로테이션됐으면 prev 에 차액을 쓴다."""
    import time

    now = int(time.time())
    current_bucket = int(now // 60)
    reserved_bucket = current_bucket - 1
    reserved_at = reserved_bucket * 60 + 30  # 직전 분 버킷 중간에 예약됨

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id=None,
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=None,
        global_rpm=None,
        global_tpm=None,
    )
    # win 마커 = 현재 버킷 (로테이션 완료 상태 시뮬레이션)
    mock_redis.get = AsyncMock(return_value=str(current_bucket).encode())

    await RateLimitService().settle_tpm(
        mock_redis,
        descriptors,
        reserved_tokens=5000,
        actual_tokens=3500,
        reserved_at=reserved_at,
    )
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrby.call_count == 1
    key, delta = pipe.incrby.call_args.args
    assert ":tpm:prev" in key
    assert delta == -1500


@pytest.mark.asyncio
async def test_settle_tpm_same_bucket_writes_cur(mock_redis):
    """같은 분 안의 정산은 여전히 cur 에 쓴다."""
    import time

    now = int(time.time())
    bucket = int(now // 60)
    mock_redis.get = AsyncMock(return_value=str(bucket).encode())

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id=None,
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=None,
        global_rpm=None,
        global_tpm=None,
    )
    await RateLimitService().settle_tpm(
        mock_redis,
        descriptors,
        reserved_tokens=5000,
        actual_tokens=3500,
        reserved_at=now,
    )
    pipe = mock_redis.pipeline.return_value
    key, delta = pipe.incrby.call_args.args
    assert ":tpm:cur" in key
    assert delta == -1500
