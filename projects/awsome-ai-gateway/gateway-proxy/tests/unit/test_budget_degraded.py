# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""wave 1c — 예산 체크 열화 모드: 마지막으로 알던 값으로 판정, 모를 때만 유예 창.

계약 (SPEC 1c):
  1. 정책 캐시 + 모든 핫패스 DB 읽기 데드라인(hot_db_timeout_sec) — DB 가 느리거나 죽으면 캐시 값.
  2. 지출은 Redis 카운터(Lua). DB 는 제한된 폴백.
  3. 알던 초과/차단 상태는 DB(·Redis) 장애 중에도 429.
  4. 정책도 상태도 모르면 유예 창(BUDGET_FAILOPEN_GRACE_SECONDS) 안에서만 허용, 그 뒤 503.
     gateway_budget_degraded_total{reason} + warning 로그.
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import budget_degraded as bd
from app.services.budget_service import BudgetBackendUnavailableError, BudgetService
from app.services.lua_loader import LuaScriptLoader

PERIOD = "2026-11"


def _resp(scope, *, allowed=True, reason=None, used=0.0, limit=100.0, present=True,
          policy="hard_block", app_clients=None):
    d = {
        "allowed": allowed, "reason": reason, "used_usd": used,
        "remaining_usd": limit - used, "limit_usd": limit, "policy": policy,
        "throttle_active": False, "throttle_rpm_pct": 50, "threshold_pct": 0,
        "soft_warning": False, "scope": scope, "config_present": present,
    }
    if app_clients is not None:
        d["app_clients"] = app_clients
    return json.dumps(d).encode()


def _evals(user=None, team=None, client=None):
    async def _eval(script, numkeys, *args, **_kw):
        label = args[2] if numkeys == 2 else None
        return {"user": user, "team": team, "client": client}[label]
    return _eval


class _Counter:
    def __init__(self):
        self.calls: list[str] = []

    def add(self, n, attrs):
        self.calls.append(attrs["reason"])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _lua():
    LuaScriptLoader._scripts["budget_check"] = "-- mock"
    LuaScriptLoader._scripts["budget_rebuild"] = "-- mock"


@pytest.fixture
def counter():
    c = _Counter()
    bd.DEGRADED.set_metric(c)
    yield c
    bd.DEGRADED.set_metric(None)


@pytest.fixture
def clock():
    """열화 상태 전부(정책·상태 캐시, 유예 창, Redis 브레이커)를 가짜 시계로."""
    c = _Clock()
    saved = bd.DEGRADED
    bd.DEGRADED = bd.BudgetDegraded(clock=c)
    yield c
    bd.DEGRADED = saved


def _down_db():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=ConnectionError("db down"))
    return db


def _no_rows_db():
    """BudgetConfig/BudgetUsage 조회에 '행 없음' 을 돌려주는 정상 DB."""
    db = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=None)
    res.scalar_one = MagicMock(return_value=Decimal("0"))
    res.all = MagicMock(return_value=[])
    res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=res)
    return db


def _healthy_counters(mock_redis):
    """카운터가 살아 있다 — 복원(DB) 경로를 타지 않게."""
    mock_redis.mget = AsyncMock(
        side_effect=lambda keys: [None if k.endswith(":lost") else b"1.0" for k in keys]
    )


# ── 3. 알던 차단은 장애 중에도 429 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_known_over_budget_user_stays_blocked_when_redis_and_db_are_down(mock_redis, counter):
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=10.0, limit=10.0),
        team=_resp("team", used=5.0, limit=1000.0),
    ))
    svc = BudgetService()
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "u1", "t1", PERIOD)

    # 이제 Redis 도 DB 도 답하지 않는다.
    mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
    with pytest.raises(PermissionError) as exc:
        await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)
    assert str(exc.value) == "user_budget_exceeded"
    assert not isinstance(exc.value, BudgetBackendUnavailableError)
    assert counter.calls[-1] == bd.R_LAST_KNOWN_BLOCK


@pytest.mark.asyncio
async def test_known_team_block_wins_even_if_this_user_was_never_seen(mock_redis, counter):
    """팀 초과는 팀 단위 상태다 — 이 파드가 처음 보는 사용자도 막힌다."""
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=False),
        team=_resp("team", allowed=False, reason="team_budget_exceeded", used=100.0, limit=100.0),
    ))
    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(mock_redis, None, "u1", "t1", PERIOD)

    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(None, _down_db(), "u-new", "t1", PERIOD)
    assert counter.calls[-1] == bd.R_LAST_KNOWN_BLOCK


def _team_cfg_get(team_cfg: dict):
    async def _get(k):
        return json.dumps(team_cfg) if k == "budget:config:team:{t1}" else None
    return _get


@pytest.mark.asyncio
async def test_db_down_redis_up_over_budget_user_is_blocked_by_redis(mock_redis):
    """DB 장애 + Redis 정상: '개인 예산 없음' 을 알면 팀 기본 cap(D)으로 Redis 가 판정한다.

    초과는 그대로 429."""
    _healthy_counters(mock_redis)

    async def _exists(k):
        return 0 if k == "budget:config:user:{u1}" else 1

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.get = AsyncMock(side_effect=_team_cfg_get(
        {"limit_usd": "100", "policy": "hard_block", "default_user_cap_usd": "5"}))
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=6.0, limit=5.0),
        team=_resp("team", used=6.0, limit=100.0),
    ))
    svc = BudgetService()
    healthy = _no_rows_db()  # 1st request: DB answers "no personal budget"
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await svc.check_budget(mock_redis, healthy, "u1", "t1", PERIOD)
    assert healthy.execute.await_count >= 1
    assert bd.DEGRADED.policy.fresh(bd.PolicyCache.user_key("u1")) is bd.ABSENT

    down = _down_db()
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await svc.check_budget(mock_redis, down, "u1", "t1", PERIOD)
    assert down.execute.await_count <= 1, "DB 장애 중 요청당 한 번 넘게 DB 를 기다렸다"


@pytest.mark.asyncio
async def test_known_absence_does_not_hide_a_new_personal_budget_while_db_is_up(mock_redis):
    """admin-api 는 설정 키를 DEL 만 하고 게이트웨이가 미스마다 DB 에서 다시 도출한다(H3).
    프로세스 캐시가 '개인 예산 없음' 을 알아도 DB 가 닿으면 DB 를 읽어야 한다 — 아니면 방금
    만든 개인 예산이 캐시 수명(최대 900 s) 동안 적용되지 않는다."""
    from app.models.budget import BudgetPolicy as OrmPolicy

    bd.DEGRADED.policy.put(bd.PolicyCache.user_key("u1"), None)  # 예전에 본 '없음'
    new_cfg = MagicMock(max_budget_usd=Decimal("5"), policy=OrmPolicy.HARD_BLOCK,
                        alert_thresholds=[80])
    db = AsyncMock()
    r_cfg = MagicMock()
    r_cfg.scalar_one_or_none = MagicMock(return_value=new_cfg)
    r_apps = MagicMock()
    r_apps.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(side_effect=[r_cfg, r_apps])

    _healthy_counters(mock_redis)
    written: dict = {}

    async def _set(k, v, ex=None, **_kw):
        written[k] = v
        return True

    async def _exists(k):
        if k == "budget:config:user:{u1}":
            return int(k in written)
        return 1

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=6.0, limit=5.0),
        team=_resp("team", used=6.0, limit=100.0)))
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await BudgetService().check_budget(mock_redis, db, "u1", "t1", PERIOD)
    assert "budget:config:user:{u1}" in written
    assert bd.DEGRADED.policy.fresh(bd.PolicyCache.user_key("u1"))["limit_usd"] == "5"
    mock_redis.delete.assert_any_await("budget:config:user:{u1}:none")


# ── 1. 정책 캐시 (stale) + DB 데드라인 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_personal_budget_marker_is_shared_through_redis(mock_redis, counter):
    """정책 캐시는 프로세스마다 따로다. 다른 워커/파드가 DB 에서 '개인 예산 없음' 을 확인했으면
    Redis 표지로 공유돼, 이 프로세스가 처음 보는 사용자도 DB 장애 중 유예 창으로 가지 않는다."""
    _healthy_counters(mock_redis)
    store: dict = {}

    async def _exists(k):
        return 0 if k == "budget:config:user:{u1}" else 1

    async def _set(k, v, ex=None, **_kw):
        store[k] = (v, ex)
        return True

    async def _get(k):
        return store.get(k, (None,))[0]

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=False), team=_resp("team", used=1.0, limit=1000.0)))
    svc = BudgetService()
    await svc.check_budget(mock_redis, _no_rows_db(), "u1", "t1", PERIOD)  # worker A, DB up
    assert store["budget:config:user:{u1}:none"] == ("1", 300)

    bd.DEGRADED.reset()  # worker B: 같은 Redis, 빈 프로세스 캐시
    status = await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)
    assert status is not None, "표지가 있는데 유예 창으로 갔다"
    assert bd.R_GRACE_ALLOW not in counter.calls


@pytest.mark.asyncio
async def test_real_personal_config_key_wins_over_the_marker(mock_redis):
    """설정 키가 있으면 표지는 보지 않는다 — admin 이 개인 예산을 만들면 즉시 적용."""
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=1)
    mock_redis.get = AsyncMock(return_value=b"1")
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=6.0, limit=5.0),
        team=_resp("team", used=6.0, limit=100.0)))
    with pytest.raises(PermissionError, match="user_budget_exceeded"):
        await BudgetService().check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)


@pytest.mark.asyncio
async def test_team_config_miss_with_db_down_uses_last_known_policy(mock_redis, counter):
    _healthy_counters(mock_redis)
    team_cfg = {"limit_usd": "1000", "policy": "hard_block", "thresholds": [80],
                "default_user_cap_usd": None}
    bd.DEGRADED.policy.put(bd.PolicyCache.team_key("t1"), team_cfg)
    bd.DEGRADED.policy.put(bd.PolicyCache.user_key("u1"), None)

    async def _exists(k):
        return 0 if "config" in k else 1

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=False), team=_resp("team", used=1.0, limit=1000.0)))
    status = await BudgetService().check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)

    assert status is not None and status.limit_usd == Decimal("1000")
    sets = [c for c in mock_redis.set.call_args_list if c.args[0] == "budget:config:team:{t1}"]
    assert sets, "캐시된 팀 정책을 Redis 에 다시 써야 reserve 경로도 같은 설정을 본다"
    assert json.loads(sets[0].args[1]) == team_cfg
    assert sets[0].kwargs["ex"] == 60, "열화 중 쓴 설정은 짧은 TTL — 복구 후 DB 값이 곧 대체한다"
    assert bd.R_STALE_POLICY in counter.calls


@pytest.mark.asyncio
async def test_policy_older_than_stale_max_is_not_used(mock_redis, clock, counter):
    _healthy_counters(mock_redis)
    bd.DEGRADED.policy.put(bd.PolicyCache.team_key("t1"), {"limit_usd": "1000",
                                                          "policy": "hard_block"})
    clock.t += 901  # BUDGET_STALE_MAX_SECONDS 기본 900

    async def _exists(k):
        return 0 if k == "budget:config:team:{t1}" else 1

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=False), team=_resp("team", present=False)))
    # 정책을 모른다 → Lua 의 '키 없음' 을 team_budget_unset(429) 로 읽지 않고 유예 창으로.
    status = await BudgetService().check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)
    assert status is None
    assert counter.calls[-1] == bd.R_GRACE_ALLOW
    assert mock_redis.eval.await_count == 0


@pytest.mark.asyncio
async def test_slow_db_is_bounded_and_opens_the_breaker(mock_redis):
    from app.services import hot_path

    _healthy_counters(mock_redis)
    bd.DEGRADED.policy.put(bd.PolicyCache.team_key("t1"), {"limit_usd": "1000",
                                                          "policy": "hard_block"})

    async def _exists(k):
        return 0 if k == "budget:config:team:{t1}" else 1

    async def _slow(*_a, **_k):
        await asyncio.sleep(5)

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=True), team=_resp("team", used=1.0, limit=1000.0)))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_slow)
    t0 = time.monotonic()
    status = await BudgetService().check_budget(mock_redis, db, "u1", "t1", PERIOD)
    assert time.monotonic() - t0 < 1.0, "DB 데드라인(0.3 s)을 넘겨 기다렸다"
    assert status is not None
    assert not hot_path.DB_BREAKER.available(), "타임아웃이 프로세스 DB 브레이커를 열어야 한다"


# ── 4. 유예 창 — 양쪽 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_policy_and_state_allows_within_grace_then_fails_closed(clock, counter,
                                                                              caplog):
    svc = BudgetService()
    # 콜드 파드 + Redis·DB 둘 다 불가 + 아무것도 모름 → 유예 창 안: 허용(None)
    assert await svc.check_budget(None, _down_db(), "u1", "t1", PERIOD) is None
    clock.t += 299
    assert await svc.check_budget(None, _down_db(), "u2", "t1", PERIOD) is None
    assert counter.calls.count(bd.R_GRACE_ALLOW) == 2

    clock.t += 2  # 301 s > BUDGET_FAILOPEN_GRACE_SECONDS(300)
    with pytest.raises(BudgetBackendUnavailableError):
        await svc.check_budget(None, _down_db(), "u3", "t1", PERIOD)
    assert counter.calls[-1] == bd.R_GRACE_EXPIRED


@pytest.mark.asyncio
async def test_grace_window_resets_after_a_successful_budget_db_read(mock_redis, clock):
    svc = BudgetService()
    assert await svc.check_budget(None, _down_db(), "u1", "t1", PERIOD) is None
    clock.t += 400
    with pytest.raises(BudgetBackendUnavailableError):
        await svc.check_budget(None, _down_db(), "u1", "t1", PERIOD)

    # DB 가 돌아왔다 — 예산 DB 읽기 성공(team_budget_unset 판정)이 유예 시계를 리셋한다.
    from app.services import hot_path

    hot_path.DB_BREAKER.reset()
    with pytest.raises(PermissionError, match="team_budget_unset"):
        await svc.check_budget(None, _no_rows_db(), "u1", "t-other", PERIOD)
    assert bd.DEGRADED.grace.started_at is None
    assert await svc.check_budget(None, _down_db(), "u9", "t9", PERIOD) is None


@pytest.mark.asyncio
async def test_grace_decisions_are_logged_at_warning_every_time(clock, capsys):
    svc = BudgetService()
    for u in ("u1", "u2", "u3"):
        await svc.check_budget(None, _down_db(), u, "t1", PERIOD)
    out = capsys.readouterr().out
    assert out.count("reason=grace_allow") == 3


# ── 마지막 상태로 허용 / 오래된 상태 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_known_allowed_state_is_reused_when_both_backends_fail(mock_redis, counter):
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", used=2.0, limit=10.0, app_clients=[]),
        team=_resp("team", used=5.0, limit=1000.0)))
    svc = BudgetService()
    first = await svc.check_budget(mock_redis, None, "u1", "t1", PERIOD, client="claude-code")

    mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
    again = await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD,
                                   client="claude-code")
    assert again is not None
    assert again.remaining_usd == first.remaining_usd == Decimal("8.0")
    assert counter.calls[-1] == bd.R_LAST_KNOWN_ALLOW


@pytest.mark.asyncio
async def test_last_known_state_expires_after_stale_max(mock_redis, clock):
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=10.0, limit=10.0),
        team=_resp("team", used=5.0, limit=1000.0)))
    svc = BudgetService()
    with pytest.raises(PermissionError):
        await svc.check_budget(mock_redis, None, "u1", "t1", PERIOD)
    clock.t += 901
    mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
    # 15 분이 지난 상태는 쓰지 않는다 → 모름 → 유예 창
    assert await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD) is None


@pytest.mark.asyncio
async def test_new_month_does_not_reuse_last_months_block(mock_redis):
    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", allowed=False, reason="user_budget_exceeded", used=10.0, limit=10.0),
        team=_resp("team", used=5.0, limit=1000.0)))
    svc = BudgetService()
    with pytest.raises(PermissionError):
        await svc.check_budget(mock_redis, None, "u1", "t1", "2026-10")
    mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
    assert await svc.check_budget(mock_redis, _down_db(), "u1", "t1", "2026-11") is None


@pytest.mark.asyncio
async def test_db_fallback_result_is_remembered(clock):
    """Redis 다운 → DB 폴백 성공 → 그 뒤 DB 도 다운: DB 폴백이 본 상태로 판정한다."""
    from app.models.budget import BudgetPolicy as OrmPolicy

    team = MagicMock(max_budget_usd=Decimal("100"), default_user_cap_usd=None,
                     policy=OrmPolicy.HARD_BLOCK, alert_thresholds=[80, 90, 100])
    usage = MagicMock(used_usd=Decimal("100"))

    def _r(v):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=v)
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_r(None), _r(team), _r(usage)])
    svc = BudgetService()
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(None, db, "u1", "t1", PERIOD)
    with pytest.raises(PermissionError, match="team_budget_exceeded"):
        await svc.check_budget(None, _down_db(), "u1", "t1", PERIOD)


# ── Redis 데드라인 + 브레이커 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_hang_is_bounded_and_skipped_for_the_cooldown(mock_redis, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "budget_redis_timeout_sec", 0.2)

    async def _hang(*_a, **_k):
        await asyncio.sleep(10)

    mock_redis.exists = AsyncMock(side_effect=_hang)
    svc = BudgetService()
    t0 = time.monotonic()
    assert await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD) is None
    assert time.monotonic() - t0 < 1.0
    assert not bd.DEGRADED.redis_breaker.available()

    calls = mock_redis.exists.await_count
    t0 = time.monotonic()
    await svc.check_budget(mock_redis, _down_db(), "u1", "t1", PERIOD)
    assert time.monotonic() - t0 < 0.1, "브레이커가 열렸는데 Redis 를 다시 기다렸다"
    assert mock_redis.exists.await_count == calls


# ── 백그라운드 정책 재조회 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_refresh_loads_team_and_user_policy(mock_redis):
    from app.models.budget import BudgetPolicy as OrmPolicy

    team = MagicMock(max_budget_usd=Decimal("500"), default_user_cap_usd=Decimal("20"),
                     policy=OrmPolicy.SOFT_WARNING, alert_thresholds=[50])
    user_total = MagicMock(client=None, max_budget_usd=Decimal("30"),
                           policy=OrmPolicy.HARD_BLOCK, alert_thresholds=[90])
    user_app = MagicMock(client="codex", max_budget_usd=Decimal("10"),
                         policy=OrmPolicy.HARD_BLOCK, alert_thresholds=[])

    async def _execute(stmt, *_a, **_k):
        res = MagicMock()
        sql = str(stmt)
        res.scalar_one_or_none = MagicMock(return_value=team)
        res.scalars = MagicMock(return_value=MagicMock(
            all=MagicMock(return_value=[user_total, user_app])))
        assert "budget_configs" in sql
        return res

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    _healthy_counters(mock_redis)
    mock_redis.exists = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", used=1.0, limit=30.0), team=_resp("team", used=1.0, limit=500.0)))
    await BudgetService().check_budget(mock_redis, None, "u1", "t1", PERIOD,
                                       session_factory=factory)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if bd.DEGRADED.policy.fresh(bd.PolicyCache.user_key("u1")) is not None and \
                bd.DEGRADED.policy.fresh(bd.PolicyCache.team_key("t1")) is not None:
            break
    pc = bd.DEGRADED.policy
    assert pc.fresh(pc.team_key("t1")) == {
        "limit_usd": "500", "policy": "soft_warning", "thresholds": [50],
        "default_user_cap_usd": "20"}
    assert pc.fresh(pc.user_key("u1"))["app_clients"] == ["codex"]
    assert pc.fresh(pc.client_key("u1", "codex"))["limit_usd"] == "10"
    assert pc.fresh(pc.client_key("u1", "claude-code")) is bd.ABSENT


@pytest.mark.asyncio
async def test_background_refresh_writes_absence_markers(mock_redis):
    """백그라운드 재조회가 확인한 '행 없음' 은 Redis 표지로 공유된다(다른 워커·파드용)."""
    from app.models.budget import BudgetPolicy as OrmPolicy

    user_total = MagicMock(client=None, max_budget_usd=Decimal("30"),
                           policy=OrmPolicy.HARD_BLOCK, alert_thresholds=[90])

    async def _execute(stmt, *_a, **_k):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=None)
        res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[user_total])))
        return res

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    BudgetService()._schedule_policy_refresh(factory, "u1", "t1", "codex", redis=mock_redis)
    await asyncio.sleep(0.05)
    set_keys = {c.args[0] for c in mock_redis.set.await_args_list}
    assert {f"budget:config:user:{{u1}}:{c}:none" for c in ("claude-code", "cowork", "codex")} \
        <= set_keys
    assert "budget:config:user:{u1}:none" not in set_keys, "총예산이 있는데 '없음' 표지를 썼다"
    mock_redis.delete.assert_any_await("budget:config:user:{u1}:none")


@pytest.mark.asyncio
async def test_background_refresh_backs_off_after_failure(mock_redis):
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=ConnectionError("db down"))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = BudgetService()
    svc._schedule_policy_refresh(factory, "u1", "t1")
    await asyncio.sleep(0.05)
    n = session.execute.await_count
    assert n == 2  # team + user
    svc._schedule_policy_refresh(factory, "u1", "t1")
    await asyncio.sleep(0.05)
    assert session.execute.await_count == n, "실패한 키를 재시도 대기 없이 다시 조회했다"


@pytest.mark.asyncio
async def test_budget_db_reads_use_the_main_pool_not_the_auth_hot_pool(mock_redis, monkeypatch):
    """wave 1 hot-path 풀(4+2)은 인증 전용 — 예산 읽기는 미들웨어의 메인 풀 팩토리 + 데드라인."""
    from app.services import hot_path

    def _hot_pool_must_not_be_used():
        raise AssertionError("budget read went to the auth hot-path pool")

    monkeypatch.setattr(hot_path, "_HOT_SESSION_FACTORY", _hot_pool_must_not_be_used)
    session = _no_rows_db()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(BudgetService, "_schedule_policy_refresh", lambda *a, **k: None)
    _healthy_counters(mock_redis)

    async def _exists(k):
        return 0 if k == "budget:config:user:{u1}" else 1

    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.eval = AsyncMock(side_effect=_evals(
        user=_resp("user", present=False), team=_resp("team", used=1.0, limit=1000.0)))
    await BudgetService().check_budget(mock_redis, None, "u1", "t1", PERIOD,
                                       session_factory=factory)
    assert session.execute.await_count >= 1
    assert bd.DEGRADED.policy.fresh(bd.PolicyCache.user_key("u1")) is bd.ABSENT


@pytest.mark.asyncio
async def test_stale_app_policy_triggers_the_user_loader(monkeypatch):
    calls = []

    async def _fake_loader(self, db, ident):
        calls.append(ident)

    monkeypatch.setattr(BudgetService, "_load_user_policy", _fake_loader)
    monkeypatch.setattr(BudgetService, "_load_team_policy", _fake_loader)
    bd.DEGRADED.policy.put(bd.PolicyCache.user_key("u1"), None)
    bd.DEGRADED.policy.put(bd.PolicyCache.team_key("t1"), None)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    svc = BudgetService()
    svc._schedule_policy_refresh(factory, "u1", "t1", None)
    await asyncio.sleep(0.02)
    assert calls == [], "모두 신선한데 재조회했다"
    svc._schedule_policy_refresh(factory, "u1", "t1", "claude-code")
    await asyncio.sleep(0.02)
    assert calls == ["u1"], "이 요청의 앱 정책을 모르는데 사용자 로더를 돌리지 않았다"


# ── 메트릭 / 미들웨어 / 예약 ───────────────────────────────────────────────────


def test_degraded_metric_is_registered():
    from app.observability.metrics import GatewayMetrics

    m = GatewayMetrics()
    assert hasattr(m, "budget_degraded_total")


def test_configure_reads_settings_and_keeps_the_metric():
    c = _Counter()
    bd.DEGRADED.set_metric(c)
    saved = bd.DEGRADED
    try:
        bd.configure(SimpleNamespace(
            budget_policy_refresh_sec=30.0, budget_stale_max_seconds=600.0,
            budget_failopen_grace_seconds=120.0, budget_local_cache_max_entries=10,
            hot_db_breaker_cooldown_sec=1.0, budget_policy_refresh_retry_sec=5.0))
        assert bd.DEGRADED.grace.seconds == 120.0
        assert bd.DEGRADED.stale_max_sec == 600.0
        assert bd.DEGRADED._counter is c
    finally:
        bd.DEGRADED = saved


@pytest.mark.asyncio
async def test_middleware_grace_passes_through_without_budget_status(monkeypatch):
    from app.middleware import budget as budget_mod
    from app.middleware.budget import BudgetMiddleware

    monkeypatch.setattr(budget_mod._budget_service, "check_budget", AsyncMock(return_value=None))
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope["state"])

    scope = {"type": "http", "path": "/v1/messages",
             "state": {"auth_context": SimpleNamespace(user_id="u1", team_id="t1")}}
    await BudgetMiddleware(app)(scope, None, AsyncMock())
    assert seen["budget_degraded"] == "grace"
    assert "budget_status" not in seen


@pytest.mark.asyncio
async def test_middleware_passes_session_factory_and_db_degraded(monkeypatch):
    from app.middleware import budget as budget_mod
    from app.middleware.budget import BudgetMiddleware
    from app.schemas.domain import DegradationLevel

    check = AsyncMock(return_value=None)
    monkeypatch.setattr(budget_mod._budget_service, "check_budget", check)
    sf = object()
    scope = {"type": "http", "path": "/v1/messages",
             "state": {"auth_context": SimpleNamespace(user_id="u1", team_id="t1"),
                       "_session_factory": sf,
                       "_degradation_manager": SimpleNamespace(
                           level=DegradationLevel.DB_DEGRADED)}}
    await BudgetMiddleware(AsyncMock())(scope, None, AsyncMock())
    kw = check.await_args.kwargs
    assert kw["session_factory"] is sf and kw["db_degraded"] is True
    assert check.await_args.args[1] is None, "요청 내내 쥐는 세션을 넘기면 안 된다"


def test_reservation_is_skipped_for_grace_requests():
    """유예 통과 요청은 월예산 선예약을 건너뛴다 — 그 외 경로는 그대로 예약한다."""
    import ast
    import inspect

    from app.services import rate_limit_enforcement as rle

    tree = ast.parse(inspect.getsource(rle))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "budget_degraded" in ast.unparse(node.test):
            body = ast.unparse(node)
            assert "budget_reservation = []" in ast.unparse(node.body[0])
            assert "reserve_budget" in body  # else 분기에 기존 예약 경로
            found = True
    assert found
