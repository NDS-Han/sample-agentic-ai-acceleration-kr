"""force_final 턴 — 이력에 남은 web_search 블록(누출분)과 비스트리밍 경로.

2026-09-16 US dev 실측. 상한을 낮추자(반복 3·턴당 2) force_final 턴이 자주 오게 됐고, 그때마다
Bedrock 이 400 "Tool 'web_search' not found in provided tools" 를 냈다. 원인 둘:

1. 클라이언트 이력에 **예전 턴에서 새어 나간** 우리 web_search tool_use/tool_result 가 남아
   있었다(4261967 이전 배포의 누출). force_final 은 tools 를 빼고 보내는데, 걷어내는 함수는
   *이 요청이 실행한* id 만 알았다 → 누출 블록이 남아 400.
2. 비스트리밍 루프 두 개는 force_final 에서 걷어내는 호출 자체가 없었다 → 이 경로의
   force_final 은 항상 400. Cowork 의 재시도(비스트리밍) 3회가 전부 여기서 죽었다.

계약: 루프 안에서는 클라이언트가 web_search 라는 도구를 선언하지 않았다(F-7 — 선언했으면
루프를 타지 않는다). 그러므로 이름이 web_search 인 블록은 전부 우리 것이고, force_final 에서
전부 텍스트로 바뀌어야 한다. 클라이언트 소유 도구는 건드리지 않는다.
"""

from __future__ import annotations

import json
import time

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage

GW = wsl.GW_WEB_SEARCH_NAME
LEAKED_ID = "toolu_leaked_from_a_previous_turn"


class _Mcp:
    def __init__(self, text: str = '{"results":[{"t":"WEBTEXT"}]}'):
        self.text = text
        self.calls = 0

    async def ensure_initialized(self) -> str:
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls += 1

        class _R:
            raw_text = self.text

        return _R()


def _history_with_leaked_block() -> list[dict]:
    """클라이언트가 보낸 이력 — 우리 web_search 가 예전 턴에서 새어 들어가 있다."""
    return [
        {"role": "user", "content": [{"type": "text", "text": "first question"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me search"},
                {"type": "tool_use", "id": LEAKED_ID, "name": GW, "input": {"query": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": LEAKED_ID, "content": "OLD PAID RESULT"},
                {"type": "text", "text": "second question"},
            ],
        },
    ]


def _blocks(messages: list[dict]):
    for m in messages:
        for b in m.get("content") or []:
            if isinstance(b, dict):
                yield b


# ─── 1. strip 함수가 이름으로도 잡는다 ───────────────────────────────────


def test_strip_treats_every_web_search_named_block_as_ours():
    out = wsl._strip_anthropic_web_search_plumbing(_history_with_leaked_block(), set())
    for b in _blocks(out):
        assert not (b["type"] == "tool_use" and b.get("name") == GW), (
            f"누출 tool_use 가 남았다: {b}"
        )
        assert b["type"] != "tool_result", f"tool_result 가 남았다: {b}"
    # 이미 지불한 결과 텍스트는 보존된다
    assert any(b.get("text") == "OLD PAID RESULT" for b in _blocks(out)), (
        "검색 결과 텍스트가 사라졌다"
    )
    # 대화 교대(assistant/user)가 유지되고 빈 content 가 없다
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert all(m["content"] for m in out)


def test_strip_leaves_client_owned_tools_alone():
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "shell", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}],
        },
    ]
    out = wsl._strip_anthropic_web_search_plumbing(msgs, set())
    assert out is msgs, "클라이언트 도구만 있는 이력은 바이트 단위로 그대로여야 한다"


def test_responses_items_strip_web_search_named_calls_too():
    items = [
        {"type": "message", "role": "user", "content": "q"},
        {"type": "function_call", "call_id": "call_leak", "name": GW, "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_leak", "output": "OLD PAID RESULT"},
    ]
    out = wsl._strip_responses_web_search_items(items, set())
    kinds = [i.get("type") for i in out]
    assert "function_call" not in kinds and "function_call_output" not in kinds, kinds
    assert "OLD PAID RESULT" in json.dumps(out), "지불한 결과 텍스트가 사라졌다"


# ─── 2. 비스트리밍 루프가 force_final 에서 실제로 걷어낸다 ────────────────


async def test_anthropic_nonstream_force_final_sends_no_web_search_blocks_and_no_tools():
    """모델이 1회 검색 → max_iterations=1 → 다음 턴이 force_final.
    그 턴의 상류 요청 본문에는 tools 도, web_search 블록도(이번 것·누출분 모두) 없어야 한다."""
    bodies: list[dict] = []
    turns = [
        {
            "content": [{"type": "tool_use", "id": "t_now", "name": GW, "input": {"query": "q"}}],
            "stop_reason": "tool_use",
            "usage": {},
        },
        {
            "content": [{"type": "text", "text": "final answer"}],
            "stop_reason": "end_turn",
            "usage": {},
        },
    ]

    async def invoke(body):
        bodies.append(body)
        return (
            200,
            json.dumps(turns.pop(0)).encode(),
            {},
            TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def on_usage(u):
        pass

    mcp = _Mcp()
    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"model": "m", "messages": _history_with_leaked_block()},
        mcp_client=mcp,
        on_usage=on_usage,
        max_iterations=1,
        deadline=time.monotonic() + 30,
        default_max_results=5,
    )
    assert resp.status_code == 200
    assert mcp.calls == 1, "검색은 한 번 실행돼야 한다"
    assert len(bodies) == 2, f"상류 호출 2회(검색 턴 + force_final)여야 한다: {len(bodies)}"
    final = bodies[1]
    assert "tools" not in final, "force_final 은 tools 를 빼고 보낸다"
    for b in _blocks(final["messages"]):
        assert not (b["type"] == "tool_use" and b.get("name") == GW), (
            f"web_search tool_use 가 남았다: {b}"
        )
        assert b["type"] != "tool_result", f"tool_result 가 남았다: {b}"
    joined = json.dumps(final["messages"])
    assert "OLD PAID RESULT" in joined and "WEBTEXT" in joined, (
        "지불한 검색 결과가 본문에서 사라졌다"
    )
    # 첫 턴(검색 턴)은 예전처럼 tools 를 갖고 이력을 그대로 보낸다
    assert any(t.get("name") == GW for t in bodies[0].get("tools") or [])


async def test_anthropic_nonstream_force_final_on_deadline_with_no_search_this_request():
    """이번 요청은 검색을 하나도 안 했지만(deadline 초과) 이력에 누출 블록이 있는 경우 —
    예전 코드는 our ids 가 비어 그대로 돌려보냈고, 그러면 400 이었다."""
    bodies: list[dict] = []

    async def invoke(body):
        bodies.append(body)
        return (
            200,
            json.dumps(
                {
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
            ).encode(),
            {},
            TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def on_usage(u):
        pass

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"model": "m", "messages": _history_with_leaked_block()},
        mcp_client=_Mcp(),
        on_usage=on_usage,
        max_iterations=3,
        deadline=time.monotonic() - 1,  # 이미 지났다 → 첫 턴부터 force_final
        default_max_results=5,
    )
    assert resp.status_code == 200
    assert len(bodies) == 1
    assert "tools" not in bodies[0]
    assert not any(
        b["type"] == "tool_use" and b.get("name") == GW for b in _blocks(bodies[0]["messages"])
    )


# ─── 3. thinking 만 남는 assistant 턴 + 마지막 user 메시지의 "이제 답하라" ─────────
# 2026-09-16 US 실측(Bedrock invocation log): 후속 검색 턴이 thinking+tool_use 만이라 strip 뒤
# assistant 메시지가 thinking 하나가 됐고, 마지막 user 메시지는 결과 JSON 뿐 → Opus 5 가 `<br>`.


def test_strip_adds_the_note_when_only_thinking_remains_and_answer_now_at_the_end():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig1"},
            {"type": "text", "text": "searching"},
            {"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "a"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "R1"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig2"},
            {"type": "tool_use", "id": "t2", "name": GW, "input": {"query": "b"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "R2",
                                      "cache_control": {"type": "ephemeral"}}]},
    ]
    out = wsl._strip_anthropic_web_search_plumbing(msgs, {"t1", "t2"})
    a1, a2 = out[1]["content"], out[3]["content"]
    assert [b["type"] for b in a1] == ["thinking", "text"], a1
    assert a1[1]["text"] == "searching", "텍스트가 있던 턴은 그대로"
    assert [b["type"] for b in a2] == ["thinking", "text"], a2
    assert a2[0]["signature"] == "sig2" and a2[1]["text"] == wsl._FINAL_TURN_TOOL_NOTE
    last = out[4]["content"]
    assert [b["type"] for b in last] == ["text", "text"] and last[0]["text"] == "R2"
    assert last[1]["text"] == wsl._FINAL_TURN_ANSWER_NOW
    assert all(m["content"] for m in out) and [m["role"] for m in out] == [
        "user", "assistant", "user", "assistant", "user"]


def test_answer_now_is_not_duplicated_on_repeated_strips():
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": GW, "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "R"}]},
    ]
    once = wsl._strip_anthropic_web_search_plumbing(msgs, {"t1"})
    twice = wsl._strip_anthropic_web_search_plumbing(once, {"t1"})
    assert twice is once or twice == once
    assert sum(b.get("text") == wsl._FINAL_TURN_ANSWER_NOW for b in twice[-1]["content"]) == 1
