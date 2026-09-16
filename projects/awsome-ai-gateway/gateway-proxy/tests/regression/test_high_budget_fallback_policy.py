# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""예산 판정이 **Redis 경로와 DB 폴백에서 같아야** 한다, 그리고 앱별 차감이 새지 않아야 한다.

두 결함
-------
1. **DB 폴백이 정책을 무시했다.** Redis 경로(``redis_scripts/budget_check.lua``)는
   ``policy == 'hard_block'`` 일 때만 100% 에서 막는다. SOFT_WARNING 은
   soft_limit_pct(기본 110%) 까지 허용하고, THROTTLE 은 **절대 막지 않고** RPM 만 줄인다.
   DB 폴백은 정책을 보기 **전에** ``used >= limit`` 에서 무조건 raise 했다.

   그래서 SOFT_WARNING 팀이 $1000.50/$1000 인 상태에서 Redis 가 한 번 타임아웃되면, Redis
   경로였다면 통과했을 요청이 429 가 됐다 — Redis 가 degrade 된 동안 팀 전원이 막힌다.
   부수적으로 ``team_soft_limit_exceeded`` 와 ``soft_warning=True`` 는 폴백에서 도달
   불가능한 죽은 코드였고, 그래서 ``X-Budget-Warning`` 헤더가 Redis 다운 중 나오지 않았다.

2. **앱별 차감이 에코된 게이트 뒤에 있었다.** user-layer EVAL 이 돌려준
   ``app_clients`` 목록에 client 가 있어야만 앱별 카운터를 올렸다. 그런데
   ``budget:config:user:{uid}`` 는 ex=300 이고 없을 때만 재생성되므로, 60초짜리 스트리밍
   응답이 잔여 20초에 시작해 만료 뒤 finalize 하면 Lua 는 ``app_clients = {}`` 로 시작하고
   cjson 이 빈 테이블을 JSON **객체**로 인코딩해 게이트가 닫힌다. 반면 **검사** 경로는 DB
   에서 설정을 재수화해 앱 한도를 계속 집행한다 — 앱 카운터에는 TTL 이 없고 복원은 키가
   없을 때만 도므로, 그 달 내내 과소청구된 채 남는다.

⚠️ 정책 일치는 **실 Redis** 로 확인한다. 두 경로가 같은 판정을 내리는지가 검증 대상이므로,
   한쪽을 가짜로 두면 비교 자체가 성립하지 않는다.

실행:
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
      pytest tests/regression/test_high_budget_fallback_policy.py
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.domain import BudgetPolicy
from app.services.budget_service import (
    DEFAULT_SOFT_LIMIT_PCT,
    PER_APP_BUDGET_CLIENTS,
    _evaluate_layer,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")


# ─────────────────────────────────────────────────────────────────────────────
# 1. 판정 함수 자체
# ─────────────────────────────────────────────────────────────────────────────


def test_hard_block_blocks_at_the_limit():
    reason, warn, throttle = _evaluate_layer(
        Decimal("100"), Decimal("100"), BudgetPolicy.HARD_BLOCK
    )
    assert reason == "budget_exceeded"
    assert not warn and not throttle


def test_hard_block_allows_below_the_limit():
    reason, _w, _t = _evaluate_layer(Decimal("99.99"), Decimal("100"), BudgetPolicy.HARD_BLOCK)
    assert reason is None


def test_soft_warning_allows_past_the_limit_up_to_the_soft_ceiling():
    """⚠️ 이것이 폴백에서 429 로 나가던 상태다.

    SOFT_WARNING 의 의도는 "한도를 넘겨도 soft_limit_pct 까지는 허용하고 경고만" 이다.
    """
    reason, warn, _t = _evaluate_layer(
        Decimal("100.50"), Decimal("100"), BudgetPolicy.SOFT_WARNING
    )
    assert reason is None, "SOFT_WARNING 이 한도 초과에서 차단됐다 — Redis 경로와 어긋난다"
    assert warn is True, "경고 플래그가 서지 않았다 — X-Budget-Warning 헤더가 나오지 않는다"


def test_soft_warning_blocks_at_the_soft_ceiling():
    ceiling = Decimal("100") * Decimal(DEFAULT_SOFT_LIMIT_PCT) / Decimal(100)
    reason, warn, _t = _evaluate_layer(ceiling, Decimal("100"), BudgetPolicy.SOFT_WARNING)
    assert reason == "soft_limit_exceeded"
    assert warn is False, "차단하면서 경고 플래그도 세우면 두 신호가 모순된다"


def test_throttle_never_blocks():
    """⚠️ THROTTLE 은 설계상 **차단하지 않는** 정책이다. 폴백에서만 100% 에서 막혔다."""
    for used in ("50", "100", "1000"):
        reason, _w, throttle = _evaluate_layer(
            Decimal(used), Decimal("100"), BudgetPolicy.THROTTLE
        )
        assert reason is None, f"THROTTLE 이 used={used} 에서 차단했다"
    _r, _w, throttle = _evaluate_layer(Decimal("100"), Decimal("100"), BudgetPolicy.THROTTLE)
    assert throttle is True, "100% 인데 스로틀이 켜지지 않았다"


def test_throttle_is_off_below_the_first_threshold():
    _r, _w, throttle = _evaluate_layer(Decimal("10"), Decimal("100"), BudgetPolicy.THROTTLE)
    assert throttle is False


def test_zero_limit_does_not_divide_by_zero():
    """한도 0 은 실재한다(자동 생성 팀의 초기 상태). ZeroDivisionError 로 500 이 되면 안 된다."""
    for policy in BudgetPolicy:
        _evaluate_layer(Decimal("5"), Decimal("0"), policy)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 Redis — Lua 가 같은 판정을 내리는가
# ─────────────────────────────────────────────────────────────────────────────

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 두 경로가 **같은** 판정을 내리는지가 검증 대상이라, "
        "한쪽을 가짜로 두면 비교가 성립하지 않는다."
    ),
)


async def _lua_decision(redis, *, used: str, limit: str, policy: str) -> dict:
    """``budget_check.lua`` 를 실제로 돌려 판정을 받는다.

    ⚠️ 스크립트는 앱 부팅 시 ``load_all`` 로 읽힌다(main.py). 테스트에서는 그 부팅을
       거치지 않으므로 여기서 직접 읽어 준다 — 안 하면 ``KeyError: not loaded`` 가 나고,
       그 실패는 "두 경로가 어긋난다" 와 구별되지 않는다.
    """
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader.load_all(_SRC / "redis_scripts")

    uid = str(uuid.uuid4())
    usage_key = f"budget:user:{{{uid}}}:2026-06"
    config_key = f"budget:config:user:{{{uid}}}"
    await redis.set(usage_key, used)
    await redis.set(
        config_key,
        json.dumps(
            {
                # ⚠️ Lua 는 `config.limit_usd` 를 읽는다(DB 컬럼명 max_budget_usd 가 아니다).
                #    틀리면 limit=0 으로 파싱돼 모든 케이스가 차단으로 나오고, 그 실패가
                #    "두 경로가 어긋난다" 처럼 보인다.
                "limit_usd": limit,
                "policy": policy,
                "soft_limit_pct": DEFAULT_SOFT_LIMIT_PCT,
                "throttle_rpm_pct": 50,
                "thresholds": [80, 90, 100],
            }
        ),
    )
    try:
        raw = await redis.eval(
            LuaScriptLoader.get("budget_check"), 2, usage_key, config_key, "user"
        )
        decoded = json.loads(raw)
        # 대조군 — 설정이 파싱되지 않으면 Lua 는 "미설정" 으로 전부 통과시키거나(config
        # 부재) limit=0 으로 전부 차단한다. 둘 다 비교를 무의미하게 만든다.
        assert decoded.get("config_present") is True, (
            f"Lua 가 설정을 읽지 못했다: {decoded} — 키 이름을 확인할 것"
        )
        assert float(decoded["limit_usd"]) == float(limit), (
            f"Lua 가 본 limit={decoded['limit_usd']} vs 기대 {limit}"
        )
        return decoded
    finally:
        await redis.delete(usage_key, config_key)


@_redis_required
@pytest.mark.parametrize(
    ("policy", "used", "limit"),
    [
        ("hard_block", "100", "100"),      # 차단
        ("hard_block", "99", "100"),       # 통과
        ("soft_warning", "100.5", "100"),  # 통과 + 경고  ← 폴백이 429 로 만들던 상태
        ("soft_warning", "110", "100"),    # 차단
        ("throttle", "150", "100"),        # 통과(절대 차단 안 함)
        ("throttle", "10", "100"),         # 통과
    ],
)
async def test_lua_and_db_fallback_agree(policy: str, used: str, limit: str):
    """⚠️ 이 파일의 핵심. 같은 상태에서 두 경로의 **차단 여부**가 같아야 한다.

    Redis 가 살아 있는지에 따라 사용자가 429 를 맞느냐가 갈리면, 장애 중에만 나타나고
    재현이 어려운 종류의 오답이 된다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    try:
        lua = await _lua_decision(r, used=used, limit=limit, policy=policy)
    finally:
        await r.aclose()

    reason, warn, _throttle = _evaluate_layer(
        Decimal(used), Decimal(limit), BudgetPolicy(policy)
    )
    db_allowed = reason is None
    assert db_allowed == bool(lua["allowed"]), (
        f"policy={policy} used={used}/{limit}: "
        f"Lua allowed={lua['allowed']} vs DB 폴백 allowed={db_allowed} "
        f"(Lua reason={lua.get('reason')!r}, DB reason={reason!r})"
    )
    if db_allowed:
        assert warn == bool(lua.get("soft_warning")), (
            f"soft_warning 플래그가 어긋난다: Lua={lua.get('soft_warning')} vs DB={warn}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 앱별 차감이 에코된 게이트에 묶여 있지 않은가
# ─────────────────────────────────────────────────────────────────────────────


def _finalize_source() -> str:
    return (_SRC / "services" / "cost_recorder.py").read_text(encoding="utf-8")


def test_per_app_deduct_is_not_gated_on_the_echoed_app_clients():
    """⚠️ 그 게이트가 조용한 과소청구의 원인이었다.

    ``result["app_clients"]`` 는 user-layer EVAL 이 에코한 값이고, 설정 키가 만료되면 빈
    객체가 되어 게이트가 닫힌다. 검사 경로는 DB 에서 재수화해 계속 집행하므로 카운터만
    어긋나고, 앱 카운터에는 TTL 이 없어 그 달 내내 남는다.
    """
    src = _finalize_source()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "finalize"
    )
    # 앱별 차감 키를 만드는 대입문을 찾고, 그것을 감싼 If 들의 조건을 본다.
    target_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "client_usage_key":
                    target_line = node.lineno
    assert target_line is not None, "client_usage_key 대입을 찾지 못했다 — 전제가 깨졌다"

    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not (node.lineno < target_line <= (node.end_lineno or 0)):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        assert "app_clients" not in names, (
            f"L{node.lineno}: 앱별 차감이 app_clients 게이트 안에 있다 — 설정 키가 만료되면 "
            "그 요청의 앱 카운터가 올라가지 않는다"
        )
        attrs = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        assert "get" not in attrs or "app_clients" not in src[node.col_offset :][:200], (
            f"L{node.lineno}: 에코된 값에 의존하는 조건이 남아 있다"
        )


def test_per_app_deduct_uses_the_shared_client_tuple():
    """차감 대상 앱 목록이 검사 경로와 같은 상수여야 한다.

    문자열 튜플을 따로 적어 두면 새 앱을 추가할 때 한쪽만 고쳐져, 검사는 하고 차감은
    안 하는(또는 반대의) 상태가 된다.
    """
    src = _finalize_source()
    assert "PER_APP_BUDGET_CLIENTS" in src, "공용 상수를 쓰지 않는다"
    assert '("claude-code", "cowork", "codex")' not in src, (
        "앱 목록을 인라인 리터럴로 다시 적었다 — 검사 경로와 갈라진다"
    )
    assert set(PER_APP_BUDGET_CLIENTS) == {"claude-code", "cowork", "codex"}


def test_the_db_fallback_uses_the_shared_decision_for_every_layer():
    """세 계층(USER / TEAM / per-app)이 모두 같은 판정 함수를 써야 한다.

    한 계층만 무조건 차단으로 남으면 그 계층에서만 정책이 무시된다 — 원래 결함이 정확히
    그 모양이었다(세 곳 모두 무조건이었고, 팀 계층의 정책 분기는 죽은 코드였다).
    """
    src = (_SRC / "services" / "budget_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_check_budget_db"
        ),
        None,
    )
    assert fn is not None, "_check_budget_db 를 찾지 못했다"
    calls = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_evaluate_layer"
    ]
    assert len(calls) == 3, (
        f"판정 함수 호출이 {len(calls)}곳이다(L{calls}) — USER/TEAM/per-app 세 계층 모두여야 한다"
    )

    # ⚠️ 불변식을 직접 표현한다: **한도 초과로 인한** 모든 차단은 _evaluate_layer 의
    #    결과에서 나와야 한다.
    #
    #    이전 버전의 이 검사는 `If` 조건에 `max_budget_usd` **속성**이 나오는지를 봤는데,
    #    무조건 차단을 `max_budget = team_config.max_budget_usd` 로 지역변수에 담은 뒤
    #    `if team_used >= max_budget:` 로 비교하면 속성이 조건에 없어 통과했다(대조군으로
    #    확인). 그래서 조건의 모양이 아니라 **raise 의 근거**를 본다.
    #
    #    설정 부재로 인한 차단(예산이 아예 없음)은 정책과 무관하므로 허용목록으로 둔다.
    _CONFIG_ABSENCE_REASONS = {"team_budget_unset", "no_budget_assigned"}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        if not (isinstance(exc, ast.Call) and getattr(exc.func, "id", None) == "PermissionError"):
            continue
        assert exc.args, f"L{node.lineno}: PermissionError 에 인자가 없다"
        arg = exc.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            assert arg.value in _CONFIG_ABSENCE_REASONS, (
                f"L{node.lineno}: 리터럴 이유 {arg.value!r} 로 차단한다 — 한도 초과 차단은 "
                "_evaluate_layer 의 결과에서 나와야 정책이 반영된다(무조건 차단 부활)"
            )
            continue
        assert isinstance(arg, ast.JoinedStr), (
            f"L{node.lineno}: 차단 이유가 f-string 이 아니다 — "
            "_evaluate_layer 결과를 쓰지 않는 것으로 보인다"
        )
        names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
        assert any(n.endswith("_block") for n in names), (
            f"L{node.lineno}: 차단 이유가 _evaluate_layer 결과(*_block)에서 오지 않는다: {names}"
        )
