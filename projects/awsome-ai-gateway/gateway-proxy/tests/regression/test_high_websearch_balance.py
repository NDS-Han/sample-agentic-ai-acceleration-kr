"""품질 우선 균형 조정(2026-09-16 반도체 14건 실측 뒤) — ②③④⑤.

② 상한 초과 tool_result: "실행 안 됨, 다음 턴에 재요청" (예전 "이미 받은 결과로 답하라" 는 fan-out
   포기를 유발). ③ 도구 설명에 요청당 검색 예산 명시(예산을 모르면 단일 사실에도 상한까지 재확인).
④ Bedrock 스트림 호출의 연결 수준 오류(응답 0바이트) 1회 재시도. ⑤ 마지막 허용 라운드에는 캐시
   표시를 두지 않는다(뒤에 읽는 턴이 없어 쓰기 비용만 냄).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from botocore.exceptions import ConnectionClosedError

import app.services.web_search_loop as wsl
from app.providers.bedrock_adapter import BedrockAdapter

GW = wsl.GW_WEB_SEARCH_NAME


def test_cap_error_says_not_run_and_retry_next_turn():
    msg = json.loads(wsl._cap_error(3))["error"]
    assert msg.startswith("per-turn web search limit reached (3 per turn)")
    assert "NOT run" in msg and "next turn" in msg
    assert "already provided" not in msg, "포기를 유도하던 옛 문구가 남아 있다"
    blk = wsl._anthropic_tool_result("t", wsl._cap_error(3), False, "capped")
    assert blk["is_error"] is True and json.loads(blk["content"])["error"] == msg


def test_tool_description_carries_the_budget_only_when_given():
    d = wsl._anthropic_tool_def((3, 2))["description"]
    assert "up to 3 searches per turn and 2 search turns per request" in d
    assert "re-verifying" in d
    assert "Budget" not in wsl._anthropic_tool_def()["description"]
    assert "Budget" not in wsl._anthropic_tool_def((0, 0))["description"]
    r = wsl._responses_tool_def((2, 1))["description"]
    assert "up to 2 searches per turn and 1 search turn per request" in r
    body = wsl._with_web_search_tool({}, "anthropic", include=True, budget=(3, 2))
    assert "Budget for this request" in body["tools"][-1]["description"]


# ─── ⑤ 마지막 라운드 캐시 표시 생략 (루프 단위) ───────────────────────────────


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


def _cc_tool_results(body):
    return [(i, b.get("tool_use_id")) for i, m in enumerate(body["messages"])
            if isinstance(m.get("content"), list) for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("cache_control")]


async def test_cache_mark_moves_to_the_last_round_and_final_turn_keeps_the_prefix():
    """max_iterations=2: 표시는 항상 가장 새 결과 하나. 턴 3(force_final) 은 도구를 유지하고
    tool_choice none 으로 보내므로 접두가 그대로라 그 표시가 읽힌다(1.0.68)."""
    turns = [_search_turn(1), _search_turn(2), _final()]
    bodies = []

    async def invoke_stream(body):
        bodies.append(json.loads(json.dumps(body)))
        frames = turns.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    async for _ in wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body={"messages": [{"role": "user", "content": "q"}]},
        mcp_client=_Mcp(), request=None, on_usage=on_usage, max_iterations=2,
        deadline=asyncio.get_event_loop().time() + 999, default_max_results=5,
        max_searches_per_turn=3,
    ):
        pass
    assert len(bodies) == 3
    assert _cc_tool_results(bodies[1]) == [(2, "toolu_1")], bodies[1]
    assert _cc_tool_results(bodies[2]) == [(4, "toolu_2")], bodies[2]
    assert bodies[2]["tool_choice"] == {"type": "none"}, "턴 3 은 force_final"
    assert "tools" in bodies[2]
    assert "Budget for this request: up to 3 searches per turn and 2 search turns" in \
        bodies[0]["tools"][-1]["description"]


# ─── ④ 어댑터: 연결 수준 오류 1회 재시도 ───────────────────────────────────


async def test_stream_invoke_retries_once_on_connection_closed():
    client = MagicMock()
    client.invoke_model_with_response_stream.side_effect = [
        ConnectionClosedError(endpoint_url="https://bedrock"),
        {"ResponseMetadata": {"RequestId": "req-2"}, "body": []},
    ]
    adapter = BedrockAdapter(client)
    status, gen, headers, rid = await adapter.invoke_stream(b"{}", "model-id")
    assert status == 200 and rid == "req-2"
    assert client.invoke_model_with_response_stream.call_count == 2
    assert [c async for c in gen] == []


async def test_stream_invoke_gives_up_after_the_second_connection_error():
    client = MagicMock()
    client.invoke_model_with_response_stream.side_effect = ConnectionClosedError(
        endpoint_url="https://bedrock")
    adapter = BedrockAdapter(client)
    status, gen, _h, _rid = await adapter.invoke_stream(b"{}", "model-id")
    assert status == 502 and client.invoke_model_with_response_stream.call_count == 2
    body = b"".join([c async for c in gen])
    assert b"provider_error" in body


# ─── 본문 없는 최종 턴은 한 번만 재촉 (2026-09-17 Cowork 실측: thinking 6k, 텍스트 0) ─────


def _thinking_only_turn():
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "signature_delta", "signature": "sig"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 500}}),
        _raw({"type": "message_stop"}),
    ]


async def _run_turns(turns, max_iterations=1):
    bodies = []

    async def invoke_stream(body):
        bodies.append(json.loads(json.dumps(body)))
        frames = turns.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    out = b""
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body={"messages": [{"role": "user", "content": "q"}]},
        mcp_client=_Mcp(), request=None, on_usage=on_usage, max_iterations=max_iterations,
        deadline=asyncio.get_event_loop().time() + 999, default_max_results=5,
    ):
        out += chunk
    return bodies, out


async def test_stream_empty_final_turn_is_nudged_once_then_answer_flows():
    bodies, out = await _run_turns([_search_turn(1), _thinking_only_turn(), _final()])
    assert len(bodies) == 3, [b.get("tool_choice") for b in bodies]
    nudge_msg = bodies[2]["messages"][-1]
    texts = [b["text"] for b in nudge_msg["content"] if b.get("type") == "text"]
    assert texts[0] == wsl._FINAL_TURN_TEXT_NUDGE and texts[-1] == wsl._FINAL_TURN_ANSWER_NOW
    assert bodies[2]["messages"][-2]["content"][0]["type"] == "thinking", (
        "thinking 턴은 이력에 남긴다")
    assert bodies[2]["tool_choice"] == {"type": "none"}
    assert b"answer" in out and b'"stop_reason": "end_turn"' in out


async def test_stream_empty_final_turn_is_nudged_only_once():
    bodies, out = await _run_turns([_search_turn(1), _thinking_only_turn(), _thinking_only_turn()])
    assert len(bodies) == 3, "재촉은 한 번 — 두 번째도 비면 그대로 끝낸다"
    assert b'"stop_reason": "end_turn"' in out


async def test_nonstream_empty_final_turn_is_nudged_once():
    bodies = []
    turns = [
        {"stop_reason": "tool_use",
         "content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {"query": "q"}}],
         "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"stop_reason": "end_turn",
         "content": [{"type": "thinking", "thinking": "", "signature": "s"}],
         "usage": {"input_tokens": 10, "output_tokens": 500}},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}],
         "usage": {"input_tokens": 10, "output_tokens": 3}},
    ]
    from app.schemas.domain import TokenUsage

    async def invoke(body):
        bodies.append(json.loads(json.dumps(body)))
        return (200, json.dumps(turns.pop(0)).encode(), {},
                TokenUsage(input_tokens=10, output_tokens=5))

    async def on_usage(_u):
        pass

    resp = await wsl._anthropic_nonstream(
        invoke=invoke, base_body={"messages": [{"role": "user", "content": "q"}]},
        mcp_client=_Mcp(), on_usage=on_usage, max_iterations=1,
        deadline=asyncio.get_event_loop().time() + 999, default_max_results=5,
    )
    body = json.loads(bytes(resp.body))
    assert len(bodies) == 3 and [b["type"] for b in body["content"]][-1] == "text"
    assert body["content"][-1]["text"] == "answer"
    assert bodies[2]["messages"][-1]["content"][0]["text"] == wsl._FINAL_TURN_TEXT_NUDGE


def test_tool_description_tells_the_model_history_traces_are_genuine():
    d = wsl._anthropic_tool_def()["description"]
    assert "inserted by the gateway" in d and "genuine evidence" in d and "never 'discard'" in d
    assert "Do not compose such lines yourself" in d
