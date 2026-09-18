"""Web search and the client's own tools in the same request (2026-09-18, US Cowork).

Two defects, both from Bedrock invocation logs of real Cowork runs:

1. MIXED TURN. The model calls a client tool and ``web_search`` in one turn (Cowork does it on
   almost every task prompt: ``TaskCreate`` ×4 + ``web_search`` ×3). The turn has to go to the
   client, and the searches were dropped without a trace; the model re-issued them one or two
   requests later (foundry comparison: 10 searches requested, 5 run; Vera Rubin: 7 requested,
   3 run, ~35 s and 2.8k output tokens spent on the dropped turn).
2. EXHAUSTED BUDGET. The forced-final turn uses ``tool_choice: none``, which blocks the
   client's tools as well. "Search the news and save it as samsung-news.md" ended with the
   summary as text and "the file tool is not available in this turn" — no file.

Contract (both behind flags, default = the previous behaviour):

- ``mixed_turn_run`` (native trace mode only): the searches of a mixed turn ARE run; their
  ``server_tool_use`` / ``web_search_tool_result`` pairs go out in the same message as the
  client's ``tool_use``, ``stop_reason: tool_use``. This is the shape a server tool has in the
  Messages API. In text mode the results could not travel back with the next request, so the
  searches stay "not run" there.
- The replayed message must become a history Bedrock accepts: strict role alternation and,
  for every ``tool_use``, its ``tool_result`` in the immediately following user message.
- ``final_turn_soft``: the first turn after the search budget is used up keeps every tool
  callable. A client tool call goes to the client as usual; a further ``web_search`` is
  answered with a "budget used up" error without running (native: ``max_uses_exceeded``), and
  only then the hard final turn (``tool_choice: none``) follows. A passed deadline goes to the
  hard final turn directly. The loop stays bounded.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage
from tests.regression import test_high_websearch_scenario_sweep as sweep

GW = wsl.GW_WEB_SEARCH_NAME
PREFIX = wsl._TRACE_PREFIX
NATIVE = ("server_tool_use", "web_search_tool_result")
CLIENT_TOOLS = [
    {"name": "TaskCreate", "input_schema": {"type": "object"}},
    {"name": "Write", "input_schema": {"type": "object"}},
]


# ── fakes ─────────────────────────────────────────────────────────────────────────────────
class _Mcp:
    def __init__(self):
        self.queries: list[str] = []

    async def ensure_initialized(self):
        return "WebSearch"

    async def search(self, query, max_results):
        self.queries.append(query)
        n = len(self.queries)

        class _R:
            raw_text = json.dumps({"results": [
                {"url": f"https://site{n}.example/a", "title": f"T{n}",
                 "text": f"body number {n} about {query} with enough distinct words to keep",
                 "publishedDate": "09:25AM, Tuesday, March 17 2026, PDT"}]})
            results = None

        return _R()


def _deadline(passed: bool = False) -> float:
    now = asyncio.get_event_loop().time()
    return now - 1 if passed else now + 999


def _tu(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


def _search(i, q):
    return _tu(i, GW, {"query": q})


def _raw(ev) -> bytes:
    return json.dumps(ev).encode()


def _stream_turn(blocks: list[dict], stop: str) -> list[bytes]:
    frames = [_raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            frames += [
                _raw({"type": "content_block_start", "index": i,
                      "content_block": {"type": "text", "text": ""}}),
                _raw({"type": "content_block_delta", "index": i,
                      "delta": {"type": "text_delta", "text": b["text"]}}),
            ]
        else:
            frames += [
                _raw({"type": "content_block_start", "index": i,
                      "content_block": {**b, "input": {}}}),
                _raw({"type": "content_block_delta", "index": i,
                      "delta": {"type": "input_json_delta",
                                "partial_json": json.dumps(b["input"])}}),
            ]
        frames.append(_raw({"type": "content_block_stop", "index": i}))
    frames += [_raw({"type": "message_delta", "delta": {"stop_reason": stop},
                     "usage": {"output_tokens": 5}}),
               _raw({"type": "message_stop"})]
    return frames


def _client_view(sse: list[bytes]) -> tuple[list[dict], str | None]:
    """Assemble the content blocks the way a client SDK does; returns (content, stop_reason)."""
    return _assemble([json.loads(line[5:]) for chunk in sse
                      for line in chunk.decode().splitlines() if line.startswith("data:")])


def _assemble(events: list[dict]) -> tuple[list[dict], str | None]:
    blocks: dict[int, dict] = {}
    stop = None
    for ev in events:
        t = ev.get("type")
        if t == "content_block_start":
            b = dict(ev["content_block"])
            if b["type"] in ("tool_use", "server_tool_use"):
                b["_json"] = ""
            blocks[ev["index"]] = b
        elif t == "content_block_delta":
            b, d = blocks[ev["index"]], ev["delta"]
            if d["type"] == "text_delta":
                b["text"] = b.get("text", "") + d["text"]
            elif d["type"] == "input_json_delta":
                b["_json"] += d["partial_json"]
        elif t == "content_block_stop":
            b = blocks[ev["index"]]
            if "_json" in b:
                b["input"] = json.loads(b.pop("_json") or "{}")
        elif t == "message_delta":
            stop = ev["delta"].get("stop_reason", stop)
    assert sorted(blocks) == list(range(len(blocks))), "envelope indices must be 0..n-1"
    return [blocks[i] for i in sorted(blocks)], stop


async def _run_stream(turns: list[tuple[list[dict], str]], *, mcp=None, max_iterations=2,
                      deadline=None, **kw):
    mcp = mcp or _Mcp()
    queue = [_stream_turn(b, s) for b, s in turns]
    bodies: list[dict] = []
    usage: list[TokenUsage] = []

    async def invoke_stream(body):
        bodies.append(json.loads(json.dumps(body)))
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(u):
        usage.append(u)

    out = [c async for c in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}], "tools": CLIENT_TOOLS},
        mcp_client=mcp, request=None, on_usage=on_usage, max_iterations=max_iterations,
        deadline=deadline if deadline is not None else _deadline(),
        default_max_results=5, max_searches_per_turn=3, **kw)]
    content, stop = _client_view(out)
    return content, stop, bodies, mcp, usage[-1]


async def _run_nonstream(turns: list[tuple[list[dict], str]], *, mcp=None, max_iterations=2,
                         deadline=None, **kw):
    mcp = mcp or _Mcp()
    queue = [{"content": b, "stop_reason": s, "usage": {"input_tokens": 10, "output_tokens": 5}}
             for b, s in turns]
    bodies: list[dict] = []
    usage: list[TokenUsage] = []

    async def invoke(body):
        bodies.append(json.loads(json.dumps(body)))
        return (200, json.dumps(queue.pop(0)).encode(), {},
                TokenUsage(input_tokens=10, output_tokens=5))

    async def on_usage(u):
        usage.append(u)

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}], "tools": CLIENT_TOOLS},
        mcp_client=mcp, on_usage=on_usage, max_iterations=max_iterations,
        deadline=deadline if deadline is not None else _deadline(),
        default_max_results=5, max_searches_per_turn=3, **kw)
    body = json.loads(resp.body)
    return body["content"], body.get("stop_reason"), bodies, mcp, usage[-1]


# ── invariants ────────────────────────────────────────────────────────────────────────────
def _assert_bedrock_valid(messages: list[dict]) -> None:
    """What Bedrock checks on a Messages history, as far as our rewrite can break it."""
    roles = [m["role"] for m in messages]
    assert all(a != b for a, b in zip(roles, roles[1:])), f"roles must alternate: {roles}"
    open_ids: set[str] = set()
    for i, m in enumerate(messages):
        content = m["content"] if isinstance(m["content"], list) else []
        assert not any(b.get("type") in NATIVE for b in content), "native block left in history"
        if m["role"] == "assistant":
            assert not open_ids, "tool_use without a tool_result in the next user message"
            assert content, "empty assistant message"
            open_ids = {b["id"] for b in content if b.get("type") == "tool_use"}
            kinds = [b.get("type") for b in content]
            if "tool_use" in kinds:   # a model turn ends with its tool calls
                assert set(kinds[kinds.index("tool_use"):]) == {"tool_use"}, kinds
        else:
            results = [b for b in content if b.get("type") == "tool_result"]
            assert {b["tool_use_id"] for b in results} == open_ids, (i, open_ids)
            kinds = [b.get("type") for b in content]
            assert kinds[:len(results)] == ["tool_result"] * len(results), \
                f"tool_result blocks must come first: {kinds}"
            open_ids = set()
    assert not open_ids


def _replay(content: list[dict]) -> list[dict]:
    """The next request as the client sends it: the assistant message verbatim, then one
    tool_result per CLIENT tool call."""
    results = [{"type": "tool_result", "tool_use_id": b["id"], "content": "ok"}
               for b in content if b.get("type") == "tool_use"]
    return [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": content},
            {"role": "user", "content": results}]


MIXED = [{"type": "text", "text": "plan"}, _tu(1, "TaskCreate", {"subject": "s"}),
         _search(2, "q one"), _search(3, "q two")]


# ── ① mixed turn ──────────────────────────────────────────────────────────────────────────
async def test_stream_mixed_turn_runs_the_searches_next_to_the_client_tool_call():
    content, stop, bodies, mcp, usage = await _run_stream(
        [(MIXED, "tool_use")], native_trace=True, mixed_turn_run=True)
    assert mcp.queries == ["q one", "q two"], "both searches of the mixed turn must run"
    assert len(bodies) == 1, "a mixed turn is terminal — the client has to run its tool"
    assert stop == "tool_use"
    assert usage.web_search_count == 2
    kinds = [b["type"] for b in content]
    assert kinds.count("tool_use") == 1 and content[kinds.index("tool_use")]["name"] == "TaskCreate"
    assert kinds.count("server_tool_use") == 2 and kinds.count("web_search_tool_result") == 2
    assert not any(b["type"] == "tool_use" and b["name"] == GW for b in content)
    trace = "\n".join(b["text"] for b in content if b["type"] == "text")
    assert trace.count(PREFIX) == 2 and "not run" not in trace


async def test_nonstream_mixed_turn_runs_the_searches_next_to_the_client_tool_call():
    content, stop, bodies, mcp, usage = await _run_nonstream(
        [(MIXED, "tool_use")], native_trace=True, mixed_turn_run=True)
    assert mcp.queries == ["q one", "q two"] and len(bodies) == 1 and stop == "tool_use"
    assert usage.web_search_count == 2
    kinds = [b["type"] for b in content]
    assert kinds.count("tool_use") == 1 and kinds.count("server_tool_use") == 2
    assert kinds.count("web_search_tool_result") == 2
    assert not any(b["type"] == "tool_use" and b["name"] == GW for b in content)


async def test_mixed_turn_replay_becomes_a_history_bedrock_accepts():
    for run in (_run_stream, _run_nonstream):
        content, *_ = await run([(MIXED, "tool_use")], native_trace=True, mixed_turn_run=True)
        body = {"messages": _replay(content), "tools": CLIENT_TOOLS}
        rewritten = wsl._rewrite_inbound_native_blocks(body)["messages"]
        _assert_bedrock_valid(rewritten)
        uses = [b for m in rewritten if m["role"] == "assistant"
                for b in m["content"] if b.get("type") == "tool_use"]
        assert sorted(b["name"] for b in uses) == sorted(["TaskCreate", GW, GW])
        results = [b for m in rewritten if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        ours = [b for b in results if b["tool_use_id"].startswith("toolu_gw_")]
        assert len(ours) == 2 and all("site" in b["content"] for b in ours), \
            "the search results must reach the model through the rebuilt tool_result"
        assert PREFIX not in json.dumps(rewritten, ensure_ascii=False)
        # requests that go out WITHOUT our tool (fallback loop, count_tokens …)
        normalized = wsl._normalize_inbound_native_blocks(body)["messages"]
        _assert_bedrock_valid(normalized)


async def test_search_turn_then_mixed_turn_replay_is_valid_too():
    turns = [([_search(1, "first")], "tool_use"),
             ([{"type": "text", "text": "now both"}, _tu(2, "TaskCreate", {"subject": "s"}),
               _search(3, "second")], "tool_use")]
    for run in (_run_stream, _run_nonstream):
        content, stop, bodies, mcp, _ = await run(turns, native_trace=True, mixed_turn_run=True)
        assert mcp.queries == ["first", "second"] and len(bodies) == 2 and stop == "tool_use"
        rewritten = wsl._rewrite_inbound_native_blocks(
            {"messages": _replay(content), "tools": CLIENT_TOOLS})["messages"]
        _assert_bedrock_valid(rewritten)


async def test_mixed_turn_stays_not_run_without_the_flag_and_in_text_mode():
    for run in (_run_stream, _run_nonstream):
        # flag off (default) — previous behaviour
        content, stop, _, mcp, usage = await run([(MIXED, "tool_use")], native_trace=True)
        assert mcp.queries == [] and usage.web_search_count == 0 and stop == "tool_use"
        assert not any(b["type"] in NATIVE for b in content)
        # text mode: results could not come back with the next request → never run
        content, stop, _, mcp, _ = await run([(MIXED, "tool_use")], native_trace=False,
                                             mixed_turn_run=True)
        assert mcp.queries == [] and stop == "tool_use"
        text = "\n".join(b["text"] for b in content if b["type"] == "text")
        assert text.count(PREFIX) == 2 and "not run" in text


async def test_mixed_turn_respects_the_per_turn_cap():
    blocks = [_tu(1, "TaskCreate", {"subject": "s"})] + [_search(i, f"q{i}") for i in range(2, 6)]
    for run in (_run_stream, _run_nonstream):
        content, _, _, mcp, usage = await run([(blocks, "tool_use")], native_trace=True,
                                              mixed_turn_run=True)
        assert len(mcp.queries) == 3 and usage.web_search_count == 3
        results = [b for b in content if b["type"] == "web_search_tool_result"]
        errors = [b for b in results if isinstance(b["content"], dict)]
        assert len(results) == 4 and len(errors) == 1
        assert errors[0]["content"]["error_code"] == "max_uses_exceeded"
        _assert_bedrock_valid(wsl._rewrite_inbound_native_blocks(
            {"messages": _replay(content), "tools": CLIENT_TOOLS})["messages"])


# ── ② exhausted budget ────────────────────────────────────────────────────────────────────
def _last_user_text(body: dict) -> str:
    last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
    return last[-1].get("text", "") if isinstance(last, list) else last


async def test_soft_final_turn_lets_the_client_tool_through():
    turns = [([_search(1, "news")], "tool_use"),
             ([{"type": "text", "text": "saving"},
               _tu(2, "Write", {"file_path": "samsung-news.md", "content": "…"})], "tool_use")]
    for run in (_run_stream, _run_nonstream):
        content, stop, bodies, mcp, _ = await run(turns, max_iterations=1, native_trace=True,
                                                  final_turn_soft=True)
        assert len(bodies) == 2 and mcp.queries == ["news"]
        soft = bodies[1]
        assert soft.get("tool_choice") != {"type": "none"}, "client tools must stay callable"
        assert {t["name"] for t in soft["tools"]} == {"TaskCreate", "Write", GW}
        assert _last_user_text(soft) == wsl._FINAL_TURN_SEARCH_EXHAUSTED
        assert stop == "tool_use"
        assert any(b["type"] == "tool_use" and b["name"] == "Write" for b in content)


async def test_soft_final_turn_answers_a_further_search_with_an_error_then_goes_hard():
    turns = [([_search(1, "a")], "tool_use"), ([_search(2, "b")], "tool_use"),
             ([{"type": "text", "text": "answer"}], "end_turn")]
    for run in (_run_stream, _run_nonstream):
        content, stop, bodies, mcp, usage = await run(turns, max_iterations=1,
                                                      native_trace=True, final_turn_soft=True)
        assert mcp.queries == ["a"], "a search past the budget must not reach the connector"
        assert usage.web_search_count == 1
        assert len(bodies) == 3 and stop == "end_turn"
        assert bodies[1].get("tool_choice") != {"type": "none"}
        hard = bodies[2]
        assert hard["tool_choice"] == {"type": "none"}
        assert _last_user_text(hard) == wsl._FINAL_TURN_ANSWER_NOW
        _assert_bedrock_valid(hard["messages"])
        refused = [b for m in hard["messages"] if isinstance(m["content"], list)
                   for b in m["content"]
                   if b.get("type") == "tool_result" and b.get("tool_use_id") == "toolu_2"]
        assert len(refused) == 1 and refused[0].get("is_error") is True
        said = str(refused[0]["content"]).lower()
        assert "not run" in said and "request it again" not in said, \
            "the per-turn wording invites a retry; an exhausted budget must not"
        errors = [b for b in content if b["type"] == "web_search_tool_result"
                  and isinstance(b["content"], dict)]
        assert [e["content"]["error_code"] for e in errors] == ["max_uses_exceeded"]
        assert any(b["type"] == "text" and b["text"].strip() == "answer" for b in content)


async def test_soft_final_turn_with_a_mixed_call_refuses_the_search_and_passes_the_tool():
    turns = [([_search(1, "a")], "tool_use"),
             ([_tu(2, "Write", {"file_path": "f.md", "content": "x"}), _search(3, "b")],
              "tool_use")]
    for run in (_run_stream, _run_nonstream):
        content, stop, bodies, mcp, _ = await run(turns, max_iterations=1, native_trace=True,
                                                  final_turn_soft=True, mixed_turn_run=True)
        assert mcp.queries == ["a"] and len(bodies) == 2 and stop == "tool_use"
        errors = [b for b in content if b["type"] == "web_search_tool_result"
                  and isinstance(b["content"], dict)]
        assert [e["content"]["error_code"] for e in errors] == ["max_uses_exceeded"]
        _assert_bedrock_valid(wsl._rewrite_inbound_native_blocks(
            {"messages": _replay(content), "tools": CLIENT_TOOLS})["messages"])


async def test_a_model_that_always_searches_still_terminates():
    max_iterations = 2
    turns = [([_search(i, f"q{i}")], "tool_use") for i in range(1, 10)]
    for run in (_run_stream, _run_nonstream):
        _, stop, bodies, mcp, _ = await run(list(turns), max_iterations=max_iterations,
                                            native_trace=True, final_turn_soft=True)
        assert len(bodies) <= max_iterations + 2, "soft round + hard round, then stop"
        assert len(mcp.queries) == max_iterations
        assert bodies[-1]["tool_choice"] == {"type": "none"}
        assert stop == "end_turn", "a tool_use stop without a visible tool call hangs the client"


async def test_a_passed_deadline_goes_to_the_hard_final_turn_directly():
    turns = [([{"type": "text", "text": "answer"}], "end_turn")]
    for run in (_run_stream, _run_nonstream):
        _, _, bodies, _, _ = await run(turns, deadline=_deadline(passed=True),
                                       native_trace=True, final_turn_soft=True)
        assert len(bodies) == 1 and bodies[0]["tool_choice"] == {"type": "none"}
        assert _last_user_text(bodies[0]) == wsl._FINAL_TURN_ANSWER_NOW


async def test_without_the_flag_the_budget_end_is_the_hard_final_turn_as_before():
    turns = [([_search(1, "a")], "tool_use"), ([{"type": "text", "text": "answer"}], "end_turn")]
    for run in (_run_stream, _run_nonstream):
        _, _, bodies, _, _ = await run(turns, max_iterations=1, native_trace=True)
        assert bodies[1]["tool_choice"] == {"type": "none"}
        assert _last_user_text(bodies[1]) == wsl._FINAL_TURN_ANSWER_NOW


# ── the whole scenario sweep again, with both flags on ────────────────────────────────────
# The sweep pins the default (flags off) contract. With the flags on, the flag-independent
# invariants must still hold for every scenario, every asked search must leave exactly one
# 🔎 line and one native pair (ran, failed, capped or refused), and whatever the client got
# must replay into a history Bedrock accepts.


@pytest.mark.parametrize("name", list(sweep.SCENARIOS))
async def test_sweep_scenarios_hold_with_both_flags_on(name):
    turns, asked, _succeeded, client_tool, kw = sweep.SCENARIOS[name]
    for streaming in (True, False):
        kw2 = dict(kw)
        mcp = sweep._Mcp(fail=kw2.pop("fail", False))
        flags = {"native_trace": True, "mixed_turn_run": True, "final_turn_soft": True}
        if streaming:
            events, usages, bodies = await sweep.run_stream(turns, mcp, **flags, **kw2)
            kinds = [e for e, _ in events]
            assert kinds.count("message_start") == 1 and kinds[-1] == "message_stop"
            sweep._stream_blocks(events)          # its own open/close/index assertions
            content, _ = _assemble([d for _e, d in events])
            md = next(d for e, d in events if e == "message_delta")
            stop, gauge = md["delta"]["stop_reason"], md["usage"]["input_tokens"]
        else:
            body, usages, bodies = await sweep.run_nonstream(turns, mcp, **flags, **kw2)
            content, stop = body["content"], body["stop_reason"]
            gauge = body["usage"]["input_tokens"]
        assert content and gauge == 10
        assert not any(b.get("type") == "tool_use" and b.get("name") == GW for b in content)
        assert (stop == "tool_use") == client_tool
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        traces = [ln for ln in text.splitlines() if ln.startswith(PREFIX)]
        pairs = sum(1 for b in content if b.get("type") == "server_tool_use")
        assert len(traces) == pairs == asked, (name, streaming, traces)
        assert sum(1 for b in content if b.get("type") == "web_search_tool_result") == asked
        assert len(usages) == 1 and (mcp.fail or usages[0].web_search_count == len(mcp.calls))
        sweep._tool_results_valid(bodies)
        _assert_bedrock_valid(wsl._rewrite_inbound_native_blocks(
            {"messages": _replay(content), "tools": CLIENT_TOOLS})["messages"])


# ── wiring: settings → dispatcher → loops ─────────────────────────────────────────────────
async def test_dispatcher_hands_the_flags_to_the_anthropic_loops_only(monkeypatch):
    from fastapi.responses import JSONResponse

    seen: dict[str, dict] = {}

    def fake(name):
        async def loop(**kw):
            seen[name] = kw
            return JSONResponse(status_code=200, content={"content": []})
        return loop

    monkeypatch.setattr(wsl, "_anthropic_nonstream", fake("anthropic"))
    monkeypatch.setattr(wsl, "_responses_nonstream", fake("responses"))
    monkeypatch.setattr(wsl, "_client_tool_flags",
                        lambda: {"mixed_turn_run": True, "final_turn_soft": True})

    async def on_usage(_u):
        pass

    for dialect, body in (("anthropic", {"messages": [{"role": "user", "content": "hi"}]}),
                          ("responses", {"input": "hi"})):
        await wsl.run_web_search_loop(
            dialect=dialect, invoke=None, invoke_stream=None, initial_req_data=body,
            is_stream=False, mcp_client=_Mcp(), request=None, on_usage=on_usage)
    assert seen["anthropic"]["mixed_turn_run"] is True
    assert seen["anthropic"]["final_turn_soft"] is True
    assert "mixed_turn_run" not in seen["responses"], "the Responses loops are out of scope"
    assert "final_turn_soft" not in seen["responses"]
