"""The forced-final turn must tell the model that NO tool can be called in it.

2026-09-18 US Cowork (Bedrock invocation log, 07:53:19Z). A news question used both search
turns; the third turn was the forced-final one, sent with ``tool_choice: none``. That setting
blocks EVERY tool, not only our web_search. The model had planned "save a note with the client's
memory tool, then write the news", could not call the tool, and ended the turn after the
announcement alone: 191 characters of text ("Let me note what worked before writing up the
news.") out of 2,895 output tokens. The user saw a response with no answer in it.

The empty-text nudge does not catch this — there IS visible text. The instruction appended to
the forced-final turn only said that no further SEARCHES can be made, so the model had no reason
to drop the rest of its plan. Contract:

- ``_FINAL_TURN_ANSWER_NOW`` states that no tool of any kind can be called in this turn, asks
  for the complete answer now, and tells the model not to announce further steps.
- both Anthropic loops send it as the last user block of the forced-final turn, together with
  ``tool_choice: none`` (the tool definition stays, so history tool blocks remain valid).
"""

from __future__ import annotations

import asyncio
import json

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage

GW = wsl.GW_WEB_SEARCH_NAME


class _Mcp:
    async def ensure_initialized(self):
        return "WebSearch"

    async def search(self, query, max_results):
        class _R:
            raw_text = json.dumps(
                {"results": [{"url": "https://a.com", "title": "A", "text": "b"}]})
            results = None

        return _R()


def _raw(ev):
    return json.dumps(ev).encode()


def _deadline():
    return asyncio.get_event_loop().time() + 999


def test_answer_now_instruction_covers_every_tool_not_only_search():
    note = wsl._FINAL_TURN_ANSWER_NOW.lower()
    assert "no tool" in note, "must say that no tool of any kind can be called in this turn"
    assert "complete" in note and "answer" in note, "must ask for the complete answer now"
    assert "announce" in note, "must tell the model not to announce further steps"
    assert note.startswith("[") and note.endswith("]"), "stays one bracketed gateway instruction"


def _last_user_text(body: dict) -> str:
    last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
    return last[-1]["text"] if isinstance(last, list) else last


async def test_stream_forced_final_turn_carries_the_instruction_and_tool_choice_none():
    search = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": "tu_0", "name": GW, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": "q"})}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]
    final = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 20}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]
    queue, bodies = [search, final], []

    async def invoke_stream(body):
        bodies.append(body)
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    async for _ in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "MemoryWrite", "input_schema": {"type": "object"}}]},
        mcp_client=_Mcp(), request=None, on_usage=on_usage,
        max_iterations=1, deadline=_deadline(), default_max_results=5,
    ):
        pass
    assert len(bodies) == 2
    forced = bodies[1]
    assert forced["tool_choice"] == {"type": "none"}
    assert {t["name"] for t in forced["tools"]} == {"MemoryWrite", GW}, "tools stay defined"
    assert _last_user_text(forced) == wsl._FINAL_TURN_ANSWER_NOW
    assert _last_user_text(bodies[0]) == "hi", "only the forced-final turn carries it"


async def test_nonstream_forced_final_turn_carries_the_instruction_and_tool_choice_none():
    queue = [
        {"content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q"}}],
         "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 20, "output_tokens": 3}},
    ]
    bodies = []

    async def invoke(body):
        bodies.append(body)
        return (200, json.dumps(queue.pop(0)).encode(), {},
                TokenUsage(input_tokens=10, output_tokens=5))

    async def on_usage(_u):
        pass

    await wsl._anthropic_nonstream(
        invoke=invoke, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(), on_usage=on_usage,
        max_iterations=1, deadline=_deadline(), default_max_results=5,
    )
    assert len(bodies) == 2 and bodies[1]["tool_choice"] == {"type": "none"}
    assert _last_user_text(bodies[1]) == wsl._FINAL_TURN_ANSWER_NOW
