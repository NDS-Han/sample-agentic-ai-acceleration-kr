"""클라이언트가 우리 web_search 를 tool_choice 로 강제할 때 — 첫 턴만 존중, 최종 턴은 풀기.

2026-09-16 US 실측(Bedrock invocation log). Cowork 의 내장 WebSearch 도구는 모델을
네이티브 web_search 도구 + ``tool_choice: {"type":"tool","name":"web_search"}`` + thinking off 로
호출한다("Perform a web search for the query: …"). 게이트웨이는 네이티브 도구를 걷어내고 우리
도구를 넣는데 이름이 같아 강제가 살아남았다 → 모델은 매 턴 검색만 하다가 max_iterations 에 닿고,
force_final 턴은 도구를 빼는데 tool_choice 가 남아 Bedrock 400
"Tool 'web_search' not found in provided tools". 검색 3회 과금 뒤 답 없음, 하위 에이전트가
40초 간격으로 8회 재시도.

계약(_relax_tool_choice):
- 우리 도구를 지목한 강제는 첫 턴에만 유지, 이후 턴은 auto.
- force_final(도구 제거) 턴: any/required 도 auto. tools 가 없으면 tool_choice 자체 제거.
- 클라이언트 도구를 지목한 강제·auto 는 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import json

import app.services.web_search_loop as wsl

GW = wsl.GW_WEB_SEARCH_NAME
FORCED_A = {"type": "tool", "name": GW}
FORCED_R = {"type": "function", "name": GW}


def _bodies(body: dict, dialect: str, *, client_tools: bool):
    """(turn1, turn2, force_final) as the loops build them."""
    b = dict(body)
    if client_tools:
        if dialect == "anthropic":
            b["tools"] = [{"name": "bash", "input_schema": {"type": "object"}}]
        else:
            b["tools"] = [{"type": "function", "name": "bash", "parameters": {"type": "object"}}]
    t1 = wsl._with_web_search_tool(b, dialect, include=True, first_turn=True)
    t2 = wsl._with_web_search_tool(b, dialect, include=True, first_turn=False)
    ff = wsl._with_web_search_tool(b, dialect, include=False, first_turn=False)
    return t1, t2, ff


def test_anthropic_forced_choice_on_our_tool_applies_once_then_auto():
    t1, t2, ff = _bodies({"tool_choice": FORCED_A}, "anthropic", client_tools=False)
    assert t1["tool_choice"] == FORCED_A, "첫 턴은 클라이언트 의도대로 검색을 강제한다"
    assert t2["tool_choice"] == {"type": "auto"}, "둘째 턴부터는 모델이 답할 수 있어야 한다"
    assert "tools" not in ff and "tool_choice" not in ff, ff


def test_anthropic_force_final_with_client_tools_relaxes_to_auto():
    _t1, _t2, ff = _bodies({"tool_choice": FORCED_A}, "anthropic", client_tools=True)
    assert [t["name"] for t in ff["tools"]] == ["bash"]
    assert ff["tool_choice"] == {"type": "auto"}, ff
    # any 도 최종 턴에서는 auto — 이 턴의 목적은 답변
    _t1, t2, ff2 = _bodies({"tool_choice": {"type": "any"}}, "anthropic", client_tools=True)
    assert t2["tool_choice"] == {"type": "any"}, "검색 턴에서는 any 를 건드리지 않는다"
    assert ff2["tool_choice"] == {"type": "auto"}, ff2


def test_anthropic_choice_on_a_client_tool_or_auto_is_untouched():
    for tc in ({"type": "tool", "name": "bash"}, {"type": "auto"}):
        t1, t2, ff = _bodies({"tool_choice": tc}, "anthropic", client_tools=True)
        assert t1["tool_choice"] == t2["tool_choice"] == ff["tool_choice"] == tc, tc


def test_responses_dialect_same_rules():
    t1, t2, ff = _bodies({"tool_choice": FORCED_R}, "responses", client_tools=False)
    assert t1["tool_choice"] == FORCED_R and t2["tool_choice"] == "auto", (t1, t2)
    assert "tools" not in ff and "tool_choice" not in ff, ff
    _t1, _t2, ff2 = _bodies({"tool_choice": "required"}, "responses", client_tools=True)
    assert ff2["tool_choice"] == "auto", ff2


# ─── 루프 단위: Cowork WebSearch 헬퍼 모양 그대로 ───────────────────────────


class _Mcp:
    async def ensure_initialized(self):
        return "WebSearch"

    async def search(self, q, n):
        class _R:
            raw_text = '{"results":[{"url":"https://x.com","title":"t","text":"body"}]}'
        return _R()


def _raw(ev):
    return json.dumps(ev).encode()


def _search_turn(i):
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": GW, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta",
                        "partial_json": json.dumps({"query": f"q{i}"})}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]


def _final():
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]


async def test_stream_loop_forced_choice_first_turn_only_and_clean_force_final():
    """헬퍼 모양: 시스템 + 네이티브 web_search(걷어냄) + 강제 tool_choice + thinking off.
    모델이 3턴 연속 검색하면 4턴째(force_final)는 tools/tool_choice 없이 나가야 한다."""
    turns = [_search_turn(1), _search_turn(2), _search_turn(3), _final()]
    bodies: list[dict] = []

    async def invoke_stream(body):
        bodies.append(json.loads(json.dumps(body)))
        frames = turns.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    base = {"system": "helper", "thinking": {"type": "disabled"}, "tool_choice": FORCED_A,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Perform a web search for the query: x"}]}]}
    out = b""
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body=base, mcp_client=_Mcp(), request=None,
        on_usage=on_usage, max_iterations=3, deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5, max_searches_per_turn=2,
    ):
        out += chunk
    assert len(bodies) == 4, [b.get("tool_choice") for b in bodies]
    assert bodies[0]["tool_choice"] == FORCED_A
    assert bodies[1]["tool_choice"] == bodies[2]["tool_choice"] == {"type": "auto"}
    assert "tool_choice" not in bodies[3] and "tools" not in bodies[3], bodies[3]
    assert b"answer" in out and b'"stop_reason": "end_turn"' in out
