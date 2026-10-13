# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import TokenUsage
from app.services.cost_recorder import (
    COST_STREAM_KEY,
    CostRecorder,
    calculate_cost,
    resolve_context_tier,
)


def test_calculate_cost_basic(model_config_bedrock):
    usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    cost = calculate_cost(usage, model_config_bedrock)
    # (1000/1000 * 0.003) + (500/1000 * 0.015) = 0.003 + 0.0075 = 0.0105
    assert cost == Decimal("0.010500")


def test_calculate_cost_zero_usage(model_config_bedrock):
    usage = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    cost = calculate_cost(usage, model_config_bedrock)
    assert cost == Decimal("0.000000")


@pytest.mark.asyncio
async def test_finalize_publishes_to_stream(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """FR-3.3 리팩터: DB INSERT 대신 XADD cost:stream 호출 확인."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    usage = TokenUsage(input_tokens=500, output_tokens=300, total_tokens=800)

    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=usage,
        request_id="req-001",
        is_stream=False,
        duration_ms=150,
    )

    assert cost > Decimal("0")
    # XADD 호출 확인 + payload 포함
    mock_redis.xadd.assert_awaited_once()
    args, kwargs = mock_redis.xadd.call_args
    assert args[0] == COST_STREAM_KEY
    assert "payload" in args[1]
    # MAXLEN approx trim — 메모리 백스톱(실제 정리는 워커의 XTRIM MINID)
    assert kwargs.get("maxlen") == 500_000
    assert kwargs.get("approximate") is True


@pytest.mark.asyncio
async def test_finalize_xadd_payload_contains_cache_tokens_and_flags(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """XADD payload에 cache 토큰 + is_streaming + estimated_usage 포함 검증."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":80}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=10,
        estimated=True,  # KI-08 tokenizer 역산 흔적
    )

    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=usage,
        request_id="req-ki08",
        is_stream=True,
        duration_ms=300,
    )

    args, _ = mock_redis.xadd.call_args
    fields = args[1]
    payload = _json.loads(fields["payload"])
    assert payload["request_id"] == "req-ki08"
    assert payload["cache_creation_tokens"] == 20
    assert payload["cache_read_tokens"] == 10
    assert payload["is_streaming"] is True
    assert payload["estimated_usage"] is True
    assert payload["threshold_triggered"] == 80


@pytest.mark.asyncio
async def test_finalize_interrupted_stream_bills_estimated_input(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """Responses 방언은 usage 를 response.completed 에만 싣는다 — 그 전에
    끊기면 usage 가 전부 0인데 provider 는 프롬프트를 이미 청구했다.
    admission 시 계산된 estimated_input 으로 과금해 과소청구를 막는다."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(),  # 전부 0 — 종결 usage 미수신
        request_id="req-interrupted",
        is_stream=True,
        duration_ms=200,
        rate_limit_state={
            "estimated_input": 5000,
            "cost_reserved": Decimal("0"),
        },
    )

    # 5000 입력토큰 × $0.003/1k = $0.015
    assert cost == Decimal("0.015000")
    args, _ = mock_redis.xadd.call_args
    payload = _json.loads(args[1]["payload"])
    assert payload["input_tokens"] == 5000
    assert payload["estimated_usage"] is True


@pytest.mark.asyncio
async def test_finalize_partial_text_stream_bills_estimated_input(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """중간 절단 스트림은 스트림 헬퍼가 누적 텍스트로 output 을 역산해
    input=0/output>0 으로 도착한다 — 입력 추정치는 그래도 청구되고
    역산된 output 추정치는 보존되어야 한다."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(output_tokens=500, total_tokens=500, estimated=True),
        request_id="req-partial",
        is_stream=True,
        duration_ms=200,
        rate_limit_state={
            "estimated_input": 5000,
            "cost_reserved": Decimal("0"),
        },
    )

    # 5000 in × $0.003/1k + 500 out × $0.015/1k = 0.015 + 0.0075
    assert cost == Decimal("0.022500")
    payload = _json.loads(mock_redis.xadd.call_args.args[1]["payload"])
    assert payload["input_tokens"] == 5000
    assert payload["output_tokens"] == 500


@pytest.mark.asyncio
async def test_finalize_nonstream_zero_usage_not_estimated(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """비스트리밍 상류 5xx 는 추정 입력 과금 대상이 아니다 — 프롬프트가
    소비됐는지 확인할 방법이 없는 경로에 사용자를 청구하지 않는다."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(),
        request_id="req-504",
        is_stream=False,
        duration_ms=50,
        rate_limit_state={
            "estimated_input": 5000,
            "cost_reserved": Decimal("0"),
        },
        status="TIMEOUT",
    )

    assert cost == Decimal("0.000000")
    payload = _json.loads(mock_redis.xadd.call_args.args[1]["payload"])
    assert payload["input_tokens"] == 0


@pytest.mark.asyncio
async def test_finalize_zero_usage_upstream_error_not_billed(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """상류 거부(ERROR)에는 추정치를 청구하지 않는다 — 프롬프트가 실제로
    소비되지 않았을 수 있는 경로에서 사용자에게 과금하는 것보다 관망이 낫다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(),
        request_id="req-error",
        is_stream=True,
        duration_ms=50,
        rate_limit_state={
            "estimated_input": 5000,
            "cost_reserved": Decimal("0"),
        },
        status="ERROR",
    )

    # 실패는 0 비용 마커 행으로 기록되지만 추정치는 청구되지 않는다
    assert cost == Decimal("0.000000")
    args, _ = mock_redis.xadd.call_args
    import json as _json

    payload = _json.loads(args[1]["payload"])
    assert payload["input_tokens"] == 0


def test_calculate_cost_multi_turn_uses_per_turn_tier(model_config_bedrock):
    """웹검색 멀티턴: 턴별 short 인 요청이 합산 프롬프트로 long 판정되면 안 된다.

    턴 A 120K 입력 + 턴 B 120K 입력, threshold=200K 인 모델 —
    합산(240K)으로 판정하면 전량 장문 요율로 과대청구된다.
    """
    from app.schemas.domain import ModelPricingSchema

    pricing = model_config_bedrock.model_copy(
        update={
            "pricing": ModelPricingSchema(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                long_context_threshold_tokens=200_000,
                long_input_per_1k=Decimal("0.006"),
                long_output_per_1k=Decimal("0.030"),
            )
        }
    )
    turn = TokenUsage(input_tokens=120_000, output_tokens=1000, total_tokens=121_000)
    merged = TokenUsage(
        input_tokens=240_000,
        output_tokens=2000,
        total_tokens=242_000,
        turn_usages=[turn, turn.model_copy()],
    )
    # 턴별 단가 합산: 2 × (120×0.003 + 1×0.015) = 2 × 0.375 = 0.75
    assert calculate_cost(merged, pricing) == Decimal("0.750000")
    # 티어 판정도 턴별 — 합산으로는 long 이 되지만 정답은 short
    assert resolve_context_tier(merged, pricing) == "short"


def test_calculate_cost_multi_turn_propagates_1h_cache_flag(model_config_bedrock):
    """요청 레벨 cache_ttl_1h 는 라우터가 merged 객체에만 싣는다 — 턴 스냅샷엔
    없다. 턴별 과금에서 전파하지 않으면 1h 캐시 쓰기가 5m 요율로 과소청구된다."""
    turn = TokenUsage(
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        cache_creation_input_tokens=1000,
        # provider 가 ephemeral 분해를 보고하지 않음 → cache_ttl_1h 폴백 사용
    )
    merged = TokenUsage(
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        cache_creation_input_tokens=1000,
        cache_ttl_1h=True,  # 요청 측 1h 신호 — 라우터가 merged 에만 설정
        turn_usages=[turn],
    )
    # 1h 요율 0.006 적용: 0.1×0.003 + 0.01×0.015 + 1×0.006 = 0.006450
    assert calculate_cost(merged, model_config_bedrock) == Decimal("0.006450")


@pytest.mark.asyncio
async def test_finalize_teamless_marks_entry(mock_redis, model_config_bedrock):
    """팀 없는 사용자 요청은 teamless=True 를 실어 워커가 TEAM 스코프 집계를
    건너뛰게 한다 — default team 센티널 한도가 타인 지출로 소진되는 것 방지."""
    import json as _json

    from app.schemas.domain import AuthContext, AuthType, Role
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    teamless_ctx = AuthContext(
        user_id="user-999",
        team_id="",
        dept_id="",
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
        key_id="key-x",
        allowed_models=None,
    )
    recorder = CostRecorder()
    await recorder.finalize(
        redis=mock_redis,
        auth_context=teamless_ctx,
        model_config=model_config_bedrock,
        usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        request_id="req-teamless",
        is_stream=False,
        duration_ms=50,
    )
    args, _ = mock_redis.xadd.call_args
    payload = _json.loads(args[1]["payload"])
    assert payload["teamless"] is True


@pytest.mark.asyncio
async def test_finalize_redis_degraded_spools_cost_event(auth_context_vk, model_config_bedrock):
    """E2E S2-13: REDIS_DEGRADED(redis=None)면 XADD 없이 spool로 비용 이벤트 보존."""
    import json as _json
    from unittest.mock import MagicMock

    spool = MagicMock()
    recorder = CostRecorder(spool=spool)
    usage = TokenUsage(input_tokens=500, output_tokens=300, total_tokens=800)

    cost = await recorder.finalize(
        redis=None,  # 라우터가 넘기는 _redis_effective — degraded 시 None
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=usage,
        request_id="req-degraded",
        is_stream=False,
        duration_ms=150,
    )

    assert cost > Decimal("0")
    spool.enqueue.assert_called_once()
    payload = _json.loads(spool.enqueue.call_args.args[0])
    assert payload["request_id"] == "req-degraded"
    assert payload["cost_usd"] == str(cost) or payload["cost_usd"] is not None


@pytest.mark.asyncio
async def test_finalize_zero_usage_only_settles_tpm_ki08(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """스트리밍 disconnect + tokenizer 역산도 실패한 zero-usage 경로:
    TPM 예약만 해제, budget_deduct/XADD 모두 호출 안 됨."""
    from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor

    mock_redis.incrby = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=b'{}')
    mock_redis.xadd = AsyncMock()

    descriptors = [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id="u1",
            model_alias="claude-opus",
            tpm_limit=10000,
        ),
        ScopeDescriptor(
            scope=RateLimitScope.TEAM,
            scope_id="t1",
            model_alias="claude-opus",
            tpm_limit=100000,
        ),
    ]

    recorder = CostRecorder()
    zero_usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )

    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=zero_usage,
        request_id="req-disconnect",
        is_stream=True,
        duration_ms=500,
        rate_limit_state={
            "tpm_descriptors": descriptors,
            "tpm_reserved": 5000,
        },
    )

    assert cost == Decimal("0")
    # TPM settle만 호출 (actual=0, reserved=5000 → -5000 환불, scope 2개).
    # settle_tpm 은 이제 파이프라인으로 묶어 incrby 한다(deepdive Q50).
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrby.call_count == 2
    for call in pipe.incrby.call_args_list:
        assert call.args[1] == -5000
    # budget_deduct Lua / XADD 모두 호출 안 됨
    mock_redis.eval.assert_not_called()
    mock_redis.xadd.assert_not_called()


def test_calculate_cost_precision(model_config_openai):
    usage = TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    cost = calculate_cost(usage, model_config_openai)
    assert len(str(cost).split(".")[-1]) <= 6


def test_calculate_cost_1h_cache_split(model_config_bedrock):
    """R2-12: 응답 분해가 있으면 5m/1h 부분을 각각 단가로 과금한다.

    pricing: cache_write_per_1k=0.00375 (5m), cache_write_1h_per_1k=0.006 (1h)
    """
    usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_creation_input_tokens=1000,
        cache_creation_1h_input_tokens=400,  # 400은 1h, 600은 5m
    )
    cost = calculate_cost(usage, model_config_bedrock)
    # 400/1000*0.006 + 600/1000*0.00375 = 0.0024 + 0.00225 = 0.00465
    assert cost == Decimal("0.004650")


def test_calculate_cost_1h_boolean_fallback(model_config_bedrock):
    """응답 분해가 없는 경로(비Anthropic) — 요청 신호로 전체를 1h 단가로."""
    usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_creation_input_tokens=1000,
        cache_ttl_1h=True,
    )
    cost = calculate_cost(usage, model_config_bedrock)
    assert cost == Decimal("0.006000")


def test_calculate_cost_5m_default(model_config_bedrock):
    usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_creation_input_tokens=1000,
    )
    cost = calculate_cost(usage, model_config_bedrock)
    assert cost == Decimal("0.003750")
