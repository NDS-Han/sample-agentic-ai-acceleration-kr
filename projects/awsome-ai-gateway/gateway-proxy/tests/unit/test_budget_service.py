# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.budget_service import BudgetService


def _resp(*, allowed: bool, reason=None, scope: str,
          used_usd=0.0, remaining_usd=0.0, limit_usd=0.0,
          policy="hard_block", throttle_active=False, throttle_rpm_pct=50,
          threshold_pct=0, soft_warning=False, config_present=True):
    """Build a Lua response JSON for single-scope budget_check."""
    return json.dumps({
        "allowed": allowed,
        "reason": reason,
        "used_usd": used_usd,
        "remaining_usd": remaining_usd,
        "limit_usd": limit_usd,
        "policy": policy,
        "throttle_active": throttle_active,
        "throttle_rpm_pct": throttle_rpm_pct,
        "threshold_pct": threshold_pct,
        "soft_warning": soft_warning,
        "scope": scope,
        "config_present": config_present,
    }).encode()


def _eval_side_effect_user_team(user_resp: bytes, team_resp: bytes):
    """Return eval side_effect that alternates user -> team responses."""
    responses = [user_resp, team_resp]
    idx = {"i": 0}

    async def _side(*_args, **_kwargs):
        r = responses[idx["i"]]
        idx["i"] += 1
        return r

    return _side


@pytest.mark.asyncio
async def test_budget_allowed(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=2.0, remaining_usd=8.0, limit_usd=10.0),
        _resp(allowed=True, scope="team", used_usd=5.0, remaining_usd=95.0, limit_usd=100.0),
    ))

    svc = BudgetService()
    status = await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")
    assert status.remaining_usd > Decimal("0")


@pytest.mark.asyncio
async def test_budget_hard_block(mock_redis):
    """TEAM hard_block on exceeded usage."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=2.0, remaining_usd=8.0, limit_usd=10.0),
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0, threshold_pct=100),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")


@pytest.mark.asyncio
async def test_budget_team_unset_denies(mock_redis):
    """TEAM config 미설정(config_present=False) -> team_budget_unset."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),
        _resp(allowed=True, scope="team", config_present=False),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.check_budget(mock_redis, None, "user-1", "team-1", "2026-04")


@pytest.mark.asyncio
async def test_check_budget_uses_single_scope_per_eval(mock_redis):
    """Regression: 각 EVAL 은 2 KEYS 만 받고, 같은 hash tag 로 묶여 있어야 한다.

    Redis Cluster CROSSSLOT 재발 방지. user EVAL 의 KEYS 는 모두 {user_id} slot,
    team EVAL 의 KEYS 는 모두 {team_id} slot.
    """
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    lua_call = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=True, scope="team", used_usd=0, remaining_usd=100, limit_usd=100),
    ))
    mock_redis.eval = lua_call

    svc = BudgetService()
    await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")

    assert lua_call.call_count == 2, "EVAL 은 scope 당 1회씩 총 2회 호출되어야 함"

    # Call 1: USER scope  — Signature (script, num_keys, key1, key2, argv1)
    user_args = lua_call.call_args_list[0].args
    assert user_args[1] == 2, f"USER EVAL num_keys=2 기대, got {user_args[1]}"
    assert user_args[2] == "budget:user:{u1}:2026-04"
    assert user_args[3] == "budget:config:user:{u1}"
    assert user_args[4] == "user"

    # Call 2: TEAM scope
    team_args = lua_call.call_args_list[1].args
    assert team_args[1] == 2, f"TEAM EVAL num_keys=2 기대, got {team_args[1]}"
    assert team_args[2] == "budget:team:{t1}:2026-04"
    assert team_args[3] == "budget:config:team:{t1}"
    assert team_args[4] == "team"


@pytest.mark.asyncio
async def test_lowercase_contract_user_budget_exceeded(mock_redis):
    """USER hard_block → PermissionError args[0] == 'user_budget_exceeded'."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=False, reason="user_budget_exceeded", scope="user",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0, threshold_pct=100),
        _resp(allowed=True, scope="team"),   # 호출되지 않아야 함
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc_info:
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert str(exc_info.value) == "user_budget_exceeded"


@pytest.mark.asyncio
async def test_lowercase_contract_team_budget_exceeded(mock_redis):
    """TEAM hard_block → PermissionError args[0] == 'team_budget_exceeded'."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=200.0, remaining_usd=0.0, limit_usd=200.0, threshold_pct=100),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc_info:
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert str(exc_info.value) == "team_budget_exceeded"


@pytest.mark.asyncio
async def test_user_unset_team_exceeded_still_denies(mock_redis):
    """USER 미설정은 pass, TEAM 은 검사. TEAM 초과 시 deny."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),  # Q: USER 미설정
        _resp(allowed=False, reason="team_budget_exceeded", scope="team",
              used_usd=100.0, remaining_usd=0.0, limit_usd=100.0),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")


# ── Task B: TEAM config cold-cache DB fallback ──

@pytest.mark.asyncio
async def test_check_budget_hydrates_team_config_when_redis_miss():
    """team_config_key 가 Redis에 없을 때 DB에서 조회해 캐시를 채운다."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.budget import BudgetConfig, BudgetScope
    from app.models.budget import BudgetPolicy as OrmBudgetPolicy
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    team_id = "t1"

    fake_redis = MagicMock()
    # exists: team_config_key=False (cold-cache miss), user_config_key=False →
    # ensure_config_cached 후 재확인=True(재수화됨), user_key counter=False
    fake_redis.exists = AsyncMock(side_effect=[False, False, True, False])
    fake_redis.set = AsyncMock()
    fake_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", used_usd=0, remaining_usd=100, limit_usd=100),
        _resp(allowed=True, scope="team", used_usd=0, remaining_usd=100, limit_usd=100),
    ))
    fake_redis.get = AsyncMock(return_value=None)

    # DB mock: TEAM BudgetConfig 존재
    team_cfg = MagicMock(spec=BudgetConfig)
    team_cfg.scope = BudgetScope.TEAM
    team_cfg.scope_id = team_id
    team_cfg.max_budget_usd = Decimal("5000")
    team_cfg.policy = OrmBudgetPolicy.HARD_BLOCK
    team_cfg.is_active = True

    db_execute_result = MagicMock()
    db_execute_result.scalar_one_or_none = MagicMock(return_value=team_cfg)
    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(return_value=db_execute_result)

    svc = BudgetService()
    await svc.check_budget(fake_redis, fake_db, "u1", team_id, "2026-04")

    # budget:config:team:{t1} SET 이 한 번 이상 호출됐는지 확인
    set_calls = [c for c in fake_redis.set.call_args_list if "budget:config:team" in str(c.args[0])]
    assert len(set_calls) >= 1, "TEAM config cache 를 Redis 에 SET 해야 함"

    # EX=300 TTL 확인
    for c in set_calls:
        assert c.kwargs.get("ex") == 300, f"TTL 이 300 이어야 함, got {c.kwargs.get('ex')}"


@pytest.mark.asyncio
async def test_check_budget_no_set_when_db_has_no_team_config():
    """DB에 TEAM budget 없으면 Redis SET 없이 Lua 가 team_budget_unset 반환."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    fake_redis = MagicMock()
    # exists: team_config miss, user_config miss, ensure 후 재확인, user_key
    fake_redis.exists = AsyncMock(side_effect=[False, False, False, False])
    fake_redis.set = AsyncMock()
    # user pass (config 없음 → pass-through), team config 없음 → team_budget_unset
    fake_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=True, scope="user", config_present=False),
        _resp(allowed=True, scope="team", config_present=False),
    ))
    fake_redis.get = AsyncMock(return_value=None)

    # DB mock: TEAM BudgetConfig 없음
    db_execute_result = MagicMock()
    db_execute_result.scalar_one_or_none = MagicMock(return_value=None)
    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(return_value=db_execute_result)

    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.check_budget(fake_redis, fake_db, "u1", "t1", "2026-04")

    # DB에 없으므로 budget:config:team 키 SET 없어야 함
    set_calls = [c for c in fake_redis.set.call_args_list if "budget:config:team" in str(c.args[0])]
    assert len(set_calls) == 0, "DB에 TEAM budget 없으면 Redis SET 하지 않아야 함"


# ── Slice 5: CAP 모델 enforcement (cap_u = A_u ?? D, D-8, §6-3/§6-5) ──

def _db_result(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


@pytest.mark.asyncio
async def test_no_team_assigned_blocks(mock_redis):
    """D-14/§6-5 단계 0: team_id='' 인 유저는 fail-closed — no_team_assigned."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"
    svc = BudgetService()
    with pytest.raises(PermissionError, match="no_team_assigned"):
        await svc.check_budget(mock_redis, None, "u1", "", "2026-04")
    mock_redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_d_fallback_synthesizes_user_eval_config(mock_redis):
    """D-3: 개인 config 없음 + 팀 D → user EVAL 에 합성 config(ARGV[2])로 D 주입."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"

    team_cfg_json = json.dumps({
        "limit_usd": "100", "policy": "soft_warning",
        "thresholds": [70, 90], "default_user_cap_usd": "2.50",
    })
    mock_redis.exists = AsyncMock(return_value=False)  # user config 없음
    mock_redis.get = AsyncMock(return_value=team_cfg_json)

    calls = []

    async def _eval(*args):
        calls.append(args)
        if args[4] == "user":
            return _resp(allowed=True, scope="user", used_usd=1.0,
                         remaining_usd=1.5, limit_usd=2.5, policy="soft_warning")
        return _resp(allowed=True, scope="team", used_usd=10.0,
                     remaining_usd=90.0, limit_usd=100.0)

    mock_redis.eval = AsyncMock(side_effect=_eval)

    svc = BudgetService()
    status = await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")

    user_args = calls[0]
    fallback = json.loads(user_args[5])
    assert fallback["limit_usd"] == "2.50"
    assert fallback["policy"] == "soft_warning"     # D 유저는 팀 정책을 따른다
    assert fallback["thresholds"] == [70, 90]
    assert status.tier == "user"                    # 잔여 1.5 < 팀 90 → user 결정
    assert status.remaining_usd > Decimal("0")


@pytest.mark.asyncio
async def test_d_cap_blocks_unset_user(mock_redis):
    """D=2, 사용 3 → 개인 예산 없이도 user_budget_exceeded 차단."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"
    mock_redis.exists = AsyncMock(return_value=False)
    mock_redis.get = AsyncMock(return_value=json.dumps({
        "limit_usd": "100", "policy": "hard_block", "default_user_cap_usd": "2",
    }))
    mock_redis.eval = AsyncMock(side_effect=_eval_side_effect_user_team(
        _resp(allowed=False, reason="user_budget_exceeded", scope="user",
              used_usd=3.0, remaining_usd=-1.0, limit_usd=2.0),
        _resp(allowed=True, scope="team"),
    ))

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc:
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert str(exc.value) == "user_budget_exceeded"


@pytest.mark.asyncio
async def test_no_fallback_when_individual_budget_exists(mock_redis):
    """개인 config 가 있으면 팀 D 와 무관하게 ARGV[2] 는 빈 문자열."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "-- mock"
    mock_redis.exists = AsyncMock(return_value=True)  # user config 있음
    mock_redis.get = AsyncMock(return_value=json.dumps({
        "limit_usd": "100", "policy": "hard_block", "default_user_cap_usd": "2",
    }))

    calls = []

    async def _eval(*args):
        calls.append(args)
        return _resp(allowed=True, scope=args[4], used_usd=1.0,
                     remaining_usd=9.0, limit_usd=10.0)

    mock_redis.eval = AsyncMock(side_effect=_eval)

    svc = BudgetService()
    await svc.check_budget(mock_redis, None, "u1", "t1", "2026-04")
    assert calls[0][5] == "", "개인 cap 이 있으면 fallback 을 넘기면 안 된다"


def test_build_status_min_remaining_across_tiers():
    """§6-5: remaining = 모든 계층 '차단까지 잔여'의 최솟값, 결정 tier 기록."""
    from app.services.budget_service import _build_status

    team = {"limit_usd": 100, "used_usd": 50, "policy": "hard_block",
            "threshold_pct": 50, "soft_warning": False, "throttle_active": False}
    tiers = [
        ("user", {"limit_usd": 10, "used_usd": 9, "policy": "hard_block",
                  "soft_warning": False, "throttle_active": False}),
        ("team", team),
        ("client", {"limit_usd": 5, "used_usd": 4.6, "policy": "hard_block",
                    "soft_warning": False, "throttle_active": False}),
    ]
    s = _build_status(tiers, team)
    assert s.tier == "client"
    assert s.remaining_usd == Decimal("0.4")
    assert s.limit_usd == Decimal("5")


def test_build_status_soft_warning_uses_effective_limit():
    """SOFT_WARNING 계층은 1.10×limit 기준 잔여로 후보에 들어간다."""
    from app.services.budget_service import _build_status

    team = {"limit_usd": 100, "used_usd": 105, "policy": "soft_warning",
            "threshold_pct": 105, "soft_warning": True, "throttle_active": False}
    tiers = [
        ("user", {"limit_usd": 10, "used_usd": 0, "policy": "hard_block",
                  "soft_warning": False, "throttle_active": False}),
        ("team", team),
    ]
    s = _build_status(tiers, team)
    # team: 110-105=5 vs user: 10-0=10 → team 결정, 경고 계층 기록
    assert s.tier == "team"
    assert s.remaining_usd == Decimal("5")
    assert s.warning_tiers == ["team"]
    assert s.soft_warning is True


def test_build_status_throttle_excluded_and_min_rpm():
    """THROTTLE 계층은 잔여 후보 제외 + 활성 계층 중 가장 작은 rpm_pct (§6-3)."""
    from app.services.budget_service import _build_status

    team = {"limit_usd": 100, "used_usd": 95, "policy": "throttle",
            "threshold_pct": 95, "soft_warning": False,
            "throttle_active": True, "throttle_rpm_pct": 30}
    tiers = [
        ("user", {"limit_usd": 10, "used_usd": 0, "policy": "hard_block",
                  "soft_warning": False, "throttle_active": True,
                  "throttle_rpm_pct": 50}),
        ("team", team),
    ]
    s = _build_status(tiers, team)
    assert s.tier == "user"                        # team(throttle)은 후보 제외
    assert s.throttle_active is True
    assert s.throttle_rpm_pct == 30                # 최솟값 적용


def test_evaluate_layer_zero_limit_blocks_all_policies():
    """D-8: limit=0 은 정책 무관 차단 — 예전엔 SOFT/THROTTLE 에서 통과했다."""
    from app.schemas.domain import BudgetPolicy
    from app.services.budget_service import _evaluate_layer

    for policy in BudgetPolicy:
        block, _, _ = _evaluate_layer(Decimal("0"), Decimal("0"), policy)
        assert block == "budget_exceeded", f"{policy} 에서 limit=0 이 통과했다"


@pytest.mark.asyncio
async def test_db_fallback_d_blocks_unset_user():
    """DB 폴백: 개인 cap 없음 + D=2 + 사용 3 → user_budget_exceeded."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.budget import BudgetConfig, BudgetUsage
    from app.models.budget import BudgetPolicy as OrmBudgetPolicy

    team_cfg = MagicMock(spec=BudgetConfig)
    team_cfg.max_budget_usd = Decimal("100")
    team_cfg.policy = OrmBudgetPolicy.HARD_BLOCK
    team_cfg.default_user_cap_usd = Decimal("2")

    usage = MagicMock(spec=BudgetUsage)
    usage.used_usd = Decimal("3")

    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(side_effect=[
        _db_result(None),        # user config — 없음
        _db_result(team_cfg),    # team config
        _db_result(usage),       # user usage (D 평가용)
    ])

    svc = BudgetService()
    with pytest.raises(PermissionError) as exc:
        await svc.check_budget(None, fake_db, "u1", "t1", "2026-04")
    assert str(exc.value) == "user_budget_exceeded"


@pytest.mark.asyncio
async def test_db_fallback_d_user_status_tier():
    """DB 폴백: D 유저 허용 — 결정 계층이 user(D 잔여 < 팀 잔여)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.budget import BudgetConfig, BudgetUsage
    from app.models.budget import BudgetPolicy as OrmBudgetPolicy

    team_cfg = MagicMock(spec=BudgetConfig)
    team_cfg.max_budget_usd = Decimal("100")
    team_cfg.policy = OrmBudgetPolicy.HARD_BLOCK
    team_cfg.default_user_cap_usd = Decimal("10")

    user_usage = MagicMock(spec=BudgetUsage)
    user_usage.used_usd = Decimal("8")
    team_usage = MagicMock(spec=BudgetUsage)
    team_usage.used_usd = Decimal("8")

    fake_db = MagicMock(spec=AsyncSession)
    fake_db.execute = AsyncMock(side_effect=[
        _db_result(None),          # user config
        _db_result(team_cfg),      # team config
        _db_result(user_usage),    # user usage (D 평가)
        _db_result(team_usage),    # team usage
    ])

    svc = BudgetService()
    status = await svc.check_budget(None, fake_db, "u1", "t1", "2026-04")
    # user D 잔여 = 2 vs team 잔여 = 92 → user 결정
    assert status.tier == "user"
    assert status.remaining_usd == Decimal("2")
