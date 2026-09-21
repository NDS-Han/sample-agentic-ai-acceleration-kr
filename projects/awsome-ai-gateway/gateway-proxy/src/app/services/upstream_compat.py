"""Bridge what an Anthropic-format client sends and what a Bedrock upstream accepts.

Behind ``ANTHROPIC_BASE_URL`` Claude Code speaks the full Anthropic Messages format, "even if
your gateway forwards to an Amazon Bedrock ... upstream. Bridging that difference is your
gateway's job" (Claude Code gateway compatibility guide). Bedrock rejects a request outright
when ``tools`` carries a server tool type it does not serve.

2026-09-18 (US): a Claude Code session with the advisor feature on put
``{"type": "advisor_20260301", ...}`` into every request; every request of that user failed
with ``400 ... tool type 'advisor_20260301' is not supported for this model`` — "hi" included.
Claude Code does not strip the tool and retry on that error wording, and ``/advisor off`` did
not help in the affected session.

Such tools are executed by Anthropic's API, not by the client and not by Bedrock. Removing one
turns the feature off quietly — the documented outcome "when both halves are absent together" —
instead of failing the whole session. The type prefixes come from settings
(``BEDROCK_UNSUPPORTED_TOOL_TYPE_PREFIXES``), so the next Anthropic-only tool type is a config
change, not a release.

Not handled here: the native ``web_search_*`` tool (the web-search loop replaces it with the
gateway's own search) and replayed web-search blocks (``web_search_loop``).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Shown in place of an assistant message that held nothing but blocks of a removed tool
#: (an empty content list is a 400).
_REMOVED_NOTE = "[a server tool result that this endpoint cannot replay was removed]"


def unsupported_tool_prefixes(raw: str | None) -> tuple[str, ...]:
    """``"advisor_, web_fetch_"`` → ``("advisor_", "web_fetch_")``; blank → ``()`` (off)."""
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


def _family(tool_type: str) -> str:
    """``advisor_20260301`` → ``advisor`` — the prefix of that tool's result block types
    (``advisor_tool_result``), following the API's ``<family>_tool_result`` naming."""
    head, _, tail = tool_type.rpartition("_")
    return head if head and tail.isdigit() else tool_type


def strip_unsupported_server_tools(body: Any, prefixes: tuple[str, ...]) -> tuple[Any, list[str]]:
    """Remove tools whose ``type`` starts with one of ``prefixes``.

    Returns ``(body, removed_types)``. The SAME object comes back when nothing matched, so
    requests without such a tool take a byte-identical path; otherwise a shallow copy with new
    ``tools`` / ``tool_choice`` / ``messages`` — the caller's object is never mutated.

    Also removed, because they are equally unknown to the upstream: history blocks produced by
    a removed tool — ``server_tool_use`` naming it and ``<family>_tool_result`` blocks. A
    ``tool_choice`` that names a removed tool becomes ``auto``; with no tool left, ``tools``
    and ``tool_choice`` are dropped (a tool choice without tools is rejected).
    """
    if not prefixes or not isinstance(body, dict):
        return body, []
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body, []
    gone = [t for t in tools
            if isinstance(t, dict) and isinstance(t.get("type"), str)
            and t["type"].startswith(prefixes)]
    if not gone:
        return body, []

    out = dict(body)
    kept = [t for t in tools if not any(t is g for g in gone)]
    names = {g.get("name") for g in gone if g.get("name")}
    result_types = {f"{_family(g['type'])}_tool_result" for g in gone}
    if kept:
        out["tools"] = kept
        tc = out.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name") in names:
            out["tool_choice"] = {"type": "auto"}
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    msgs = body.get("messages")
    if isinstance(msgs, list):
        new_msgs: list = []
        changed = False
        for m in msgs:
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, list):
                new_msgs.append(m)
                continue
            keep = [b for b in content if not (
                isinstance(b, dict) and (
                    (b.get("type") == "server_tool_use" and b.get("name") in names)
                    or b.get("type") in result_types))]
            if len(keep) == len(content):
                new_msgs.append(m)
                continue
            changed = True
            new_msgs.append({**m, "content": keep or [{"type": "text", "text": _REMOVED_NOTE}]})
        if changed:
            out["messages"] = new_msgs

    removed = [g["type"] for g in gone]
    logger.info("upstream_compat.unsupported_server_tool_stripped", tool_types=removed,
                tools_left=len(kept))
    return out, removed
