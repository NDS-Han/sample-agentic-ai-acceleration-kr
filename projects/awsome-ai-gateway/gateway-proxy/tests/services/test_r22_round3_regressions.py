# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R22 라운드 3 회귀 — 프로브 토큰 소비, 앱 카운터 단독 유실 복원, 인덱스 웜업 지속.

세 결함 모두 라운드 2 수정의 부수효과/누락이었다 — 다시 생기면 같은 방향으로
조용히 열리므로 구조·행동으로 못 박는다.
"""
from __future__ import annotations

import ast
import json as _json
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# S1 — 멀티턴 웹서치: 첫 성공 이후 토큰은 소비돼야 한다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_turn_success_does_not_close_reopened_circuit(fake_redis):
    """턴1 프로브 성공이 회로를 닫은 뒤 다른 파드가 재오픈하면, 턴2의 성공은
    그 ``open`` 을 지우면 안 된다 — 라우터는 토큰을 첫 성공과 함께 소비한다."""
    from app.services.circuit_breaker import CircuitBreakerService

    cb = CircuitBreakerService(halfopen_ttl_ms=8000)
    pmid = "m"
    await fake_redis.set(f"cb:{{{pmid}}}:open", "1")
    assert await cb.try_acquire_halfopen_probe(fake_redis, pmid, token="tok-1") is True

    # 턴1 성공 — 소유자 토큰으로 회로를 닫는다.
    await cb.record_success(fake_redis, pmid, probe_token="tok-1")
    assert await fake_redis.exists(f"cb:{{{pmid}}}:open") == 0

    # 사이에 다른 파드가 실패로 회로를 재오픈하고 새 프로브를 땄다.
    await fake_redis.set(f"cb:{{{pmid}}}:open", "1")
    await fake_redis.set(f"cb:{{{pmid}}}:halfopen", "tok-other-pod")

    # 턴2 성공 — 라우터는 토큰을 소비했으므로 None 으로 온다. 재오픈된 회로 유지.
    await cb.record_success(fake_redis, pmid, probe_token=None)
    assert await fake_redis.exists(f"cb:{{{pmid}}}:open") == 1
    assert await fake_redis.get(f"cb:{{{pmid}}}:halfopen") == "tok-other-pod"


@pytest.mark.parametrize("router", ["messages.py", "openai_compat.py"])
def test_ws_probe_token_is_consumed_in_router(router):
    """``_ws_cb_record`` 안에서 ``_ws_probe_token`` 을 None 으로 소비하는지 AST로 확인.

    소비하지 않으면 두 번째 턴의 성공이 (라운드 2 N1 의 무조건 close 로)
    재오픈된 회로를 지운다 — 결함은 라우터 배선에 있으므로 서비스 테스트만으로는
    잡히지 않는다.
    """
    src = (_SRC / "routers" / router).read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_ws_cb_record"
    )
    consumed = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            names = {
                e.id
                for e in ast.walk(target)
                if isinstance(e, ast.Name)
            }
            if "_ws_probe_token" not in names:
                continue
            for v in ast.walk(node.value):
                if isinstance(v, ast.Constant) and v.value is None:
                    consumed = True
    assert consumed, (
        f"{router}::_ws_cb_record 가 _ws_probe_token 을 None 으로 소비하지 않는다 — "
        "두 번째 턴의 성공이 재오픈된 회로를 지운다(R22 r3 S1)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# S2 — 앱 카운터 단독 유실도 복원 경로를 태운다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lost_app_counter_triggers_rebuild_when_user_key_intact(mock_redis):
    """유저 총합 키는 멀쩡한데 앱 키만 축출된 경우 — 예전에는 복원 루프가
    유저 트리거 블록 안에 중첩돼 있어 영구 fail-open 이었다."""
    from app.services.budget_service import BudgetService
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "--check"
    LuaScriptLoader._scripts["budget_rebuild"] = "--rebuild"

    uid, tid, period, client = "u1", "t1", "2026-10", "claude-code"
    user_key = f"budget:user:{{{uid}}}:{period}"
    app_key = f"budget:user:{{{uid}}}:{client}:{period}"
    team_key = f"budget:team:{{{tid}}}:{period}"

    rebuild_calls: list[list] = []

    async def _get(k):
        if k == "budget:pendingidx:since":
            return b"1"            # 웜업 종료(오래된 타임스탬프)
        return None

    async def _mget(keys):
        # r4 N3: 카운터+:lost 는 MGET 으로 묶어 읽는다.
        return [
            {
                user_key: b"5.0",           # 유저 카운터는 살아 있다
                team_key: b"1.0",           # 팀 카운터도 살아 있다
                f"{app_key}:lost": b"1",    # 앱 키만 센티넬
            }.get(k)
            for k in keys
        ]

    async def _exists(k):
        return 1 if "config" in k else 0

    _OK = (
        b'{"allowed":true,"scope":"user","used_usd":5.0,'
        b'"remaining_usd":5.0,"limit_usd":10.0,"policy":"hard_block",'
        b'"config_present":true}'
    )

    async def _eval(script, numkeys, *args, **_kw):
        if script == "--rebuild":
            rebuild_calls.append(list(args))
            return 1
        return _OK

    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.mget = AsyncMock(side_effect=_mget)
    mock_redis.exists = AsyncMock(side_effect=_exists)
    mock_redis.eval = AsyncMock(side_effect=_eval)
    mock_redis.set = AsyncMock(return_value=True)   # inflight 플래그 획득
    mock_redis.hkeys = AsyncMock(return_value=[])

    db = AsyncMock()
    result = MagicMock()
    result.all = MagicMock(return_value=[(None, Decimal("5.0"))])
    db.execute = AsyncMock(return_value=result)

    svc = BudgetService()
    await svc.check_budget(mock_redis, db, uid, tid, period, client=client)

    assert rebuild_calls, (
        "앱 카운터 :lost 센티넬이 있는데 rebuild eval 이 한 번도 안 돌았다 — "
        "유저 키가 멀쩡하면 앱 유실을 영구히 못 본다(R22 r3 S2)"
    )
    assert any(call[0] == app_key for call in rebuild_calls), (
        f"rebuild 대상에 {app_key} 가 없다: {rebuild_calls}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# S3 — 인덱스 웜업은 한 번 끝나면 재시작하지 않는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_warmup_stays_finished_after_ttl(fake_redis, monkeypatch):
    """예전에는 ``since`` 키가 ``ttl*2`` 로 만료돼 만료 때마다 웜업이 재시작됐다 —
    시간의 절반 가까이 rebuild 가 SCAN 폴백으로 돌아갔다(R22 r3 S3)."""
    from app.config import get_settings
    from app.services.budget_service import BudgetService

    ttl = get_settings().budget_reservation_ttl_seconds

    # 첫 관측 — 웜업 시작(True)이고 since 를 세운다.
    assert await BudgetService._index_in_warmup(fake_redis) is True
    # 웜업 창 안 — 여전히 True.
    assert await BudgetService._index_in_warmup(fake_redis) is True

    # since 를 과거로 되돌려 웜업 창을 지나게 한다.
    await fake_redis.set(
        "budget:pendingidx:since", str(int(time.time() - ttl - 10))
    )
    assert await BudgetService._index_in_warmup(fake_redis) is False
    # 핵심 — 키가 만료 없이 남아 있으므로 이후 호출도 계속 False 여야 한다
    # (구버전은 만료 후 GET None → SET NX → True 재시작이었다).
    assert await BudgetService._index_in_warmup(fake_redis) is False
    assert await BudgetService._index_in_warmup(fake_redis) is False


# ─────────────────────────────────────────────────────────────────────────────
# r4 S1 — 프로브로 닫힌 회로를 다음 일반 성공이 재오픈하지 않는다 (실 Lua)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_close_then_success_does_not_reopen_circuit(fake_redis):
    """실 ``circuit_breaker.lua`` 를 fakeredis+lupa 로 실행한다.

    r4 이전에는 열기 판정이 is_failure 무관이라 프로브 close 직후의 일반 성공도
    옛 실패 버킷으로 회로를 재오픈했다 — 프로브 회복이 창 만료(≈30s)까지 죽는다.
    """
    from app.services.circuit_breaker import CircuitBreakerService

    cb = CircuitBreakerService(
        window_sec=30, min_calls=3, error_rate=0.5, open_sec=30,
        open_jitter_sec=0,
    )
    pmid = "m"

    for _ in range(3):
        opened = await cb.record_failure(fake_redis, pmid)
    assert opened, "3F/3T = 100% >= 50% — 회로가 열려야 한다"
    assert await cb.is_open(fake_redis, pmid)

    # 프로브 성공으로 닫는다.
    assert await cb.try_acquire_halfopen_probe(fake_redis, pmid, token="t1")
    assert await cb.record_success(fake_redis, pmid, probe_token="t1") is False
    assert not await cb.is_open(fake_redis, pmid)

    # 핵심 — close 직후 일반 성공이 회로를 재오픈하면 안 된다.
    opened = await cb.record_success(fake_redis, pmid)
    assert not opened, "성공 기록이 회로를 다시 열었다 — 옛 실패 버킷 잔존(R22 r4 S1)"
    assert not await cb.is_open(fake_redis, pmid)


@pytest.mark.asyncio
async def test_failures_still_open_circuit_after_successes(fake_redis):
    """열기 판정이 실패 시점으로 옮겨져도 실패율 경계는 그대로 동작해야 한다."""
    from app.services.circuit_breaker import CircuitBreakerService

    cb = CircuitBreakerService(
        window_sec=30, min_calls=4, error_rate=0.5, open_sec=30,
        open_jitter_sec=0,
    )
    pmid = "m"
    # F S F S — total=4, fails=2. 성공 기록은 판정을 건너뛰므로 아직 닫힘.
    await cb.record_failure(fake_redis, pmid)
    await cb.record_success(fake_redis, pmid)
    await cb.record_failure(fake_redis, pmid)
    await cb.record_success(fake_redis, pmid)
    assert not await cb.is_open(fake_redis, pmid)
    # 3F/5T = 60% >= 50% — 실패 기록에서 열린다.
    assert await cb.record_failure(fake_redis, pmid)


# ─────────────────────────────────────────────────────────────────────────────
# r4 N3 — db=None 경로는 유저 mget 을 하지 않는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_budget_db_none_skips_user_mget(mock_redis):
    """db=None 이면 복원 판정 자체가 불가 — 유저/앱 mget 은 건너뛰고 팀만 읽는다."""
    from app.services.budget_service import BudgetService
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_check"] = "--check"

    mget_calls: list[list] = []
    orig_mget = mock_redis.mget.side_effect

    async def _mget_spy(keys):
        mget_calls.append(list(keys))
        return orig_mget(keys)

    mock_redis.mget = AsyncMock(side_effect=_mget_spy)
    mock_redis.exists = AsyncMock(return_value=1)  # config 캐시 히트
    mock_redis.eval = AsyncMock(
        return_value=_json.dumps(
            {"allowed": True, "scope": "team", "config_present": True}
        ).encode()
    )

    svc = BudgetService()
    await svc.check_budget(mock_redis, None, "u1", "t1", "2026-10",
                           client="claude-code")

    assert len(mget_calls) == 1
    keys = mget_calls[0]
    assert all("team" in k for k in keys), (
        f"db=None 인데 유저/앱 키를 읽었다: {keys}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# r4 N4 — MCP 세션 id 는 initialize 응답에서만 캡처한다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_session_id_not_overwritten_by_non_initialize_echo():
    """tools/call 응답이 ``Mcp-Session-Id`` 헤더를 에코해도 세션 id를 덮어쓰지
    않는다 — 재핸드셰이크 뒤 도착한 구버전 응답이 새 세션을 오염시키던 결함."""
    import httpx
    from botocore.credentials import Credentials

    from app.services.agentcore_mcp_client import AgentCoreMcpClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "S-NEW"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "S-STALE-ECHO"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "gw___WebSearch"}]},
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "S-STALE-ECHO"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [
                            {"type": "text", "text": '{"results": []}'}
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0",
                                       "id": body.get("id"), "result": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AgentCoreMcpClient(
        http,
        "https://example.invalid/mcp",
        session_provider=lambda: Credentials("AK", "SK"),
    )
    await client.search("q")
    assert client._mcp_session_id == "S-NEW", (
        f"비-initialize 응답의 에코가 세션 id를 덮어썼다: {client._mcp_session_id}"
    )
    await http.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# r4 N5 — BUDGET_PENDINGIDX_WARMUP=false 면 웜업을 시작하지 않는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_disabled_never_starts(fake_redis, monkeypatch):
    """롤아웃 완료 선언(false)이면 since 키도 만들지 않고 항상 False —
    Redis 플러시 직후 rebuild 폭풍 창에 SCAN 폴백으로 돌아가지 않는다."""
    from app.config import get_settings
    from app.services.budget_service import BudgetService

    monkeypatch.setattr(
        get_settings(), "budget_pendingidx_warmup", False
    )
    assert await BudgetService._index_in_warmup(fake_redis) is False
    assert await fake_redis.get("budget:pendingidx:since") is None
