"""탐침(1.0.70): 검색 흔적을 텍스트 줄 대신 Anthropic **네이티브 블록**으로 남긴다.

2026-09-17 US Cowork 실측(Bedrock invocation log S3 원문으로 확인). 텍스트 흔적(1.0.61~1.0.69)은
두 방향에서 천장에 닿았다:
- 자백: 이력에 실제 흔적 줄 12개 + "genuine evidence" 도구 설명이 있어도, "진짜 검색했냐" 고
  캐물으면 모델이 "성공한 웹 조회 0건" 이라며 앞 답변 세 개를 폐기했다(ws=0, 새 검색 없음).
  모델에게 텍스트 줄은 "내가 쓴 글" 이지 "도구 기록" 이 아니다.
- 모방: 흔적 줄이 클라이언트 도구(TaskUpdate)와 같은 assistant 메시지에 실리자, 모델이
  "흔적 줄 + TaskUpdate" 를 한 동작으로 복사해 검색 없이 흔적 줄만 썼다(한 세션 4회). 필터가
  화면은 지켰지만 모델의 믿음은 못 지켰고, 그 턴은 섞인 턴이라 재촉도 불가능하다.

계약(WEB_SEARCH_TRACE_MODE=native, anthropic-client-platform 이 허용 목록에 있을 때만):
- 검색마다 ``server_tool_use``(name web_search, input {query}) + ``web_search_tool_result``
  (``web_search_result`` 항목: title·url·page_age, 짧은 발췌는 ``encrypted_content`` 에 base64 —
  API 가 "불투명 재전송용" 으로 정의한 자리) 두 블록을 클라이언트 봉투에 넣는다. 🔎 텍스트 줄은
  내지 않는다. 실패/상한은 ``web_search_tool_result_error`` (unavailable / max_uses_exceeded).
- 스트리밍: server_tool_use 는 tool_use 처럼 start(input {}) → input_json_delta → stop, 결과
  블록은 start(전체) → stop. 비스트리밍: content 맨 앞.
- 인바운드(탐침): 클라이언트가 그 블록을 이력에 실어 되돌리면 Bedrock 이 모르는 형태이므로
  텍스트 흔적 줄로 환원하고 개수를 로그한다(400 불가). 되돌아오는 것이 확인되면 본 구현
  (tool_use/tool_result 재작성)으로 간다.
- 기본값 text = 종전과 바이트 단위로 같은 경로.

같은 커밋의 소품: 섞인 턴(클라이언트 도구 + 우리 검색)에서 조용히 버려지던 검색에 "not run"
흔적, 도구 설명의 "회사별로 나눠 검색" 한 줄.
"""

from __future__ import annotations

import asyncio
import base64
import json

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage

GW = wsl.GW_WEB_SEARCH_NAME
PREFIX = wsl._TRACE_PREFIX
RESULTS = [
    {"url": "https://a.com/x", "title": "A title", "text": "A body " * 80,
     "published_date": "2026-09-01"},
    {"url": "https://b.com/y", "title": "B title", "text": "B body"},
]


class _Req:
    """Fake request: ``client`` = what the middleware would have stored; ``platform`` = raw
    header only (no middleware ran), so the header fallback classification is exercised."""

    def __init__(self, platform: str | None = "desktop_app", client: str | None = None):
        self.headers = {"anthropic-client-platform": platform} if platform else {}
        self.scope = {"state": {"client": client}} if client else {}

    async def is_disconnected(self) -> bool:
        return False


class _Mcp:
    def __init__(self, fail: bool = False, results=None):
        self.fail = fail
        self.results = RESULTS if results is None else results
        self.calls: list[str] = []
        self.init_calls = 0

    async def ensure_initialized(self) -> str:
        self.init_calls += 1
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls.append(query)
        if self.fail:
            from app.services.agentcore_mcp_client import AgentCoreMcpError
            raise AgentCoreMcpError("connector down")
        results = self.results

        class _R:
            raw_text = json.dumps({"results": results})

        _R.results = results
        return _R()


def _native_on(monkeypatch, clients: str = "cowork"):
    allowed = frozenset(p for p in clients.split(",") if p)
    monkeypatch.setattr(wsl, "_trace_mode", lambda: ("native", allowed))


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


def _search_turn(queries: list[str], client_tool: str | None = None) -> list[bytes]:
    ev = [_raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})]
    i = 0
    if client_tool:
        ev += [
            _raw({"type": "content_block_start", "index": 0,
                  "content_block": {"type": "tool_use", "id": "ct_0", "name": client_tool,
                                    "input": {}}}),
            _raw({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
            _raw({"type": "content_block_stop", "index": 0}),
        ]
        i = 1
    for q in queries:
        ev += [
            _raw({"type": "content_block_start", "index": i,
                  "content_block": {"type": "tool_use", "id": f"tu_{i}", "name": GW, "input": {}}}),
            _raw({"type": "content_block_delta", "index": i,
                  "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": q})}}),
            _raw({"type": "content_block_stop", "index": i}),
        ]
        i += 1
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
        mcp_client=mcp, request=_Req(), on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5, **kw,
    ):
        out.append(chunk.decode())
    return _parse(out)


def _blocks(events) -> list[dict]:
    """Client-visible content blocks in order, with streamed input/text folded in."""
    blocks: dict[int, dict] = {}
    order: list[int] = []
    for e, d in events:
        if e == "content_block_start":
            blk = dict(d["content_block"])
            blk.setdefault("_json", "")
            blocks[d["index"]] = blk
            order.append(d["index"])
        elif e == "content_block_delta":
            delta = d["delta"]
            if delta.get("type") == "text_delta":
                blocks[d["index"]]["text"] = blocks[d["index"]].get("text", "") + delta["text"]
            elif delta.get("type") == "input_json_delta":
                blocks[d["index"]]["_json"] += delta["partial_json"]
    out = []
    for i in order:
        b = blocks[i]
        if b["_json"]:
            b["input"] = json.loads(b["_json"])
        b.pop("_json", None)
        out.append(b)
    return out


async def _run_nonstream(turns, mcp, capture: list | None = None, **kw):
    queue = list(turns)

    async def invoke(body):
        if capture is not None:
            capture.append(body)
        payload = queue.pop(0)
        return 200, json.dumps(payload).encode(), {}, TokenUsage(input_tokens=10, output_tokens=5)

    async def on_usage(_u):
        pass

    resp = await wsl._anthropic_nonstream(
        invoke=invoke, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp, on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5, **kw,
    )
    return json.loads(bytes(resp.body))


# ── 스트리밍: 블록 형태 ──────────────────────────────────────────────────────────
async def test_native_stream_emits_server_blocks_plus_display_line():
    mcp = _Mcp()
    ev = await _run_stream([_search_turn(["q1", "q2"]), _final("done")], mcp,
                           native_trace=True)
    blocks = _blocks(ev)
    kinds = [b["type"] for b in blocks]
    assert kinds == ["server_tool_use", "web_search_tool_result",
                     "server_tool_use", "web_search_tool_result", "text", "text"], kinds
    use, res = blocks[0], blocks[1]
    assert use["name"] == GW and use["id"].startswith("srvtoolu_")
    assert use["input"] == {"query": "q1"}
    assert res["tool_use_id"] == use["id"]
    items = res["content"]
    assert [i["type"] for i in items] == ["web_search_result", "web_search_result"]
    assert items[0]["title"] == "A title" and items[0]["url"] == "https://a.com/x"
    assert items[0]["page_age"] == "2026-09-01" and items[1]["page_age"] is None
    digest = json.loads(base64.b64decode(items[0]["encrypted_content"]).decode())
    assert digest["snippet"].startswith("A body") and len(digest["snippet"]) <= 600
    assert blocks[2]["input"] == {"query": "q2"}
    assert blocks[1]["tool_use_id"] != blocks[3]["tool_use_id"]
    shown = blocks[4]["text"].strip().splitlines()
    assert len(shown) == 2 and all(ln.startswith(PREFIX) for ln in shown), (
        "표시용 🔎 줄은 블록 뒤에 한 블록으로(Cowork 화면은 블록을 그리지 않는다)")
    assert blocks[-1]["text"] == "done"
    assert mcp.calls == ["q1", "q2"]


async def test_native_stream_frames_are_well_formed_for_sdk_parsers():
    ev = await _run_stream([_search_turn(["q1"]), _final()], _Mcp(), native_trace=True)
    starts = [d["index"] for e, d in ev if e == "content_block_start"]
    stops = [d["index"] for e, d in ev if e == "content_block_stop"]
    assert starts == [0, 1, 2, 3] and stops == [0, 1, 2, 3], (
        "블록 인덱스가 봉투 안에서 연속·1:1 이어야 한다")
    # server_tool_use starts with an EMPTY input and streams it as input_json_delta (like tool_use)
    stu = next(d for e, d in ev if e == "content_block_start"
               and d["content_block"]["type"] == "server_tool_use")
    assert stu["content_block"]["input"] == {}
    delta = next(d for e, d in ev if e == "content_block_delta" and d["index"] == stu["index"])
    assert delta["delta"]["type"] == "input_json_delta"
    assert json.loads(delta["delta"]["partial_json"]) == {"query": "q1"}
    # the result block arrives complete in content_block_start (no deltas)
    res_idx = next(d["index"] for e, d in ev if e == "content_block_start"
                   and d["content_block"]["type"] == "web_search_tool_result")
    assert not any(e == "content_block_delta" and d["index"] == res_idx for e, d in ev)
    assert [e for e, _ in ev][-2:] == ["message_delta", "message_stop"]
    md = next(d for e, d in ev if e == "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"


async def test_native_failed_and_capped_searches_use_error_results():
    ev = await _run_stream([_search_turn(["q1"]), _final()], _Mcp(fail=True), native_trace=True)
    res = next(b for b in _blocks(ev) if b["type"] == "web_search_tool_result")
    assert res["content"] == {"type": "web_search_tool_result_error", "error_code": "unavailable"}

    ev = await _run_stream([_search_turn(["q1", "q2"]), _final()], _Mcp(),
                           native_trace=True, max_searches_per_turn=1)
    results = [b for b in _blocks(ev) if b["type"] == "web_search_tool_result"]
    assert isinstance(results[0]["content"], list)
    assert results[1]["content"] == {"type": "web_search_tool_result_error",
                                     "error_code": "max_uses_exceeded"}


# ── 게이팅 ───────────────────────────────────────────────────────────────────────
def test_native_mode_gated_by_setting_and_classified_client(monkeypatch):
    monkeypatch.setattr(wsl, "_trace_mode", lambda: ("text", frozenset({"cowork"})))
    assert wsl._native_trace_enabled(_Req(client="cowork")) is False, "기본 text = 종전 경로"
    _native_on(monkeypatch, "cowork")
    # 미들웨어가 분류해 둔 값이 기준 — 실제 Cowork 는 UA 로 잡히고 platform 헤더가 없다.
    assert wsl._native_trace_enabled(_Req(None, client="cowork")) is True
    assert wsl._native_trace_enabled(_Req(None, client="claude-code")) is False
    assert wsl._native_trace_enabled(_Req(None, client="other")) is False
    # 미들웨어 없이 직접 부른 경우(테스트·에뮬레이션): 헤더에서 같은 규칙으로 분류
    assert wsl._native_trace_enabled(_Req("desktop_app")) is True
    assert wsl._native_trace_enabled(_Req("cli")) is False, "Claude Code 는 탐침 대상이 아니다"
    assert wsl._native_trace_enabled(_Req(None)) is False
    assert wsl._native_trace_enabled(None) is False
    _native_on(monkeypatch, "")
    assert wsl._native_trace_enabled(_Req(None, client="claude-code")) is True, "빈 목록 = 전부"


def test_request_client_prefers_middleware_state_over_headers():
    assert wsl._request_client(_Req("desktop_app", client="claude-code")) == "claude-code"
    assert wsl._request_client(_Req("desktop_app")) == "cowork"
    ua = type("R", (), {"headers": {
        "user-agent": "claude-cli/2.0.1 (external, claude-desktop-3p)"}})()
    assert wsl._request_client(ua) == "cowork"


def test_trace_mode_reads_settings(monkeypatch):
    from app import config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: type("S", (), {
        "web_search_trace_mode": "NATIVE",
        "web_search_trace_native_clients": "cowork, Claude-Code"})())
    assert wsl._trace_mode() == ("native", frozenset({"cowork", "claude-code"}))


async def test_text_mode_stream_is_unchanged():
    ev = await _run_stream([_search_turn(["q1"]), _final("done")], _Mcp(), native_trace=False)
    kinds = [b["type"] for b in _blocks(ev)]
    assert kinds == ["text", "text"] and PREFIX in _blocks(ev)[0]["text"]


# ── 비스트리밍 ───────────────────────────────────────────────────────────────────
async def test_native_nonstream_puts_blocks_first_in_content():
    turns = [
        {"content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q1"}}],
         "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 20, "output_tokens": 5}},
    ]
    body = await _run_nonstream(turns, _Mcp(), native_trace=True)
    kinds = [b["type"] for b in body["content"]]
    assert kinds == ["server_tool_use", "web_search_tool_result", "text", "text"], kinds
    assert body["content"][0]["input"] == {"query": "q1"}
    assert body["content"][1]["tool_use_id"] == body["content"][0]["id"]
    assert body["content"][2]["text"].startswith(f'{PREFIX} "q1"'), "표시용 🔎 줄"
    assert body["content"][3]["text"] == "done" and body["stop_reason"] == "end_turn"


# ── 인바운드 환원(탐침) ──────────────────────────────────────────────────────────
def _echoed_history() -> list[dict]:
    return [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": GW, "input": {"query": "q1"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "title": "A", "url": "https://a.com/x",
                 "encrypted_content": "e30=", "page_age": None},
                {"type": "web_search_result", "title": "B", "url": "https://www.b.com/y",
                 "encrypted_content": "e30=", "page_age": None}]},
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": GW, "input": {"query": "q2"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2",
             "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
            {"type": "text", "text": "answer"},
        ]},
        {"role": "user", "content": "really?"},
    ]


def test_inbound_native_blocks_become_trace_text():
    body = {"messages": _echoed_history(), "tools": [{"name": "TaskUpdate"}]}
    out = wsl._normalize_inbound_native_blocks(body)
    assert out is not body, "원본 불변"
    assert body["messages"][1]["content"][1]["type"] == "server_tool_use", "원본 불변"
    content = out["messages"][1]["content"]
    assert [b["type"] for b in content] == ["text", "text", "text", "text"]
    assert content[1]["text"] == f'{PREFIX} "q1" — 2 results (a.com, b.com)'
    assert content[2]["text"] == f'{PREFIX} "q2" — failed (unavailable)'
    assert content[0]["text"] == "looking" and content[3]["text"] == "answer"
    assert out["messages"][0] is body["messages"][0] and out["tools"] is body["tools"]
    assert not json.dumps(out).count("server_tool_use")
    plain = {"messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": [{"type": "text", "text": "x"}]}]}
    assert wsl._normalize_inbound_native_blocks(plain) is plain, (
        "블록이 없으면 같은 객체(바이트 동일 경로)")


async def test_loop_entry_rewrites_inbound_blocks_and_gates_native(monkeypatch):
    _native_on(monkeypatch, "cowork")
    mcp = _Mcp()
    captured: list[dict] = []
    queue = [
        {"content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q3"}}],
         "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 20, "output_tokens": 5}},
    ]

    async def invoke(body):
        captured.append(body)
        return (200, json.dumps(queue.pop(0)).encode(), {},
                TokenUsage(input_tokens=10, output_tokens=5))

    async def invoke_stream(_body):
        raise AssertionError("non-stream path expected")

    async def on_usage(_u):
        pass

    resp = await wsl.run_web_search_loop(
        dialect="anthropic", invoke=invoke, invoke_stream=invoke_stream,
        initial_req_data={"messages": _echoed_history()}, is_stream=False,
        mcp_client=mcp, request=_Req(None, client="cowork"), on_usage=on_usage,
    )
    for b in captured:
        dumped = json.dumps(b)
        assert "server_tool_use" not in dumped and "web_search_tool_result" not in dumped
    hist = captured[0]["messages"]
    # 루프 안은 도구 기록으로 재작성(본 구현, test_high_websearch_native_inbound_rewrite 참조)
    assert [b["type"] for b in hist[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["type"] for b in hist[2]["content"]] == ["tool_result", "tool_result"]
    body = json.loads(bytes(resp.body))
    assert [b["type"] for b in body["content"]][:2] == ["server_tool_use", "web_search_tool_result"]
    assert mcp.calls == ["q3"]


# ── 섞인 턴: 버려진 검색에 흔적 ──────────────────────────────────────────────────
async def test_mixed_turn_dropped_search_leaves_not_run_trace_stream():
    mcp = _Mcp()
    ev = await _run_stream([_search_turn(["q1"], client_tool="TaskUpdate")], mcp)
    blocks = _blocks(ev)
    assert mcp.calls == [], "섞인 턴의 검색은 실행하지 않는다(계약 불변)"
    assert [b["type"] for b in blocks] == ["tool_use", "text"]
    assert blocks[0]["name"] == "TaskUpdate"
    assert blocks[1]["text"].strip() == (
        f'{PREFIX} "q1" — not run (called in the same turn as a client tool — '
        'call web_search on its own next turn)')
    md = next(d for e, d in ev if e == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use"
    # native mode: no text line (blocks only) — the dropped search leaves nothing, as before
    ev = await _run_stream([_search_turn(["q1"], client_tool="TaskUpdate")], _Mcp(),
                           native_trace=True)
    assert [b["type"] for b in _blocks(ev)] == ["tool_use"]


async def test_mixed_turn_dropped_search_leaves_not_run_trace_nonstream():
    turns = [{"content": [{"type": "tool_use", "id": "c1", "name": "TaskUpdate", "input": {}},
                          {"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q1"}}],
              "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}]
    mcp = _Mcp()
    body = await _run_nonstream(turns, mcp)
    assert mcp.calls == []
    assert [b["type"] for b in body["content"]] == ["text", "tool_use"]
    assert body["content"][0]["text"].startswith(f'{PREFIX} "q1" — not run (')
    assert body["content"][1]["name"] == "TaskUpdate" and body["stop_reason"] == "tool_use"


def test_trace_words_mixed_korean(monkeypatch):
    monkeypatch.setattr(wsl, "_trace_lang", lambda: "ko")
    assert wsl._trace_words("mixed").startswith("실행 안 됨 (클라이언트 도구")


# ── 소품 ─────────────────────────────────────────────────────────────────────────
def test_description_asks_one_query_per_entity():
    desc = wsl._anthropic_tool_def((3, 2))["description"]
    assert "one query per entity" in desc and "combined query" in desc


def test_result_digest_is_bounded_and_tolerant():
    class _R:
        results = [{"url": f"https://h{i}.com", "title": f"t{i}", "text": "x" * 500}
                   for i in range(8)]

    d = wsl._result_digest(_R())
    assert len(d) == 5 and all(len(x["snippet"]) <= 600 for x in d)
    assert d[0] == {"title": "t0", "url": "https://h0.com", "snippet": "x" * 500, "page_age": None}

    class _Raw:
        results = None
        raw_text = json.dumps([{"url": "https://z.com", "content": "  a  b "}])

    assert wsl._result_digest(_Raw()) == [{"title": "", "url": "https://z.com", "snippet": "a b",
                                           "page_age": None}]
    assert wsl._result_digest(object()) == []


def test_pick_snippet_skips_page_noise_and_cuts_at_sentence():
    noisy = ("705.310.55% 76537.602.1162% 520.570.75% 394.010.3% 80.660.33% September 15, 2026 "
             "2:23 PM 3 min read SK Hynix Is Shipping 16-Layer HBM4 for Nvidia Rubin. Micron and "
             "Samsung trail. " + "More detail follows here. " * 40)
    snip = wsl._pick_snippet(noisy, 600)
    assert not snip.startswith("705") and "SK Hynix Is Shipping" in snip[:60], snip[:80]
    assert len(snip) <= 602 and snip.endswith(" …") and snip.rstrip(" …").endswith(".")
    ko = ("삼성전자, 세계 최초 업계 최고 성능의 HBM4 양산 출하 2026년 02월 12일 "
          "삼성전자가 세계 최초로 HBM4를 양산 출하했다.")
    assert wsl._pick_snippet(ko, 600) == ko, "본문으로 시작하는 글은 그대로"
    assert wsl._pick_snippet("  a   b ", 600) == "a b" and wsl._pick_snippet(None, 600) == ""
    table = "| a | 1 | | b | 2 |"
    assert wsl._pick_snippet(table, 600) == table, "단어 구간이 없으면 통째로"


def test_digest_comes_from_the_trimmed_json_the_model_saw(monkeypatch):
    monkeypatch.setattr(wsl, "_digest_chars", lambda: 50)
    trimmed = json.dumps({"results": [
        {"url": "https://a.com", "title": "A",
         "text": "Real body sentence one. Sentence two is long.", "publishedDate": "2026-09-15"},
        {"url": "https://b.com", "title": "B", "text": "B body"}]})
    d = wsl._result_digest_from_text(trimmed, None, 5, 50)
    assert d[0]["page_age"] == "2026-09-15"
    assert d[0]["snippet"].startswith("Real body sentence one.")
    assert len(d) == 2 and d[1]["page_age"] is None
    raw = type("R", (), {"results": [{"url": "https://z.com", "title": "Z", "text": "Z body"}]})()
    assert wsl._result_digest_from_text("not json", raw, 5, 50)[0]["title"] == "Z"
