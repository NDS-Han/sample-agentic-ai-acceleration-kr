"""본 구현(1.0.72): 되돌아온 native 검색 블록을 **진짜 도구 기록**(tool_use/tool_result)으로 재작성.

탐침(1.0.70/71) 결과 — Cowork 는 server_tool_use / web_search_tool_result 블록을 이력에 그대로 실어
되돌려 준다(2026-09-17 07:57 inbound 2/2·4/4). 탐침은 그것을 결과 없는 🔎 텍스트 한 줄로 환원했고,
그 요청의 답은 "원문 접근이 제한적" 이라며 흐려졌다(모델이 본 이력: 흔적 4줄, 결과 0).

계약(루프에 들어오는 Anthropic 요청):
- assistant [text0, server_tool_use, web_search_tool_result, text1]
  → assistant [text0, tool_use(web_search)] / user [tool_result] / assistant [text1]
  (검색 → 결과 → 계속 의 원래 턴 구조를 그대로 복원. 연속 검색은 한 assistant 턴에 병렬 tool_use.)
- tool_result 본문 = 블록에 실려 온 digest(제목·URL·날짜 + encrypted_content 의 발췌)를 provider
  JSON 과 같은 키(title/url/text/publishedDate)로. 오류 블록은 is_error.
- id 는 srvtoolu_X → toolu_gw_X (tool_use/tool_result 쌍이 같은 id).
- 루프 안(우리 도구가 주입되는 턴)에만 재작성한다. 우리 도구 없이 나가는 경로 — F-7 패스스루,
  MCP 초기화 실패 폴백, 라우터의 폴백 루프·Mantle·count_tokens — 는 종전대로 텍스트 흔적으로 환원
  (도구 정의 없는 tool_use 는 Bedrock 400).
- 블록이 없으면 같은 객체(바이트 동일 경로).
"""

from __future__ import annotations

import base64
import json

import pytest

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage
from app.services.agentcore_mcp_client import AgentCoreMcpError

GW = wsl.GW_WEB_SEARCH_NAME
PREFIX = wsl._TRACE_PREFIX


def _enc(snippet: str) -> str:
    return base64.b64encode(json.dumps({"gw": 1, "snippet": snippet}).encode()).decode()


def _item(title, url, snippet=None, page_age=None, enc=None):
    return {
        "type": "web_search_result",
        "title": title,
        "url": url,
        "encrypted_content": enc if enc is not None else (_enc(snippet) if snippet else "e30="),
        "page_age": page_age,
    }


def _stu(i, q):
    return {"type": "server_tool_use", "id": f"srvtoolu_{i}", "name": GW, "input": {"query": q}}


def _res(i, items=None, error=None):
    content = (
        {"type": "web_search_tool_result_error", "error_code": error} if error else (items or [])
    )
    return {"type": "web_search_tool_result", "tool_use_id": f"srvtoolu_{i}", "content": content}


def _text(t):
    return {"type": "text", "text": t}


def _roles(msgs):
    return [m["role"] for m in msgs]


def _kinds(m):
    return [b["type"] for b in m["content"]]


# ── 재작성 형태 ──────────────────────────────────────────────────────────────────
def test_rewrite_restores_search_result_continue_turn_structure():
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    _text("looking"),
                    _stu(1, "q1"),
                    _res(
                        1,
                        [
                            _item("A", "https://a.com/x", "A snippet", "2026-09-01"),
                            _item("B", "https://b.com/y"),
                        ],
                    ),
                    _text("answer"),
                ],
            },
            {"role": "user", "content": "more"},
        ],
        "model": "m",
    }
    out = wsl._rewrite_inbound_native_blocks(body)
    assert out is not body and body["messages"][1]["content"][1]["type"] == "server_tool_use", (
        "원본 불변"
    )
    msgs = out["messages"]
    assert _roles(msgs) == ["user", "assistant", "user", "assistant", "user"]
    assert _kinds(msgs[1]) == ["text", "tool_use"] and _kinds(msgs[2]) == ["tool_result"]
    assert _kinds(msgs[3]) == ["text"] and msgs[3]["content"][0]["text"] == "answer"
    tu = msgs[1]["content"][1]
    assert tu == {"type": "tool_use", "id": "toolu_gw_1", "name": GW, "input": {"query": "q1"}}
    tr = msgs[2]["content"][0]
    assert (
        tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_gw_1" and "is_error" not in tr
    )
    results = json.loads(tr["content"])["results"]
    assert results[0] == {
        "title": "A",
        "url": "https://a.com/x",
        "text": "A snippet",
        "publishedDate": "2026-09-01",
    }
    assert results[1] == {"title": "B", "url": "https://b.com/y", "text": ""}, "발췌 없으면 빈 본문"
    assert msgs[0] is body["messages"][0] and msgs[4] is body["messages"][2]
    assert out["model"] == "m"
    assert "server_tool_use" not in json.dumps(out) and PREFIX not in json.dumps(out)


def test_rewrite_parallel_searches_share_one_turn_and_interleaved_text_splits_turns():
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    _stu(1, "q1"),
                    _res(1, [_item("A", "https://a.com")]),
                    _stu(2, "q2"),
                    _res(2, [_item("B", "https://b.com")]),
                    _text("t1"),
                    _stu(3, "q3"),
                    _res(3, [_item("C", "https://c.com")]),
                    _text("t2"),
                ],
            },
            {"role": "user", "content": "more"},
        ]
    }
    msgs = wsl._rewrite_inbound_native_blocks(body)["messages"]
    assert _roles(msgs) == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert _kinds(msgs[1]) == ["tool_use", "tool_use"] and _kinds(msgs[2]) == [
        "tool_result",
        "tool_result",
    ]
    assert [b["id"] for b in msgs[1]["content"]] == ["toolu_gw_1", "toolu_gw_2"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["toolu_gw_1", "toolu_gw_2"]
    assert _kinds(msgs[3]) == ["text", "tool_use"] and msgs[3]["content"][0]["text"] == "t1"
    assert (
        _kinds(msgs[4]) == ["tool_result"] and msgs[4]["content"][0]["tool_use_id"] == "toolu_gw_3"
    )
    assert _kinds(msgs[5]) == ["text"] and msgs[5]["content"][0]["text"] == "t2"


def test_rewrite_keeps_client_tool_call_after_search_and_handles_errors():
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    _text("t0"),
                    _stu(1, "q1"),
                    _res(1, error="max_uses_exceeded"),
                    _stu(2, "q2"),  # result block missing entirely
                    {
                        "type": "tool_use",
                        "id": "ct_1",
                        "name": "Read",
                        "input": {"file_path": "/x"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "ct_1", "content": "ok"}],
            },
        ]
    }
    msgs = wsl._rewrite_inbound_native_blocks(body)["messages"]
    assert _roles(msgs) == ["user", "assistant", "user", "assistant", "user"]
    assert _kinds(msgs[1]) == ["text", "tool_use", "tool_use"]
    trs = msgs[2]["content"]
    assert [t["tool_use_id"] for t in trs] == ["toolu_gw_1", "toolu_gw_2"]
    assert trs[0]["is_error"] is True and "max_uses_exceeded" in trs[0]["content"]
    assert trs[1]["is_error"] is True and "missing" in trs[1]["content"]
    assert _kinds(msgs[3]) == ["tool_use"] and msgs[3]["content"][0]["name"] == "Read"
    assert msgs[4] is body["messages"][2], "클라이언트 도구의 tool_result 는 그대로 그 뒤에"


def test_rewrite_tolerates_bad_digest_and_no_blocks():
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    _stu(1, "q1"),
                    _res(
                        1,
                        [
                            _item("A", "https://a.com", enc="not-base64!!"),
                            _item("B", "https://b.com", enc=base64.b64encode(b"[1,2]").decode()),
                            {"type": "web_search_result", "title": "C", "url": "https://c.com"},
                        ],
                    ),
                    _text("x"),
                ],
            },
        ]
    }
    msgs = wsl._rewrite_inbound_native_blocks(body)["messages"]
    results = json.loads(msgs[2]["content"][0]["content"])["results"]
    assert [r["title"] for r in results] == ["A", "B", "C"] and all(
        r["text"] == "" for r in results
    )
    plain = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [_text("x")]},
        ]
    }
    assert wsl._rewrite_inbound_native_blocks(plain) is plain
    assert wsl._rewrite_inbound_native_blocks({"messages": "nope"}) is not None


def test_rewrite_drops_display_trace_lines_only_next_to_native_blocks():
    """native 모드가 사용자에게 보여 준 🔎 줄은 모델 이력에서 빠진다(도구 기록이 대신 있다).
    native 블록이 없는 메시지(text 모드 클라이언트)의 🔎 줄은 그대로 — 거기선 유일한 흔적."""
    shown = _text(f'{PREFIX} "q1" — 1 result (a.com)\n')
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    _text("t0"),
                    _stu(1, "q1"),
                    _res(1, [_item("A", "https://a.com")]),
                    shown,
                    _text(f'{PREFIX} "q1" — 1 result (a.com)\n\nanswer'),
                ],
            },
            {"role": "user", "content": "more"},
            {
                "role": "assistant",
                "content": [_text(f'{PREFIX} "q9" — 2 results (z.com)\n'), _text("x")],
            },
        ]
    }
    msgs = wsl._rewrite_inbound_native_blocks(body)["messages"]
    assert _roles(msgs) == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert _kinds(msgs[3]) == ["text"] and msgs[3]["content"][0]["text"].strip() == "answer"
    assert PREFIX not in json.dumps(msgs[:5])
    assert msgs[5] is body["messages"][3], "native 블록 없는 메시지는 손대지 않는다"
    # a trailing display-only block after the search leaves no dangling assistant message
    body2 = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    _text("t0"),
                    _stu(1, "q1"),
                    _res(1, [_item("A", "https://a.com")]),
                    shown,
                ],
            }
        ]
    }
    msgs2 = wsl._rewrite_inbound_native_blocks(body2)["messages"]
    assert _roles(msgs2) == ["assistant", "user"] and _kinds(msgs2[0]) == ["text", "tool_use"]


def test_rewrite_result_only_message_never_becomes_empty():
    body = {
        "messages": [{"role": "assistant", "content": [_res(9, [_item("A", "https://a.com")])]}]
    }
    msgs = wsl._rewrite_inbound_native_blocks(body)["messages"]
    assert (
        msgs[0]["role"] == "assistant"
        and msgs[0]["content"]
        and msgs[0]["content"][0]["type"] == "text"
    )


# ── 루프 진입: 루프 안은 재작성, 우리 도구 없는 패스스루는 텍스트 환원 ───────────
class _Req:
    def __init__(self):
        self.headers = {}
        self.scope = {"state": {"client": "cowork"}}

    async def is_disconnected(self):
        return False


class _Mcp:
    def __init__(self, init_fail: bool = False):
        self.init_fail = init_fail
        self.calls: list[str] = []

    async def ensure_initialized(self):
        if self.init_fail:
            raise AgentCoreMcpError("discovery down")
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls.append(query)
        items = [{"url": "https://z.com", "title": "Z", "text": "Z body"}]

        class _R:
            raw_text = json.dumps({"results": items})
            results = items

        return _R()


def _echoed() -> list[dict]:
    return [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": [
                _text("looking"),
                _stu(1, "q1"),
                _res(1, [_item("A", "https://a.com/x", "A snippet")]),
                _text(f'{PREFIX} "q1" — 1 result (a.com)\n'),
                _text("answer"),
            ],
        },
        {"role": "user", "content": "really?"},
    ]


@pytest.mark.asyncio
async def test_loop_rewrites_echoed_blocks_into_tool_history(monkeypatch):
    monkeypatch.setattr(wsl, "_trace_mode", lambda: ("native", frozenset({"cowork"})))
    captured: list[dict] = []
    queue = [
        {
            "content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q3"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 5},
        },
    ]

    async def invoke(body):
        captured.append(body)
        return (
            200,
            json.dumps(queue.pop(0)).encode(),
            {},
            TokenUsage(input_tokens=10, output_tokens=5),
        )

    async def invoke_stream(_body):
        raise AssertionError("non-stream path expected")

    async def on_usage(_u):
        pass

    mcp = _Mcp()
    resp = await wsl.run_web_search_loop(
        dialect="anthropic",
        invoke=invoke,
        invoke_stream=invoke_stream,
        initial_req_data={"messages": _echoed()},
        is_stream=False,
        mcp_client=mcp,
        request=_Req(),
        on_usage=on_usage,
    )
    first = captured[0]
    assert "server_tool_use" not in json.dumps(first) and PREFIX not in json.dumps(first)
    assert _roles(first["messages"]) == ["user", "assistant", "user", "assistant", "user"]
    assert _kinds(first["messages"][1]) == ["text", "tool_use"]
    assert first["messages"][1]["content"][1]["name"] == GW
    assert (
        json.loads(first["messages"][2]["content"][0]["content"])["results"][0]["text"]
        == "A snippet"
    )
    assert any(t.get("name") == GW and "input_schema" in t for t in first["tools"]), (
        "우리 도구가 함께 간다"
    )
    # the final (tool_choice none) turn keeps the rewritten history + tools (no strip needed)
    assert _roles(captured[-1]["messages"])[:5] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    body = json.loads(bytes(resp.body))
    assert [b["type"] for b in body["content"]][:2] == ["server_tool_use", "web_search_tool_result"]
    assert mcp.calls == ["q3"]


@pytest.mark.asyncio
async def test_mcp_init_failure_passthrough_uses_text_normalization(monkeypatch):
    monkeypatch.setattr(wsl, "_trace_mode", lambda: ("native", frozenset({"cowork"})))
    captured: list[dict] = []

    async def invoke(body):
        captured.append(body)
        return (
            200,
            json.dumps(
                {
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode(),
            {},
            TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def invoke_stream(_body):
        raise AssertionError("non-stream path expected")

    async def on_usage(_u):
        pass

    await wsl.run_web_search_loop(
        dialect="anthropic",
        invoke=invoke,
        invoke_stream=invoke_stream,
        initial_req_data={"messages": _echoed()},
        is_stream=False,
        mcp_client=_Mcp(init_fail=True),
        request=_Req(),
        on_usage=on_usage,
    )
    sent = captured[0]
    assert "server_tool_use" not in json.dumps(sent)
    assert not any(
        b.get("type") == "tool_use"
        for m in sent["messages"]
        if isinstance(m["content"], list)
        for b in m["content"]
    ), "도구 정의 없이 나가는 패스스루에 tool_use 가 있으면 400"
    assert sent["messages"][1]["content"][1]["text"].startswith(f'{PREFIX} "q1"')
