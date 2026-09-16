"""검색 흔적 한 줄이 클라이언트 응답에 남아야 한다 — "검색한 적 없다" 자백 루프 방지.

2026-09-16 US Cowork 실측. 우리 web_search tool_use/tool_result 는 클라이언트가 선언하지 않은
도구라 응답에서 걷어낸다(안 걷어내면 다음 턴 400). 그런데 Cowork 가 한 턴 안에서 자기 도구
(TaskCreate·bash)를 쓰면 게이트웨이 요청이 여러 번 나가고, 그 사이 이력에서 검색 증거가
사라진다. 모델은 자기 마지막 턴에 "검색하겠습니다 … 확보했습니다" 텍스트만 있고 도구 호출이
없는 것을 보고 "검색한 적 없다, 지어냈다" 고 판단해 검색을 되풀이했다 — 질문 하나에 요청 8건·
검색 21회·$3.3, 최종 답변은 자백문.

계약(Anthropic 방언 두 경로):
- 검색 라운드마다 텍스트 블록 하나에 검색당 한 줄:
  ``🔎 [gateway web_search] "<query>" → <n> results: <hosts>``
  (실패 → ``failed …``, 상한/마감으로 건너뜀 → ``skipped (…)``).
- 결과 본문은 싣지 않는다(≤12k자 — 컨텍스트 부담 없음). 내부 대화(Bedrock 턴)에는 넣지 않는다.
- 스트리밍: 블록이 열리고 닫힌다(SDK 파서). 비스트리밍: 본문 content 맨 앞.
Responses(Codex) 경로는 아직 없음 — server_tool_use 방식과 함께 upstream PR 후보.
"""

from __future__ import annotations

import asyncio
import json

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage

GW = wsl.GW_WEB_SEARCH_NAME
PREFIX = wsl._TRACE_PREFIX


class _Mcp:
    def __init__(self, raw: str = '{"results":[{"t":"WEBTEXT"}]}', results=None,
                 fail: bool = False):
        self.raw = raw
        self.results = results
        self.fail = fail
        self.calls: list[str] = []

    async def ensure_initialized(self) -> str:
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls.append(query)
        if self.fail:
            from app.services.agentcore_mcp_client import AgentCoreMcpError
            raise AgentCoreMcpError("connector down")

        class _R:
            raw_text = self.raw

        if self.results is not None:
            _R.results = self.results
        return _R()


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


def _parse(out: list[str]) -> list[tuple[str, dict]]:
    got = []
    for chunk in out:
        head, _, rest = chunk.partition("\n")
        got.append((head.replace("event: ", ""), json.loads(rest.partition("data: ")[2])))
    return got


def _deadline() -> float:
    return asyncio.get_event_loop().time() + 999


def _search_turn(queries: list[str]) -> list[bytes]:
    ev = [_raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})]
    for i, q in enumerate(queries):
        ev += [
            _raw({"type": "content_block_start", "index": i,
                  "content_block": {"type": "tool_use", "id": f"tu_{i}", "name": GW, "input": {}}}),
            _raw({"type": "content_block_delta", "index": i,
                  "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": q})}}),
            _raw({"type": "content_block_stop", "index": i}),
        ]
    ev += [_raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                 "usage": {"output_tokens": 5}}),
           _raw({"type": "message_stop"})]
    return ev


def _final(text: str = "answer") -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 50}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": text}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]


async def _run_stream(turns, mcp, **kw):
    queue = list(turns)

    async def invoke_stream(_body):
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    out: list[str] = []
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp, request=None, on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5, **kw,
    ):
        out.append(chunk.decode())
    return out


def _texts(events) -> list[str]:
    """Client-visible text blocks in order (one string per block)."""
    blocks: dict[int, str] = {}
    order: list[int] = []
    for e, d in events:
        if e == "content_block_start" and d["content_block"]["type"] == "text":
            blocks[d["index"]] = ""
            order.append(d["index"])
        elif e == "content_block_delta" and d["delta"].get("type") == "text_delta":
            blocks[d["index"]] += d["delta"]["text"]
    return [blocks[i] for i in order]


# ─── 스트리밍 ───────────────────────────────────────────────────────────────


async def test_stream_leaves_one_trace_line_per_search_before_the_answer():
    mcp = _Mcp(results=[
        {"url": "https://www.reuters.com/a", "title": "x"},
        {"url": "https://nvidianews.nvidia.com/b", "title": "y"},
        {"url": "https://reuters.com/c", "title": "z"},
    ])
    out = await _run_stream([_search_turn(["NVIDIA Q2 FY2027 results"]), _final("answer")], mcp)
    events = _parse(out)
    texts = _texts(events)
    assert len(texts) == 2, f"텍스트 블록이 [흔적, 답변] 둘이어야 한다: {texts}"
    trace, answer = texts
    expected = (f'{PREFIX} "NVIDIA Q2 FY2027 results" → 3 results: '
                "reuters.com, nvidianews.nvidia.com")
    assert trace.strip() == expected, trace
    assert answer == "answer"
    # 블록은 열리고 닫힌다 — SDK 파서가 열린 채 끝나면 오류로 본다
    starts = [d["index"] for e, d in events if e == "content_block_start"]
    stops = [d["index"] for e, d in events if e == "content_block_stop"]
    assert sorted(starts) == sorted(stops) == [0, 1], (starts, stops)
    # 우리 tool_use 는 여전히 클라이언트에 나가지 않는다
    assert not any(e == "content_block_start" and d["content_block"]["type"] == "tool_use"
                   for e, d in events)
    # 결과 본문(WEBTEXT)은 싣지 않는다
    assert "WEBTEXT" not in "".join(texts)


async def test_stream_trace_marks_capped_and_failed_searches():
    mcp = _Mcp(fail=True)
    out = await _run_stream([_search_turn(["q1", "q2"]), _final()], mcp, max_searches_per_turn=1)
    trace = _texts(_parse(out))[0]
    lines = [ln for ln in trace.splitlines() if ln.strip()]
    assert len(lines) == 2, lines
    assert lines[0].startswith(f'{PREFIX} "q1" → failed'), lines[0]
    assert lines[1] == f'{PREFIX} "q2" → skipped (per-turn limit)', lines[1]
    assert mcp.calls == ["q1"], "상한을 넘긴 검색은 실행되지 않아야 한다"


async def test_stream_without_search_has_no_trace():
    out = await _run_stream([_final("plain")], _Mcp())
    assert _texts(_parse(out)) == ["plain"]


# ─── 비스트리밍 ─────────────────────────────────────────────────────────────


async def _run_nonstream(turns, mcp):
    queue = list(turns)

    async def invoke(_body):
        payload = queue.pop(0)
        return 200, json.dumps(payload).encode(), {}, TokenUsage(input_tokens=10, output_tokens=5)

    async def on_usage(_u):
        pass

    resp = await wsl._anthropic_nonstream(
        invoke=invoke, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp, on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5,
    )
    return json.loads(bytes(resp.body))


async def test_nonstream_body_starts_with_the_trace_block():
    mcp = _Mcp(results=[{"url": "https://ir.amd.com/q2", "title": "t"}])
    turn1 = {"stop_reason": "tool_use",
             "content": [{"type": "tool_use", "id": "tu_1", "name": GW,
                          "input": {"query": "AMD Q2 2026"}}],
             "usage": {"input_tokens": 10, "output_tokens": 5}}
    turn2 = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}],
             "usage": {"input_tokens": 40, "output_tokens": 3}}
    body = await _run_nonstream([turn1, turn2], mcp)
    kinds = [b["type"] for b in body["content"]]
    assert kinds == ["text", "text"], kinds
    assert body["content"][0]["text"].strip() == f'{PREFIX} "AMD Q2 2026" → 1 results: ir.amd.com'
    assert body["content"][1]["text"] == "answer"
    assert body["stop_reason"] == "end_turn"


async def test_nonstream_without_search_is_untouched():
    body = await _run_nonstream([{"stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "plain"}],
                                  "usage": {"input_tokens": 10, "output_tokens": 5}}], _Mcp())
    assert [b["text"] for b in body["content"]] == ["plain"]


# ─── 흔적 문자열 ────────────────────────────────────────────────────────────


def test_trace_line_truncates_long_queries_and_hosts_are_deduped():
    line = wsl._trace_line("x" * 120, "1 results")
    assert line.startswith(f'{PREFIX} "') and "…" in line and len(line) < 130
    n, hosts = wsl._result_hosts(type("R", (), {"results": [
        {"url": "https://www.a.com/1"}, {"url": "https://a.com/2"}, {"url": "https://b.com"},
        {"url": "https://c.com"}, {"url": "https://d.com"}]})())
    assert (n, hosts) == (5, ["a.com", "b.com", "c.com"])
    # results 속성이 없는 가짜(raw_text 만) 도 세어진다
    n2, hosts2 = wsl._result_hosts(type("R", (), {"raw_text": '{"results":[{"t":1},{"t":2}]}'})())
    assert (n2, hosts2) == (2, [])
