"""Scenario sweep over the Anthropic loops: the same invariants across every combination.

Existing regression files each pin one incident. This sweep runs a table of scenarios through
both Anthropic loops (stream / non-stream) in both trace modes (text / native) and checks the
invariants that must hold everywhere:

- the client envelope is well-formed (one message_start … message_stop, every content block
  opened is closed, indices unique) and never contains our tool_use blocks;
- the number of 🔎 trace lines equals the number of searches the model asked for (ran + capped),
  and in native mode the number of server_tool_use/web_search_tool_result pairs matches too;
- the client-facing usage carries the FIRST turn's prompt buckets;
- ``on_usage`` fires exactly once with ``web_search_count`` == successful searches;
- ``stop_reason`` is never ``tool_use`` unless a client tool call was forwarded;
- every JSON tool_result the model received parses.

Written as the verification pass for the 2026-09-17 English-docstring refactor of
``web_search_loop.py`` (code AST-identical; this sweep is the behavioural double check).
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage
from app.services.agentcore_mcp_client import AgentCoreMcpError

GW = wsl.GW_WEB_SEARCH_NAME
PREFIX = wsl._TRACE_PREFIX
ITEMS = [
    {"url": f"https://s{i}.example.com/a", "title": f"T{i}", "text": f"Body {i}. " * 40}
    for i in range(6)
]


class _Mcp:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def ensure_initialized(self):
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls.append(query)
        if self.fail:
            raise AgentCoreMcpError("connector down")

        class _R:
            raw_text = json.dumps({"results": ITEMS[:max_results]})
            results = None

        return _R()


class _Req:
    headers: dict = {}
    scope = {"state": {"client": "cowork"}}

    async def is_disconnected(self):
        return False


def _raw(ev):
    return json.dumps(ev).encode()


def _deadline():
    return asyncio.get_event_loop().time() + 999


# ── turn builders (stream frames and non-stream bodies from one description) ──────────
def _turn_frames(spec: dict) -> list[bytes]:
    """spec: {searches: [q...], client_tool: name|None, texts: [...], thinking: bool,
    stop: str, error_mid: bool}"""
    ev = [_raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})]
    i = 0
    if spec.get("thinking"):
        ev += [
            _raw(
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "thinking", "thinking": ""},
                }
            ),
            _raw(
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                }
            ),
            _raw({"type": "content_block_stop", "index": i}),
        ]
        i += 1
    for t in spec.get("texts", []):
        ev += [
            _raw(
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            _raw(
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": t},
                }
            ),
            _raw({"type": "content_block_stop", "index": i}),
        ]
        i += 1
    if spec.get("client_tool"):
        ev += [
            _raw(
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"ct_{i}",
                        "name": spec["client_tool"],
                        "input": {},
                    },
                }
            ),
            _raw(
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": "{}"},
                }
            ),
            _raw({"type": "content_block_stop", "index": i}),
        ]
        i += 1
    for q in spec.get("searches", []):
        ev += [
            _raw(
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "tool_use", "id": f"tu_{i}", "name": GW, "input": {}},
                }
            ),
            _raw(
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": q})},
                }
            ),
            _raw({"type": "content_block_stop", "index": i}),
        ]
        i += 1
    if spec.get("error_mid"):
        ev.append(_raw({"error": {"type": "provider_error", "message": "boom"}}))
        return ev
    ev += [
        _raw(
            {
                "type": "message_delta",
                "delta": {"stop_reason": spec.get("stop", "end_turn")},
                "usage": {"output_tokens": 5},
            }
        ),
        _raw({"type": "message_stop"}),
    ]
    return ev


def _turn_body(spec: dict) -> dict:
    content: list = []
    i = 0
    if spec.get("thinking"):
        content.append({"type": "thinking", "thinking": "hmm", "signature": "s"})
    for t in spec.get("texts", []):
        content.append({"type": "text", "text": t})
    if spec.get("client_tool"):
        content.append(
            {"type": "tool_use", "id": f"ct_{i}", "name": spec["client_tool"], "input": {}}
        )
        i += 1
    for q in spec.get("searches", []):
        content.append({"type": "tool_use", "id": f"tu_{i}", "name": GW, "input": {"query": q}})
        i += 1
    return {
        "content": content,
        "stop_reason": spec.get("stop", "end_turn"),
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# ── runners ───────────────────────────────────────────────────────────────────────────
async def run_stream(turns, mcp, **kw):
    queue = [_turn_frames(t) for t in turns]
    usages: list[TokenUsage] = []
    bodies: list[dict] = []

    async def invoke_stream(body):
        bodies.append(body)
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(u):
        usages.append(u)

    out: list[str] = []
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp,
        request=_Req(),
        on_usage=on_usage,
        max_iterations=kw.pop("max_iterations", 5),
        deadline=_deadline(),
        default_max_results=5,
        **kw,
    ):
        out.append(chunk.decode())
    events = []
    for chunk in out:
        head, _, rest = chunk.partition("\n")
        events.append((head.replace("event: ", ""), json.loads(rest.partition("data: ")[2])))
    return events, usages, bodies


async def run_nonstream(turns, mcp, **kw):
    queue = [_turn_body(t) for t in turns]
    usages: list[TokenUsage] = []
    bodies: list[dict] = []

    async def invoke(body):
        bodies.append(body)
        return (
            200,
            json.dumps(queue.pop(0)).encode(),
            {},
            TokenUsage(input_tokens=10, output_tokens=5),
        )

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp,
        on_usage=on_usage,
        max_iterations=kw.pop("max_iterations", 5),
        deadline=_deadline(),
        default_max_results=5,
        **kw,
    )
    return json.loads(bytes(resp.body)), usages, bodies


def _stream_blocks(events):
    blocks: dict[int, dict] = {}
    order: list[int] = []
    for e, d in events:
        if e == "content_block_start":
            assert d["index"] not in blocks, "content block index reused"
            blocks[d["index"]] = {"type": d["content_block"]["type"], "text": "", "open": True}
            order.append(d["index"])
        elif e == "content_block_delta" and d["delta"].get("type") == "text_delta":
            blocks[d["index"]]["text"] += d["delta"]["text"]
        elif e == "content_block_stop":
            assert blocks[d["index"]]["open"], "stop for a block that is not open"
            blocks[d["index"]]["open"] = False
    assert not any(b["open"] for b in blocks.values()), "block left open"
    return [blocks[i] for i in order]


def _tool_results_valid(bodies):
    for b in bodies:
        for m in b.get("messages", []):
            if m["role"] != "user" or isinstance(m["content"], str):
                continue
            for blk in m["content"]:
                if (
                    blk.get("type") == "tool_result"
                    and isinstance(blk.get("content"), str)
                    and blk["content"].startswith("{")
                ):
                    json.loads(blk["content"])


SCENARIOS = {
    # name: (turns, expected searches asked, expected successful, client_tool, kwargs)
    "single search": (
        [{"searches": ["q1"], "stop": "tool_use"}, {"texts": ["done"]}],
        1,
        1,
        False,
        {},
    ),
    "fan-out 4 capped to 3": (
        [{"searches": ["a", "b", "c", "d"], "stop": "tool_use"}, {"texts": ["done"]}],
        4,
        3,
        False,
        {"max_searches_per_turn": 3},
    ),
    "three rounds, cap 2 → forced final": (
        [
            {"searches": ["q1"], "stop": "tool_use"},
            {"searches": ["q2"], "stop": "tool_use"},
            {"texts": ["final"]},
        ],
        2,
        2,
        False,
        {"max_iterations": 2},
    ),
    "connector failure": (
        [{"searches": ["q1"], "stop": "tool_use"}, {"texts": ["done"]}],
        1,
        0,
        False,
        {"fail": True},
    ),
    "mixed turn (client tool + search)": (
        [{"client_tool": "Read", "searches": ["q1"], "stop": "tool_use"}],
        1,
        0,
        True,
        {},
    ),
    "imitated trace lines": (
        [
            {"searches": ["q1"], "stop": "tool_use"},
            {"texts": [f'{PREFIX} "fake" — 9 results\n', "real answer"]},
        ],
        1,
        1,
        False,
        {},
    ),
    "thinking-only final → nudge": (
        [{"searches": ["q1"], "stop": "tool_use"}, {"thinking": True}, {"texts": ["after nudge"]}],
        1,
        1,
        False,
        {"max_iterations": 1},
    ),
    "no search at all": ([{"texts": ["plain"]}], 0, 0, False, {}),
}


@pytest.mark.parametrize("native", [False, True], ids=["text", "native"])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_stream_invariants(name, native):
    turns, asked, succeeded, client_tool, kw = SCENARIOS[name]
    kw = dict(kw)
    mcp = _Mcp(fail=kw.pop("fail", False))
    events, usages, bodies = await run_stream(turns, mcp, native_trace=native, **kw)
    kinds = [e for e, _ in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop", kinds
    assert kinds.count("message_start") == 1 and kinds.count("message_stop") == 1
    blocks = _stream_blocks(events)
    assert all(b["type"] != "tool_use" or client_tool for b in blocks), "our tool_use leaked"
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    traces = [ln for ln in text.splitlines() if ln.startswith(PREFIX)]
    # Contract: one 🔎 line per search asked. A mixed turn (client tool + our search) does not
    # run the search; text mode says so with a "not run" line, native mode emits nothing yet
    # (open follow-up).
    expected_traces = 0 if (client_tool and native) else asked
    assert len(traces) == expected_traces, (name, traces)
    if client_tool and not native:
        assert all("not run" in ln for ln in traces)
    if native:
        assert sum(1 for b in blocks if b["type"] == "server_tool_use") == (
            0 if client_tool else asked
        )
        assert sum(1 for b in blocks if b["type"] == "web_search_tool_result") == (
            0 if client_tool else asked
        )
    assert not any("fake" in ln for ln in traces), "imitated trace line reached the client"
    md = next(d for e, d in events if e == "message_delta")
    assert md["usage"]["input_tokens"] == 10, "client usage must be the FIRST turn's prompt"
    assert (md["delta"]["stop_reason"] == "tool_use") == client_tool
    assert len(usages) == 1 and usages[0].web_search_count == succeeded
    assert mcp.fail or len(mcp.calls) == succeeded, (mcp.calls, succeeded)
    _tool_results_valid(bodies)


@pytest.mark.parametrize("native", [False, True], ids=["text", "native"])
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_nonstream_invariants(name, native):
    turns, asked, succeeded, client_tool, kw = SCENARIOS[name]
    kw = dict(kw)
    mcp = _Mcp(fail=kw.pop("fail", False))
    body, usages, bodies = await run_nonstream(turns, mcp, native_trace=native, **kw)
    content = body["content"]
    assert not any(b.get("type") == "tool_use" and b.get("name") == GW for b in content)
    assert not any(b.get("type") in ("thinking", "redacted_thinking") for b in content)
    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    traces = [ln for ln in text.splitlines() if ln.startswith(PREFIX)]
    expected_traces = 0 if (client_tool and native) else asked  # same contract as streaming
    assert len(traces) == expected_traces, (name, traces)
    if native:
        assert sum(1 for b in content if b.get("type") == "server_tool_use") == (
            0 if client_tool else asked
        )
    assert not any("fake" in ln for ln in traces)
    assert body["usage"]["input_tokens"] == 10
    assert (body["stop_reason"] == "tool_use") == client_tool
    assert len(usages) == 1 and usages[0].web_search_count == succeeded
    assert content, "never an empty content list"
    _tool_results_valid(bodies)


async def test_stream_upstream_error_mid_turn_is_not_reported_as_success():
    mcp = _Mcp()
    events, usages, _ = await run_stream(
        [{"searches": ["q1"], "stop": "tool_use"}, {"texts": ["partial"], "error_mid": True}], mcp
    )
    kinds = [e for e, _ in events]
    assert "error" in kinds and "message_delta" not in kinds, kinds
    assert kinds[-1] == "message_stop" and len(usages) == 1
    _stream_blocks(events)


async def test_stream_non_200_first_turn_is_drained_without_envelope():
    async def invoke_stream(_body):
        async def gen():
            yield _raw({"type": "error", "error": {"type": "overloaded", "message": "busy"}})

        return 529, gen(), {}, None

    calls = []

    async def on_usage(u):
        calls.append(u)

    out = []
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(),
        request=_Req(),
        on_usage=on_usage,
        max_iterations=2,
        deadline=_deadline(),
        default_max_results=5,
    ):
        out.append(chunk.decode())
    assert out and out[0].startswith("event: error") and "message_stop" not in "".join(out)
    assert len(calls) == 1 and calls[0].web_search_count == 0


async def test_native_replay_round_trip_through_the_loop():
    """Blocks emitted by one request come back as history and are rewritten for the next."""
    mcp = _Mcp()
    body, _, _ = await run_nonstream(
        [{"searches": ["q1"], "stop": "tool_use"}, {"texts": ["done"]}], mcp, native_trace=True
    )
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": body["content"]},
        {"role": "user", "content": "sources?"},
    ]
    rewritten = wsl._rewrite_inbound_native_blocks({"messages": history})["messages"]
    roles = [m["role"] for m in rewritten]
    assert roles == ["user", "assistant", "user", "assistant", "user"], roles
    tr = rewritten[2]["content"][0]
    assert tr["type"] == "tool_result" and json.loads(tr["content"])["results"][0]["title"] == "T0"
    assert PREFIX not in json.dumps(rewritten, ensure_ascii=False)
