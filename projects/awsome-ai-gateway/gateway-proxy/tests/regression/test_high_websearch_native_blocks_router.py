"""되돌아온 native 검색 블록은 **라우터 진입 지점**에서 환원돼야 한다 — 루프 안에서만이 아니라.

탐침(1.0.70-native)이 server_tool_use / web_search_tool_result 블록을 응답에 넣으면, 클라이언트는
그것을 이력에 실어 다음 요청마다 되돌려 준다. 그 요청이 언제나 웹서치 루프를 타는 것은 아니다:
프로파일 토글이 꺼진 앱, MCP 미설정, 폴백 루프, Cowork 의 하이쿠 보조 요청, 그리고
/v1/messages/count_tokens. 루프 안에서만 환원하면 그런 요청이 Bedrock 400 — "되돌아온 블록은
400 이 될 수 없다" 는 탐침의 전제가 깨진다. 그래서 두 라우터 모두 본문 파싱 직후에 환원한다.

count_tokens 는 실제로 호출해 어댑터가 받은 본문을 본다. /v1/messages 는 미들웨어·상태 의존이
커서 AST 로 "파싱 직후, bedrock_body 조립 전" 위치만 확인한다(보조 검사).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.web_search_loop as wsl
from tests.unit.test_count_tokens_router import MODEL_ALIAS, _build_app

_ROUTER = Path(__file__).resolve().parents[2] / "src" / "app" / "routers" / "messages.py"
GW = wsl.GW_WEB_SEARCH_NAME


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": GW, "input": {"query": "q1"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "title": "A", "url": "https://a.com/x",
                 "encrypted_content": "e30=", "page_age": None}]},
            {"type": "text", "text": "answer"},
        ]},
        {"role": "user", "content": "more"},
    ]


@pytest.mark.asyncio
async def test_count_tokens_normalizes_echoed_native_blocks():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock(return_value=(200, 7))
    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/messages/count_tokens",
                                 json={"model": MODEL_ALIAS, "messages": _history()})
    assert resp.status_code == 200
    body_arg, _model = adapter.count_tokens.call_args.args
    body_arg = body_arg.decode() if isinstance(body_arg, bytes) else body_arg
    sent = json.loads(body_arg)
    assert "server_tool_use" not in body_arg and "web_search_tool_result" not in body_arg
    content = sent["messages"][1]["content"]
    assert [b["type"] for b in content] == ["text", "text"]
    assert content[0]["text"] == f'{wsl._TRACE_PREFIX} "q1" — 1 result (a.com)'


def _call_line(fn: ast.FunctionDef, name: str) -> int:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == name):
            return node.lineno
    raise AssertionError(f"{fn.name}: {name}() 호출이 없다")


def _assign_line(fn: ast.FunctionDef, target: str) -> int:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == target for t in node.targets
        ):
            return node.lineno
    raise AssertionError(f"{fn.name}: {target} = … 가 없다")


def test_fallback_loop_receives_the_text_normalized_body():
    """폴백 루프(우리 도구 없이 나감)는 원본 req_data 가 아니라 환원본 req_for_bedrock 을 받아야
    한다 — 원본이 가면 되돌아온 server_tool_use 가 다운그레이드 경로에서 Bedrock 400."""
    tree = ast.parse(_ROUTER.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "messages")
    call = next(node for node in ast.walk(fn) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "run_fallback_loop")
    kw = next(k for k in call.keywords if k.arg == "req_data")
    names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
    assert "req_for_bedrock" in names and "req_data" not in names, ast.dump(kw.value)


def test_both_routes_normalize_before_building_the_bedrock_body():
    tree = ast.parse(_ROUTER.read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    assert {"messages", "count_tokens"} <= set(fns), sorted(fns)
    for name in ("messages", "count_tokens"):
        fn = fns[name]
        norm = _call_line(fn, "normalize_inbound_native_blocks")
        build = _assign_line(fn, "bedrock_body")
        assert norm < build, f"{name}: 환원({norm})이 bedrock_body 조립({build})보다 앞서야 한다"
