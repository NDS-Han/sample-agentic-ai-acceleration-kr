# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""월예산 admission 예약(reserve/settle) 단위 테스트.

budget_check 가 읽기 전용이라 생기던 check-then-act 초과 창을
budget_reserve.lua 의 원자 예약으로 닫는다 — Python 측 계약(스코프 순서,
거절 시 커밋분 환불, settle 의 마커 멱등 배선)을 검증한다.
Lua 자체의 동작은 실 Redis 통합검증으로 별도 확인했다.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.budget_service import BudgetService


def _resv_resp(*, allowed=True, reserved=True, scope="user", config_present=True,
               reason=None, used_usd=0.0, limit_usd=100.0):
    return json.dumps({
        "allowed": allowed,
        "reserved": reserved,
        "reason": reason,
        "used_usd": used_usd,
        "limit_usd": limit_usd,
        "scope": scope,
        "config_present": config_present,
    }).encode()


def _settle_resp(*, settled=True, threshold=None):
    return json.dumps({
        "settled": settled,
        "new_used": 5.0,
        "remaining": 95.0,
        "threshold_triggered": threshold,
        "app_clients": [],
    }).encode()


def _dispatch_by_scope(scope_map: dict[str, bytes]):
    """eval side_effect — reserve 호출 시그니처는
    (script, 5, usage_key, config_key, marker_key, resvsum_key, idx_key, scope, est, fallback, ttl, idx_field)."""
    async def _side(*args, **_kw):
        scope = args[7]
        return scope_map[scope]
    return _side


@pytest.fixture(autouse=True)
def _lua_stub():
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_reserve"] = "-- reserve"
    LuaScriptLoader._scripts["budget_settle"] = "-- settle"
    yield


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """재시도 백오프를 0 으로 — 테스트는 즉시."""
    import asyncio

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
async def test_reserve_retries_max_connections_burst(mock_redis):
    """관측된 풀고갈 형태 — MaxConnectionsError 는 커맨드가 안 나간 상태라
    재시도가 안전하고 버스트를 흡수한다."""
    from redis.exceptions import MaxConnectionsError

    calls = []

    async def _side(*args, **_kw):
        calls.append(args)
        if len(calls) == 1:
            raise MaxConnectionsError("Too many connections")
        return _resv_resp(scope=args[7])

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )

    assert [s["scope"] for s in committed] == ["team", "user"]
    # 첫 시도 실패 → 재시도 성공 → 총 3회 eval(team 실패+재시도, user).
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_reserve_timeout_retries_then_fail_open(mock_redis):
    """reserve eval 은 request_id 마커로 서버측 멱등 — TimeoutError(서버 실행
    모호)도 재시도한다. 재전송은 Lua 의 EXISTS 가드가 dedup_hit 재생으로
    바꾸므로 이중 적용이 없다. 전부 실패하면 여전히 fail-open."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    calls = []

    async def _side(*args, **_kw):
        calls.append(args)
        raise RedisTimeoutError("timeout")

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )

    assert committed == []
    reserve_calls = [c for c in calls if c[0] == "-- reserve"]
    # 모호한 타임아웃도 재시도한다 — 첫 스코프(team)가 3회 시도 후 실패하면
    # 루프가 중단돼 user 는 호출조차 안 한다(fail-open 유지).
    assert len(reserve_calls) == 3
    assert all(c[7] == "team" for c in reserve_calls)


@pytest.mark.asyncio
async def test_reserve_timeout_retry_succeeds_and_reuses_marker(mock_redis):
    """타임아웃 뒤 재시도 성공 시 예약은 커밋된다 — 서버에서 이미 실행됐던
    재전송은 Lua 가 dedup_hit 재생으로 돌려주므로 커밋 디스크립터 계약 유지."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    calls = []

    async def _side(*args, **_kw):
        calls.append(args)
        if len(calls) == 1:
            raise RedisTimeoutError("ambiguous timeout")
        return _resv_resp(scope=args[7])

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )
    assert [s["scope"] for s in committed] == ["team", "user"]


@pytest.mark.asyncio
async def test_reserve_top_level_reads_retry(mock_redis):
    """reserve 본문 진입 전의 config 읽기(exists/get)는 멱등이라 일시 오류를
    전부 재시도한다 — 여기서 나는 풀고갈이 reserve 전체 실패로 번지던 결함."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    mock_redis.exists = AsyncMock(
        side_effect=[RedisConnectionError("pool burst"), True]
    )
    mock_redis.eval = AsyncMock(side_effect=_dispatch_by_scope({
        "team": _resv_resp(scope="team"),
        "user": _resv_resp(scope="user"),
    }))

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )

    assert [s["scope"] for s in committed] == ["team", "user"]
    assert mock_redis.exists.await_count == 2


@pytest.mark.asyncio
async def test_reserve_commits_team_then_user(mock_redis):
    """정상 경로: team → user 순으로 커밋되고 디스크립터가 반환된다."""
    mock_redis.eval = AsyncMock(side_effect=_dispatch_by_scope({
        "team": _resv_resp(scope="team"),
        "user": _resv_resp(scope="user"),
    }))
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )

    assert [s["scope"] for s in committed] == ["team", "user"]
    assert committed[0]["usage_key"] == "budget:team:{t1}:2026-05"
    assert committed[1]["marker_key"] == "budget:pending:user:{u1}:req-1"


@pytest.mark.asyncio
async def test_reserve_user_reject_refunds_team_commit(mock_redis):
    """user 거절 시 이미 커밋된 team 예약을 settle(0) 으로 되돌린다."""
    calls: list[tuple] = []

    async def _side(*args, **_kw):
        calls.append(args)
        if args[0] == "-- settle":
            return _settle_resp()
        return {
            "team": _resv_resp(scope="team"),
            "user": _resv_resp(
                allowed=False, reserved=False, scope="user",
                reason="user_budget_exceeded",
            ),
        }[args[7]]

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await svc.reserve_budget(
            mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
        )

    settle_calls = [c for c in calls if c[0] == "-- settle"]
    assert len(settle_calls) == 1
    # settle(script, 5, usage, config, marker, resvsum, idx, actual, fallback, field)
    assert settle_calls[0][4] == "budget:pending:team:{t1}:req-1"
    assert settle_calls[0][7] == "0"


@pytest.mark.asyncio
async def test_reserve_no_team_fail_closed(mock_redis):
    svc = BudgetService()
    with pytest.raises(PermissionError, match="no_team_assigned"):
        await svc.reserve_budget(
            mock_redis, "u1", "", "2026-05", Decimal("4.5"), "req-1",
        )


@pytest.mark.asyncio
async def test_reserve_team_unset_denies(mock_redis):
    """team config 없음 → C-1 정책 그대로 deny."""
    mock_redis.eval = AsyncMock(side_effect=_dispatch_by_scope({
        "team": _resv_resp(scope="team", config_present=False, reserved=False),
        "user": _resv_resp(scope="user"),
    }))
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.reserve_budget(
            mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
        )


@pytest.mark.asyncio
async def test_reserve_user_config_absent_passes_unreserved(mock_redis):
    """user config 없음(+D 없음) → Q 정책 pass, user scope 는 미예약."""
    mock_redis.eval = AsyncMock(side_effect=_dispatch_by_scope({
        "team": _resv_resp(scope="team"),
        "user": _resv_resp(scope="user", config_present=False, reserved=False),
    }))
    mock_redis.exists = AsyncMock(return_value=False)  # user config key 부재
    mock_redis.get = AsyncMock(return_value=None)       # team config 도 부재 → fallback 없음

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )
    assert [s["scope"] for s in committed] == ["team"]


@pytest.mark.asyncio
async def test_reserve_client_scope_only_for_target_clients(mock_redis):
    """client 스코프는 PER_APP_BUDGET_CLIENTS 에만 예약된다."""
    mock_redis.eval = AsyncMock(side_effect=_dispatch_by_scope({
        "team": _resv_resp(scope="team"),
        "user": _resv_resp(scope="user"),
        "client": _resv_resp(scope="client"),
    }))
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
        client="claude-code",
    )
    assert [s["scope"] for s in committed] == ["team", "user", "client"]
    assert committed[2]["usage_key"] == "budget:user:{u1}:claude-code:2026-05"

    committed2 = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-2",
        client="unknown-app",
    )
    assert [s["scope"] for s in committed2] == ["team", "user"]


@pytest.mark.asyncio
async def test_reserve_eval_failure_fail_open_with_refund(mock_redis):
    """eval 예외 → 커밋분 환불 후 빈 목록(fail-open = 기존 체크 동작으로 회귀)."""
    call_count = {"n": 0}

    async def _side(*args, **_kw):
        call_count["n"] += 1
        if args[0] == "-- settle":
            return _settle_resp()
        if args[7] == "user":
            raise ConnectionError("redis hiccup")
        return {"team": _resv_resp(scope="team")}[args[7]]

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=True)

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )
    assert committed == []


@pytest.mark.asyncio
async def test_reserve_d_fallback_synthesized(mock_redis):
    """user config 없음 + team default_user_cap_usd → D 합성 fallback 이 ARGV 로 전달."""
    seen: dict = {}

    async def _side(*args, **_kw):
        seen[args[7]] = args
        return _resv_resp(scope=args[7])

    mock_redis.eval = AsyncMock(side_effect=_side)
    mock_redis.exists = AsyncMock(return_value=False)
    mock_redis.get = AsyncMock(return_value=json.dumps({
        "limit_usd": "100",
        "default_user_cap_usd": "25",
        "policy": "hard_block",
    }).encode())

    svc = BudgetService()
    committed = await svc.reserve_budget(
        mock_redis, "u1", "t1", "2026-05", Decimal("4.5"), "req-1",
    )
    assert [s["scope"] for s in committed] == ["team", "user"]
    user_call = seen["user"]
    fallback = json.loads(user_call[9])  # (script,5,k1..k5,scope,est,fallback,ttl,field)
    assert fallback["limit_usd"] == "25"
    assert committed[1]["fallback"] == user_call[9]


@pytest.mark.asyncio
async def test_settle_budget_returns_threshold_and_scope(mock_redis):
    mock_redis.eval = AsyncMock(side_effect=[
        _settle_resp(threshold=90),
        _settle_resp(threshold=None),
    ])
    svc = BudgetService()
    scopes = [
        {"scope": "team", "usage_key": "k", "config_key": "c",
         "marker_key": "m1", "resvsum_key": "r1", "fallback": ""},
        {"scope": "user", "usage_key": "k", "config_key": "c",
         "marker_key": "m2", "resvsum_key": "r2", "fallback": ""},
    ]
    triggered, scope = await svc.settle_budget(mock_redis, scopes, Decimal("1.5"))
    assert triggered == 90
    assert scope == "team"


@pytest.mark.asyncio
async def test_settle_budget_eval_failure_continues(mock_redis):
    """한 scope 정산 실패가 다른 scope 를 막지 않는다."""
    calls = []

    async def _side(*args, **_kw):
        calls.append(args[4] if len(args) > 4 else None)
        if len(calls) == 1:
            raise ConnectionError("redis down")
        return _settle_resp()

    mock_redis.eval = AsyncMock(side_effect=_side)
    svc = BudgetService()
    scopes = [
        {"scope": "team", "usage_key": "k", "config_key": "c",
         "marker_key": "m1", "resvsum_key": "r1"},
        {"scope": "user", "usage_key": "k", "config_key": "c",
         "marker_key": "m2", "resvsum_key": "r2"},
    ]
    await svc.settle_budget(mock_redis, scopes, Decimal("1.5"))
    assert len(calls) == 2


def _mc(pricing_kwargs) -> "ModelConfigSchema":
    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider=ProviderType.BEDROCK,
        provider_model_id="m",
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        status=ModelStatus.ACTIVE,
        pricing=ModelPricingSchema(**pricing_kwargs),
    )


def test_estimate_worst_cost_uses_highest_input_rate():
    """캐시 쓰기(1h)가 입력보다 비싸면 최악비용은 그 단가를 쓴다."""
    from app.services.rate_limit_enforcement import _estimate_worst_cost

    mc = _mc({
        "input_per_1k": Decimal("0.003"),
        "output_per_1k": Decimal("0.015"),
        "cache_write_per_1k": Decimal("0.00375"),
        "cache_write_1h_per_1k": Decimal("0.006"),
        "cache_read_per_1k": Decimal("0.0003"),
    })
    # input=1000 tokens worst = 1h-cache-write rate 0.006 → 0.006 + 1000 out × 0.015
    cost = _estimate_worst_cost(mc, 1000, 1000)
    assert cost == Decimal("0.021000")


def test_estimate_worst_cost_long_tier():
    from app.services.rate_limit_enforcement import _estimate_worst_cost

    mc = _mc({
        "input_per_1k": Decimal("0.001"),
        "output_per_1k": Decimal("0.005"),
        "long_context_threshold_tokens": 200000,
        "long_input_per_1k": Decimal("0.002"),
        "long_output_per_1k": Decimal("0.0075"),
    })
    # 문자 추정 오차 마진 — 임계의 70%(140K) 를 넘으면 long 으로 과대예약한다.
    # 경계 아래 과소예약(실비 5배 초과)보다 경계 위 과대예약이 안전하다.
    short = _estimate_worst_cost(mc, 139999, 1000)
    long = _estimate_worst_cost(mc, 140001, 1000)
    # short: 139.999*0.001 + 0.005 ≈ 0.144999; long: 140.001*0.002 + 0.0075
    assert long > short
    assert long == Decimal("0.287502")


def test_estimate_cost_uses_long_tier():
    """kiro R18 S1 — CPM/CPH 선예약(_estimate_cost)도 long 요율을 본다.
    예전엔 short 만 봐서 100K 초과 요청이 분/시간 한도를 실비의 1/5 로 통과했다."""
    from app.services.rate_limit_enforcement import _estimate_cost

    mc = _mc({
        "input_per_1k": Decimal("0.0001"),
        "output_per_1k": Decimal("0.0005"),
        "long_context_threshold_tokens": 100000,
        "long_input_per_1k": Decimal("0.0005"),
        "long_output_per_1k": Decimal("0.0025"),
    })
    short = _estimate_cost(mc, 69000, 1000)   # 70% 경계 아래 — 단락 요율
    long = _estimate_cost(mc, 71000, 1000)    # 70% 경계 위 — 장문 요율
    assert short == Decimal("0.007400")       # 69*0.0001 + 0.0005
    assert long == Decimal("0.038000")        # 71*0.0005 + 0.0025
