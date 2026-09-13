# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``budget:config:user:{uid}`` 의 ``app_clients`` 가 지워지지 않는지 — **실 Redis 로**.

무엇이 걸려 있나
----------------
그 키에는 총액 예산 필드(``limit_usd``/``policy``/``thresholds``)와 앱별 하위 한도
게이트(``app_clients``)가 함께 산다. 쓰는 주체가 다르고 시점도 다르다.
``app_clients`` 가 사라지면 게이트웨이는 **에러도 로그도 없이 앱별 예산 평가를 통째로
건너뛴다** — ``budget_check.lua`` 가 없는 필드를 빈 테이블로 읽고, 파이썬 쪽
``isinstance(..., list)`` 검사에서 떨어지기 때문이다. 즉 조용한 예산 우회다.

⚠️ 이 파일의 예전 버전은 이랬다::

    source = src.read_text()
    assert "app_clients" in source

문자열이 파일 어딘가에 있으면 통과한다. 그래서 그 가드는 아래 **전부**를 통과시켰다:

* ``_sync_redis_thresholds`` 의 GET-modify-SET 경합(실제로 있었다),
* ``CLIService._cache_for_gateway`` 의 로그인 클로버(``app_clients`` 없는 페이로드를
  ``redis.set`` — TTL 까지 버린다),
* 쓰기 경로에서 ``app_clients`` 를 지우고 **주석에만** 남겨 두는 변경.

Redis 를 한 번도 부르지 않는 가드로 Redis 불변식을 지킬 수 없다. 그래서 실 Redis 를
쓴다(``REDIS_PROOF_URL``). 가짜 Redis 는 쓰지 않는다 — 검증 대상이 **Lua 의 원자성**과
**SET 의 TTL 폐기 동작**이라, 그 둘을 흉내내는 double 은 무엇도 증명하지 못한다.

실행::

    docker run -d --name redis-proof -p 56379:6379 redis:7-alpine
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 pytest \\
        tests/regression/test_high_budget_redis_clobber.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_SVC = Path(__file__).resolve().parents[2] / "src" / "app" / "services"
_CACHE_PY = Path(__file__).resolve().parents[2] / "src" / "app" / "core" / "budget_cache.py"

TTL = 300


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정적 — 쓰기 경로가 raw redis.set 으로 되돌아가지 않았는지
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


def test_login_path_does_not_raw_set_the_user_config_key():
    """``_cache_for_gateway`` 가 ``redis.set`` 을 하면 app_clients + TTL 이 날아간다."""
    code = _executable_code(_SVC / "cli_service.py", "_cache_for_gateway")
    assert "write_user_budget_config" in code, (
        "로그인 경로가 budget_cache 헬퍼를 쓰지 않는다 — app_clients 를 지운다"
    )
    # budget:config 키에 대한 raw set 이 없어야 한다.
    assert "budget:config:user" not in code or "attr='set'" not in code, (
        "로그인 경로가 여전히 raw redis.set 으로 budget config 를 쓴다"
    )


def test_threshold_sync_uses_lua_for_the_user_scope():
    code = _executable_code(_SVC / "budget_service.py", "_sync_redis_thresholds")
    assert "write_user_budget_config" in code, (
        "USER 스코프가 Lua 병합을 쓰지 않는다 — GET-modify-SET 경합이 남았다"
    )
    # 애플리케이션 레벨 GET-merge 흔적이 없어야 한다.
    assert "attr='get'" not in code, (
        "여전히 redis.get 으로 읽어 병합한다 — await 가 이벤트 루프를 양보하므로 "
        "워커 하나 안에서도 두 요청이 교차한다"
    )


def test_app_clients_refresh_uses_lua():
    code = _executable_code(_SVC / "budget_service.py", "_refresh_user_app_clients")
    assert "refresh_user_app_clients" in code, "Lua 갱신을 쓰지 않는다"
    assert "attr='get'" not in code


def test_helper_never_falls_back_to_a_plain_set():
    """Lua 실패 시 ``redis.set`` 으로 폴백하면 클로버를 다시 들여온다."""
    src = _CACHE_PY.read_text(encoding="utf-8")
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


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 Redis — 불변식 자체
# ─────────────────────────────────────────────────────────────────────────────

live = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason="REDIS_PROOF_URL 미설정 — Lua 원자성과 SET 의 TTL 폐기는 실 Redis 에서만 증명된다",
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


async def _seed(client, uid, *, app_clients=None, limit="100"):
    payload = {"limit_usd": limit, "policy": "hard_block", "thresholds": [80, 90, 100]}
    if app_clients is not None:
        payload["app_clients"] = app_clients
    await client.set(_key(uid), json.dumps(payload), ex=TTL)


async def _read(client, uid) -> dict:
    raw = await client.get(_key(uid))
    return json.loads(raw) if raw else {}


@live
async def test_writing_the_total_preserves_app_clients(redis_client):
    from app.core.budget_cache import write_user_budget_config

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["claude-code", "codex"])

    await write_user_budget_config(
        redis_client, uid, {"limit_usd": "500", "policy": "soft_limit"}, TTL
    )

    got = await _read(redis_client, uid)
    assert got["app_clients"] == ["claude-code", "codex"], (
        f"app_clients 가 지워졌다: {got} — 앱별 예산 한도가 조용히 사라진다"
    )
    assert got["limit_usd"] == "500", "총액이 갱신되지 않았다"


@live
async def test_empty_app_clients_is_preserved_as_an_empty_collection(redis_client):
    """``[]`` 는 "활성 per-app 예산이 없다" 는 **의미 있는 값**이다 — 필드가 없는 것과 다르다.

    truthiness 로 판정하면 사라지고, 그러면 게이트웨이가 DB 에서 재도출하는 창이 열린다.

    ⚠️ 값이 ``[]`` 로 되돌아오지는 **않는다.** Redis 의 cjson 에는 빈 배열 개념이 없어
       ``[]`` 를 round-trip 하면 ``{}`` 가 된다(실측: redis 7.4, ``cjson.empty_array``
       가 nil). 그래서 여기서 요구하는 것은 "빈 컬렉션으로 남아 있다" 이고, 그 두 형상이
       읽는 쪽에서 같게 해석되는지는 아래 test_gateway_reads_both_empty_shapes... 가
       확인한다. 이 제약을 문서화하지 않으면 다음 사람이 ``{}`` 를 "알 수 없음" 으로
       오독한다.
    """
    from app.core.budget_cache import write_user_budget_config

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=[])
    await write_user_budget_config(redis_client, uid, {"limit_usd": "1"}, TTL)
    got = await _read(redis_client, uid)
    assert "app_clients" in got, f"빈 컬렉션이 통째로 사라졌다: {got}"
    assert got["app_clients"] in ([], {}), f"빈 컬렉션이 아니다: {got}"
    assert not got["app_clients"], f"비어 있지 않다: {got}"


def test_gateway_normalizes_both_empty_shapes():
    """cjson 의 ``{}`` 와 진짜 ``[]`` 를 게이트웨이가 같게 해석해야 한다.

    ⚠️ 이 동등성이 없으면 위 보존은 무의미하다. ``isinstance(list)`` 만 보던 예전 코드는
       ``{}`` 를 "타입 불일치" 로 떨어뜨려 per-app 분기 전체(콜드 캐시 재수화 안전망
       포함)를 건너뛰었다.

    ⚠️ 여기서는 **소스 수준**으로만 확인한다. 두 서비스가 모듈 경로를 공유하므로
       (``app.services.budget_service`` 가 양쪽에 있다) admin-api 프로세스에서
       gateway-proxy 쪽을 import 하면 이미 로드된 admin-api 모듈이 잡힌다. 함수의
       **행동** 검증은 그 함수가 사는 곳에 있다:
       gateway-proxy/tests/regression/test_high_app_clients_normalization.py
       (CI 도 서비스별 venv 로 돌리므로 그게 맞는 위치다).
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


@live
async def test_ttl_is_set_and_not_dropped(redis_client):
    """⚠️ 이게 로그인 클로버의 두 번째 절반이다.

    Redis ``SET`` 은 ``KEEPTTL`` 없이는 기존 TTL 을 **버린다**. TTL 이 사라진 키는
    영구히 남고, 게이트웨이는 키가 **없을 때만** 재수화하므로 자가치유가 없다.
    """
    from app.core.budget_cache import write_user_budget_config

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["codex"])
    await write_user_budget_config(redis_client, uid, {"limit_usd": "7"}, TTL)

    ttl = await redis_client.ttl(_key(uid))
    assert ttl > 0, f"TTL 이 없다(persistent): {ttl} — 키가 영구히 남아 자가치유되지 않는다"
    assert ttl <= TTL


@live
async def test_login_path_preserves_app_clients_end_to_end(redis_client):
    """실제 로그인 경로(``_cache_for_gateway``)를 태운다.

    헬퍼만 테스트하면 호출부가 여전히 raw set 을 쓰는 경우를 놓친다.
    """
    from unittest.mock import MagicMock

    from app.models.budget import BudgetPolicy
    from app.services.cli_service import CLIService

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["claude-code"])

    user = MagicMock()
    user.id = uid
    budget = MagicMock()
    budget.max_budget_usd = Decimal("42")
    budget.policy = BudgetPolicy.HARD_BLOCK

    await CLIService._cache_for_gateway(redis_client, user, budget)

    got = await _read(redis_client, uid)
    assert got.get("app_clients") == ["claude-code"], (
        f"로그인이 app_clients 를 지웠다: {got} — CLI 로그인 한 번으로 앱별 예산이 꺼진다"
    )
    assert got["limit_usd"] == "42"
    assert await redis_client.ttl(_key(uid)) > 0, "로그인이 TTL 을 버렸다"


@live
async def test_refresh_only_touches_app_clients(redis_client):
    from app.core.budget_cache import refresh_user_app_clients

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["codex"], limit="900")
    ok = await refresh_user_app_clients(redis_client, uid, ["claude-code", "cowork"], TTL)
    assert ok is True
    got = await _read(redis_client, uid)
    assert got["app_clients"] == ["claude-code", "cowork"]
    assert got["limit_usd"] == "900", "총액 필드가 훼손됐다"
    assert got["policy"] == "hard_block"


@live
async def test_refresh_does_nothing_when_the_key_is_absent(redis_client):
    """총액 필드를 모르는 채로 키를 만들면 게이트웨이가 한도 없는 설정으로 읽는다."""
    from app.core.budget_cache import refresh_user_app_clients

    uid = uuid.uuid4()
    ok = await refresh_user_app_clients(redis_client, uid, ["codex"], TTL)
    assert ok is False
    assert await redis_client.get(_key(uid)) is None, "없는 키를 만들었다"


@live
async def test_concurrent_total_write_and_app_refresh_lose_nothing(redis_client):
    """**핵심**: 두 쓰기가 동시에 나가도 어느 필드도 사라지지 않는다.

    애플리케이션 레벨 GET-modify-SET 이었다면 나중에 SET 하는 쪽이 상대의 필드를 지운다.
    """
    from app.core.budget_cache import refresh_user_app_clients, write_user_budget_config

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["codex"], limit="1")

    await asyncio.gather(
        write_user_budget_config(redis_client, uid, {"limit_usd": "777", "policy": "x"}, TTL),
        refresh_user_app_clients(redis_client, uid, ["claude-code", "cowork"], TTL),
    )

    got = await _read(redis_client, uid)
    # 두 쓰기의 순서는 정해지지 않았지만, **어느 필드도 없어서는 안 된다**.
    assert "app_clients" in got, f"동시 쓰기로 app_clients 가 사라졌다: {got}"
    assert "limit_usd" in got, f"동시 쓰기로 limit_usd 가 사라졌다: {got}"
    assert got["app_clients"] in (["codex"], ["claude-code", "cowork"]), got
    assert got["limit_usd"] in ("1", "777"), got


@live
async def test_repeated_interleaving_never_loses_a_field(redis_client):
    """단발 경합은 운으로 통과할 수 있다 — 여러 번 교차시킨다."""
    from app.core.budget_cache import refresh_user_app_clients, write_user_budget_config

    uid = uuid.uuid4()
    await _seed(redis_client, uid, app_clients=["codex"], limit="1")

    for i in range(25):
        await asyncio.gather(
            write_user_budget_config(redis_client, uid, {"limit_usd": str(i), "policy": "p"}, TTL),
            refresh_user_app_clients(redis_client, uid, [f"c{i}"], TTL),
        )
        got = await _read(redis_client, uid)
        assert "app_clients" in got, f"{i}번째 교차에서 app_clients 손실: {got}"
        assert "limit_usd" in got, f"{i}번째 교차에서 limit_usd 손실: {got}"
