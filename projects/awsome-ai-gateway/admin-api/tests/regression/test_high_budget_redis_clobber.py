# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``budget:config:user:{uid}`` 는 **DEL-only** 다 — admin-api 는 절대 SET 하지 않는다.

무엇이 걸려 있나
----------------
그 키에는 총액 예산 필드(``limit_usd``/``policy``/``thresholds``)와 앱별 하위 한도
게이트(``app_clients``)가 함께 산다. ``app_clients`` 가 사라지면 게이트웨이는
**에러도 로그도 없이 앱별 예산 평가를 통째로 건너뛴다** — ``budget_check.lua`` 가
없는 필드를 빈 테이블로 읽고, 파이썬 쪽 ``_as_client_list`` 정규화가 빈 리스트로
돌려서 per-app 분기를 건너뛰기 때문이다. 즉 조용한 예산 우회다.

역사 — Lua 병합은 필요했지만 충분하지 않았다
-------------------------------------------
한때 ``write_user_budget_config``/``refresh_user_app_clients`` 두 Lua 스크립트로
"SET 하되 다른 필드를 보존" 하는 방식을 썼다. 그러나 H3 리뷰에서 구조적 한계가
드러났다:

* 커밋-후-지연 쓰기는 ``asyncio.create_task`` 로 떨어지고, 유저 단위
  ``pg_advisory`` 락은 **트랜잭션만** 직렬화한다 — 두 요청의 지연 태스크 실행
  순서는 보장되지 않는다. 마지막에 도착한 stale 스냅샷이 TTL(300s) 동안 이긴다.
* ``refresh_user_app_clients`` 는 키 부재 시 조용히 no-op 이라, 동시 DEL 이
  먼저 도착하면 최신 ``app_clients`` 가 통째로 유실된다.
* ``internal.py`` 의 raw ``redis.set`` 은 ``ex=`` 없이 app_clients 없는
  페이로드를 썼고(하드코딩 thresholds 포함) — present-and-persistent 상태가
  된 키는 자가치유도 없었다.

해법 — DEL 만 쓴다
-------------------
게이트웨이는 부모 키 미스 시 ``ensure_config_cached`` 로 DB 에서 완전한 설정
(총액 + ``alert_thresholds`` 컬럼 + 활성 per-app 행의 ``app_clients``)을 원자
재도출하고, per-app 키 미스 시 ``_hydrate_client_config_cache`` 로 재도출한다.
따라서 admin-api 가 커밋 후 하는 안전한 쓰기는 DEL 뿐이다 — DEL 은 교환
가능(commutative)하고 어느 순서에 도착해도 최종 상태가 "미스 → 신선한 재도출"
으로 수렴한다. SET 은 이 성질이 없다.

⚠️ 이 파일의 예전 버전은 두 Lua 헬퍼의 필드 보존을 검증했다 — 헬퍼가 제거된
   지금 그 테스트는 아무것도 지키지 않으므로, 계약 자체(DEL-only + DB 원천
   thresholds)를 정적 가드로 못 박는다.

실 Redis 라이브 테스트(``REDIS_PROOF_URL``)는 로그인 경로가 실제로 DEL 만
수행하는지 끝까지 확인한다::

    docker run -d --name redis-proof -p 56379:6379 redis:7-alpine
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 pytest \\
        tests/regression/test_high_budget_redis_clobber.py
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_SVC = Path(__file__).resolve().parents[2] / "src" / "app" / "services"
_ROUTERS = Path(__file__).resolve().parents[2] / "src" / "app" / "routers"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정적 — DEL-only 계약
# ─────────────────────────────────────────────────────────────────────────────


def _executable_code(path: Path, func_name: str) -> str:
    """함수 본문에서 docstring 을 뺀 실행부만. 주석은 AST 에 남지 않는다."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    body = body[1:]
            return ast.dump(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{path.name} 에서 {func_name} 을 찾지 못했다")


def test_set_user_budget_is_del_only_for_the_user_config_key():
    """``set_user_budget`` 은 DEL 만 한다 — 지연 SET 이 다른 커밋과 얽히면
    stale 스냅샷이 TTL 동안 이긴다(H3)."""
    code = _executable_code(_SVC / "budget_service.py", "set_user_budget")
    assert "invalidate_after_commit" in code, "커밋 후 DEL 이 없다"
    assert "_sync_redis_thresholds" not in code, (
        "user-scope SET 이 부활했다 — DEL-only 계약 위반"
    )
    assert "attr='set'" not in code


def test_app_budget_paths_are_del_only():
    """per-app 경로도 DEL-only — refresh/SET 이 부활하면 app_clients 유실이
    돌아온다."""
    for func in ("set_user_client_budget", "clear_user_client_budget"):
        code = _executable_code(_SVC / "budget_service.py", func)
        assert "invalidate_after_commit" in code, f"{func}: 커밋 후 DEL 없음"
        for banned in (
            "_sync_redis_app_config",
            "_write_user_app_clients",
            "_delete_redis_app_config",
            "refresh_user_app_clients",
        ):
            assert banned not in code, f"{func}: {banned} 부활 — DEL-only 위반"
        assert "attr='set'" not in code, f"{func}: raw redis.set 부활"


def test_threshold_sync_is_team_only():
    """``_sync_redis_thresholds`` 의 user 분기는 제거됐어야 한다 —
    ``budget:config:user:{uid}`` SET 의 마지막 통로다."""
    code = _executable_code(_SVC / "budget_service.py", "_sync_redis_thresholds")
    assert "write_user_budget_config" not in code
    # user 분기는 `scope_type == "user"` 비교의 str 상수로 드러난다.
    assert "Constant(value='user')" not in code, (
        "user-scope 분기가 부활했다 — budget:config:user:* SET 은 금지"
    )


def test_login_path_does_not_write_the_user_config_key():
    """``_cache_for_gateway`` 는 DEL 만 한다 — SET 의 모든 형태(raw set,
    병합 헬퍼)를 금지한다."""
    code = _executable_code(_SVC / "cli_service.py", "_cache_for_gateway")
    assert "attr='delete'" in code, "로그인 경로가 키를 DEL 하지 않는다"
    assert "write_user_budget_config" not in code, (
        "로그인 경로가 SET 헬퍼를 쓴다 — DEL-only 위반"
    )
    assert "attr='set'" not in code, "로그인 경로가 raw redis.set 을 쓴다"


def test_internal_issue_key_does_not_write_the_user_config_key():
    """``/internal/test/issue-key`` 도 DEL-only — 예전엔 ex= 없는 raw set
    으로 TTL 까지 버렸다."""
    code = _executable_code(_ROUTERS / "internal.py", "test_issue_key")
    assert "attr='delete'" in code
    assert "attr='set'" not in code, "internal 경로가 raw redis.set 을 쓴다"


def test_thresholds_are_read_from_the_db_column():
    """thresholds 의 원천은 ``alert_thresholds`` DB 컬럼(migration 0041)이다 —
    Redis 캐시는 부재·지연될 수 있어 응답/보존 규칙의 원천이 될 수 없다."""
    code = _executable_code(_SVC / "budget_service.py", "_resolve_policy_thresholds")
    assert "read_budget_config" not in code, (
        "thresholds 를 Redis 캐시에서 읽는다 — 캐시 미스 시 기본값으로 되돌아간다"
    )
    assert "alert_thresholds" in code, "DB 컬럼을 읽지 않는다"


def test_helper_never_falls_back_to_a_plain_set():
    """budget_cache 모듈의 except 절이 redis.set 으로 폴백하면 클로버를
    다시 들여온다."""
    src = (_SVC.parent / "core" / "budget_cache.py").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for handler in [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]:
            dumped = ast.dump(ast.Module(body=handler.body, type_ignores=[]))
            assert "attr='set'" not in dumped, (
                f"{node.name} 의 except 절이 redis.set 으로 폴백한다 — 클로버 재도입"
            )


def test_admin_api_never_sets_the_user_config_key():
    """전수 스캔 — admin-api 의 **어떤** 경로도 ``budget:config:user:{...}``
    키에 ``redis.set`` 하지 않는다.

    팀 키(``budget:config:team:``)는 공유 필드가 없는 완전 페이로드라 SET 이
    안전해 대상이 아니다.
    """
    import ast
    import re

    violations = []
    for py in (_SVC.parent).rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # redis.set(...) 호출인가
            if not (isinstance(func, ast.Attribute) and func.attr == "set"):
                continue
            if not node.args:
                continue
            src_key = ast.dump(node.args[0])
            if "budget:config:user" in src_key:
                violations.append(f"{py.name}:.{node.lineno}")
    assert not violations, (
        f"budget:config:user:* 키에 redis.set 하는 경로: {violations} — "
        "DEL-only 계약 위반(게이트웨이가 미스 시 DB 에서 재도출한다)"
    )


def test_guard_is_not_the_old_substring_check():
    """이 파일 자신이 예전의 공허한 형태로 되돌아가지 않았는지.

    ``assert "app_clients" in source`` 는 Redis 를 부르지 않으므로 어떤 클로버도 잡지
    못한다. 그 형태가 다시 들어오는 것을 막는다.
    """
    me = Path(__file__).read_text(encoding="utf-8")
    import ast

    tree = ast.parse(me)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        dumped = ast.dump(node)
        # `"app_clients" in <something named source/src/text>` 형태를 금지한다.
        if "'app_clients'" in dumped and "Compare" in dumped and "In()" in dumped:
            assert "read_text" not in dumped, (
                "파일 전문에 대한 substring 단정이 다시 들어왔다 — 이 가드의 원래 실패 형태다"
            )


def test_gateway_normalizes_both_empty_shapes():
    """cjson 의 ``{}`` 와 진짜 ``[]`` 를 게이트웨이가 같게 해석해야 한다.

    Redis 의 cjson 에는 빈 배열 개념이 없어 ``[]`` 를 round-trip 하면 ``{}``
    가 된다. ``isinstance(list)`` 만 보던 예전 코드는 ``{}`` 를 "타입 불일치"
    로 떨어뜨려 per-app 분기 전체(콜드 캐시 재수화 안전망 포함)를 건너뛰었다.

    ⚠️ 여기서는 **소스 수준**으로만 확인한다. 두 서비스가 모듈 경로를 공유하므로
       (``app.services.budget_service`` 가 양쪽에 있다) admin-api 프로세스에서
       gateway-proxy 쪽을 import 하면 이미 로드된 admin-api 모듈이 잡힌다. 함수의
       **행동** 검증은 그 함수가 사는 곳에 있다:
       gateway-proxy/tests/regression/test_high_app_clients_normalization.py
    """
    gw = Path(__file__).resolve().parents[3] / "gateway-proxy" / "src" / "app" / "services"
    src_file = gw / "budget_service.py"
    assert src_file.is_file(), f"gateway-proxy budget_service 를 찾지 못했다: {src_file}"
    src = src_file.read_text(encoding="utf-8")

    assert "def _as_client_list(" in src, (
        "게이트웨이에 app_clients 정규화 함수가 없다 — cjson 의 빈 dict 가 "
        "'타입 불일치' 로 떨어져 per-app 예산 평가가 건너뛰어진다"
    )
    # 정규화를 만들어 두고 게이트에서 쓰지 않으면 아무 효과가 없다.
    assert "_as_client_list(user_result.get(" in src, (
        "정규화 함수가 per-app 게이트에서 호출되지 않는다"
    )
    # 그리고 옛 형태로 되돌아가지 않았는지.
    assert "isinstance(user_app_clients, list)" not in src, (
        "여전히 isinstance(list) 로 게이팅한다 — cjson 의 {} 를 거부한다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 Redis — 로그인 경로가 DEL 만 수행하는지
# ─────────────────────────────────────────────────────────────────────────────

live = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason="REDIS_PROOF_URL 미설정 — DEL 동작은 실 Redis 에서만 증명된다",
)


@pytest.fixture
async def redis_client():
    redis_asyncio = pytest.importorskip("redis.asyncio", reason="redis 패키지 필요")
    client = redis_asyncio.from_url(REDIS_PROOF_URL, decode_responses=True)
    # 대조군 — 정말 붙었는가. 못 붙었으면 skip 이 아니라 실패다(설정했으니 기대가 있다).
    assert await client.ping() is True, f"REDIS_PROOF_URL 에 붙지 못했다: {REDIS_PROOF_URL}"
    yield client
    await client.aclose()


def _key(uid) -> str:
    return f"budget:config:user:{{{uid}}}"


@live
async def test_login_path_deletes_and_never_writes_the_key(redis_client):
    """``_cache_for_gateway`` 가 기존 키를 지우고 **아무것도 다시 쓰지 않는다**.

    씨앗 키(app_clients 포함)가 DEL 되면, 게이트웨이는 다음 요청에서 DB 에서
    완전한 설정(총액 + thresholds + app_clients)을 재도출한다. SET 하는 경로는
    app_clients 를 잃거나 stale 스냅샷을 남기므로 이 단정이 핵심이다.
    """
    from unittest.mock import MagicMock

    from app.models.budget import BudgetPolicy
    from app.services.cli_service import CLIService

    uid = uuid.uuid4()
    await redis_client.set(
        _key(uid),
        json.dumps({
            "limit_usd": "100",
            "policy": "hard_block",
            "thresholds": [80, 90, 100],
            "app_clients": ["claude-code"],
        }),
        ex=300,
    )

    user = MagicMock()
    user.id = uid
    budget = MagicMock()
    budget.max_budget_usd = Decimal("42")
    budget.policy = BudgetPolicy.HARD_BLOCK

    await CLIService._cache_for_gateway(redis_client, user, budget)

    assert await redis_client.get(_key(uid)) is None, (
        "로그인 경로가 키를 남겼다 — SET 계열 쓰기가 부활했다(DEL-only 위반)"
    )
