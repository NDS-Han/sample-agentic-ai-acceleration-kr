"""Anthropic-only server tools must not reach Bedrock (2026-09-18, US: Claude Code advisor).

A Claude Code session with the advisor feature on sends ``{"type": "advisor_20260301", ...}``
in ``tools`` on EVERY request. Behind ``ANTHROPIC_BASE_URL`` Claude Code speaks the full
Anthropic Messages format; Bedrock rejected the tool type and every request of that user —
even "hi" — failed with ``400 ... tool type 'advisor_20260301' is not supported for this
model``. ``/advisor off`` on the client did not help in that session, and Claude Code does not
retry without the tool on this error wording.

Claude Code's gateway guide: "When the client speaks the Anthropic Messages format, Claude
Code sends the full set, even if your gateway forwards to an Amazon Bedrock ... upstream.
Bridging that difference is your gateway's job."

Contract:
- tools whose ``type`` starts with a configured prefix (default ``advisor_``) are removed
  before anything is sent upstream; every other tool — including tools WITHOUT a ``type`` and
  client tools with a type Bedrock knows (bash_*, text_editor_*) — is left untouched;
- a ``tool_choice`` naming a removed tool is relaxed to auto; with no tool left, ``tools`` and
  ``tool_choice`` are removed (a tool choice without tools is a 400);
- history blocks of a removed tool (``server_tool_use`` of that name, ``*_tool_result`` of
  that family) are removed as well, never leaving an empty message;
- the prefixes come from settings, so the next Anthropic-only tool type is a config change,
  not a release;
- without such a tool the SAME object is returned (byte-identical path for everyone else).
"""

from __future__ import annotations

import copy

import pytest

from app.services.upstream_compat import strip_unsupported_server_tools

ADVISOR = {"type": "advisor_20260301", "name": "advisor", "model": "claude-fable-5-1"}
BASH = {"type": "bash_20250124", "name": "bash"}
READ = {"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}


def _body(**kw):
    return {"model": "claude-opus-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}], **kw}


def test_advisor_tool_is_removed_and_the_rest_is_untouched():
    body = _body(tools=[READ, ADVISOR, BASH])
    before = copy.deepcopy(body)
    out, removed = strip_unsupported_server_tools(body, ("advisor_",))
    assert removed == ["advisor_20260301"]
    assert out["tools"] == [READ, BASH]
    assert body == before, "the caller's object must not be mutated"
    assert {k: v for k, v in out.items() if k != "tools"} == \
           {k: v for k, v in before.items() if k != "tools"}


def test_same_object_when_there_is_nothing_to_remove():
    for body in (_body(), _body(tools=[READ, BASH]), _body(tools=[]), {"messages": "x"}):
        out, removed = strip_unsupported_server_tools(body, ("advisor_",))
        assert out is body and removed == []


@pytest.mark.parametrize(
    "bad", [None, [], "tools", 3, {"tools": "nope"}, {"tools": [1, "x", None]}])
def test_malformed_input_is_passed_through(bad):
    out, removed = strip_unsupported_server_tools(bad, ("advisor_",))
    assert out is bad and removed == []


def test_only_tool_removed_drops_tools_and_tool_choice():
    body = _body(tools=[ADVISOR], tool_choice={"type": "auto"})
    out, _ = strip_unsupported_server_tools(body, ("advisor_",))
    assert "tools" not in out and "tool_choice" not in out


def test_tool_choice_naming_a_removed_tool_is_relaxed():
    body = _body(tools=[READ, ADVISOR], tool_choice={"type": "tool", "name": "advisor"})
    out, _ = strip_unsupported_server_tools(body, ("advisor_",))
    assert out["tool_choice"] == {"type": "auto"}
    kept = _body(tools=[READ, ADVISOR], tool_choice={"type": "tool", "name": "Read"})
    out, _ = strip_unsupported_server_tools(kept, ("advisor_",))
    assert out["tool_choice"] == {"type": "tool", "name": "Read"}


def test_history_blocks_of_the_removed_tool_go_too():
    body = _body(tools=[READ, ADVISOR])
    body["messages"] = [
        {"role": "user", "content": "plan this"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_1",
             "content": {"type": "advisor_result", "text": "do X"}},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        {"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_2", "content": {}}]},
        {"role": "user", "content": "go on"},
    ]
    out, removed = strip_unsupported_server_tools(body, ("advisor_",))
    assert removed == ["advisor_20260301"]
    kinds = [[b["type"] for b in m["content"]] if isinstance(m["content"], list) else "str"
             for m in out["messages"]]
    assert kinds == ["str", ["text", "tool_use"], ["tool_result"], ["text"], "str"]
    assert out["messages"][3]["content"][0]["text"].strip(), "never an empty assistant message"


def test_other_server_tool_blocks_are_not_touched():
    # our own replayed web search blocks are handled elsewhere (web_search_loop) — leave them
    body = _body(tools=[ADVISOR])
    body["messages"] = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [
        {"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_9", "content": []}]}]
    out, _ = strip_unsupported_server_tools(body, ("advisor_",))
    assert [b["type"] for b in out["messages"][1]["content"]] == \
           ["server_tool_use", "web_search_tool_result"]


def test_prefixes_are_configurable_and_empty_disables():
    code_exec = {"type": "code_execution_20250825", "name": "code_execution"}
    body = _body(tools=[READ, ADVISOR, code_exec])
    out, removed = strip_unsupported_server_tools(body, ("advisor_", "code_execution_"))
    assert removed == ["advisor_20260301", "code_execution_20250825"] and out["tools"] == [READ]
    out, removed = strip_unsupported_server_tools(body, ())
    assert out is body and removed == []


def test_settings_default_covers_the_advisor_and_parses_a_list():
    from app.config import Settings
    from app.services.upstream_compat import unsupported_tool_prefixes

    assert "advisor_" in Settings().bedrock_unsupported_tool_type_prefixes
    assert unsupported_tool_prefixes("advisor_, web_fetch_ ,,") == ("advisor_", "web_fetch_")
    assert unsupported_tool_prefixes("") == ()


# ── wiring: both routes strip BEFORE anything is built for the upstream ───────────────────
async def test_count_tokens_route_sends_no_advisor_tool_upstream():
    import json
    from unittest.mock import AsyncMock, MagicMock

    from httpx import ASGITransport, AsyncClient

    from tests.unit.test_count_tokens_router import MODEL_ALIAS, _build_app

    adapter = MagicMock()
    adapter.count_tokens = AsyncMock(return_value=(200, 7))
    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/messages/count_tokens", json={
            "model": MODEL_ALIAS, "tools": [READ, ADVISOR],
            "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    body_arg, _model = adapter.count_tokens.call_args.args
    sent = json.loads(body_arg.decode() if isinstance(body_arg, bytes) else body_arg)
    assert [t.get("name") for t in sent["tools"]] == ["Read"]
    assert "advisor" not in json.dumps(sent)


def test_both_routes_strip_right_after_parsing_the_body():
    """/v1/messages depends on middleware state too heavily for a direct call; check the
    position instead: the strip sits after json.loads and before every consumer of req_data
    (the native-block normaliser, the Bedrock body, and therefore the web-search loop)."""
    import ast
    from pathlib import Path

    router = Path(__file__).resolve().parents[2] / "src" / "app" / "routers" / "messages.py"
    fns = {n.name: n for n in ast.parse(router.read_text()).body
           if isinstance(n, ast.AsyncFunctionDef)}

    def call_line(fn, name):
        return min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", getattr(n.func, "attr", None)) == name)

    def assign_line(fn, target):
        return min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == target for t in n.targets))

    for name in ("messages", "count_tokens"):
        fn = fns[name]
        strip = call_line(fn, "strip_unsupported_server_tools")
        assert call_line(fn, "loads") < strip
        assert strip < call_line(fn, "normalize_inbound_native_blocks")
        assert strip < assign_line(fn, "bedrock_body")
