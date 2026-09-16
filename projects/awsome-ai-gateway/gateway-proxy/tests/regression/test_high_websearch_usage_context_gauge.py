"""클라이언트에 돌려주는 usage 의 입력·캐시 버킷은 **첫 턴 값** — 컨텍스트 게이지 계약.

2026-09-16 US dev 실측. web search 루프는 검색마다 Bedrock 을 한 턴 더 돌고, 종료 프레임
(스트리밍)·본문(비스트리밍)의 usage 에 **N 턴 합계**를 실었다. Claude Code·Cowork·Codex 는
마지막 응답의 input+cache_creation+cache_read 를 "컨텍스트 창 점유" 로 읽고 한계 근처에서
자동 압축하므로, 검색 3회 요청은 실제 26.7k 대화를 106.6k 로 보고했고(4.0×), 55k Cowork
세션은 270k 로 보고돼 200k 창을 넘겨 검색 턴마다 압축이 돌았다("Autocompact is thrashing").

계약:
- 클라이언트 usage 의 입력·캐시 버킷 = **첫 턴** 값(클라이언트가 실제로 보낸 대화).
  뒤 턴들은 거기에 우리 검색 결과(클라이언트는 받지 않는다)를 더해 다시 보낸 것이다.
- 출력 = 전 턴 합계(전부 클라이언트에게 간 답변이다).
- 청구 on_usage(merged) = 전 턴 합계 그대로(Bedrock 은 N 번 과금한다).
- 검색이 없는 1턴 요청은 첫 턴 = 합계라 아무것도 바뀌지 않는다.
네 루프(Anthropic/Responses × 스트리밍/비스트리밍) 모두 같은 계약이다.
"""

from __future__ import annotations

import asyncio
import json

import app.services.web_search_loop as wsl
from app.schemas.domain import TokenUsage

GW = wsl.GW_WEB_SEARCH_NAME


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


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


def _payloads(out: list[str], event: str) -> list[dict]:
    got = []
    for chunk in out:
        head, _, rest = chunk.partition("\n")
        if head.replace("event: ", "") != event:
            continue
        got.append(json.loads(rest.partition("data: ")[2]))
    return got


def _deadline() -> float:
    return asyncio.get_event_loop().time() + 999


# ─── Anthropic 스트리밍 ───────────────────────────────────────────────────────


def _a_usage(inp: int, cw: int = 0, cr: int = 0) -> dict:
    return {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


def _a_search_turn(i: int, inp: int, *, cw: int = 0, cr: int = 0, out: int = 5) -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": _a_usage(inp, cw, cr)}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": f"tu_{i}", "name": GW, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": out}}),
        _raw({"type": "message_stop"}),
    ]


def _a_final(inp: int, *, cw: int = 0, cr: int = 0, out: int = 3) -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": _a_usage(inp, cw, cr)}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": out}}),
        _raw({"type": "message_stop"}),
    ]


async def _run_a_stream(turns):
    queue = list(turns)
    usages: list[TokenUsage] = []

    async def invoke_stream(_body):
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
        mcp_client=_Mcp(), request=None, on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5,
    ):
        out.append(chunk.decode())
    return out, usages


async def test_anthropic_stream_client_usage_is_first_turn_input_and_summed_output():
    """검색 2회 = 3턴. 첫 턴이 캐시를 쓰고(cw 100) 뒤 턴들이 읽는다(cr 100) — 실제 Cowork 패턴."""
    out, usages = await _run_a_stream([
        _a_search_turn(1, 10, cw=100),
        _a_search_turn(2, 40, cr=100),
        _a_final(70, cr=100),
    ])
    usage = _payloads(out, "message_delta")[-1]["usage"]
    assert usage["input_tokens"] == 10, f"입력이 첫 턴 값(10)이 아니다: {usage}"
    assert usage["cache_creation_input_tokens"] == 100, usage
    assert usage["cache_read_input_tokens"] == 0, f"뒤 턴의 캐시 읽기가 섞였다: {usage}"
    assert usage["output_tokens"] == 5 + 5 + 3, usage
    # 게이지 = input + cache 합 = 110 (실제 컨텍스트). 예전 합계 방식이면 120+100+200=420.
    gauge = (usage["input_tokens"] + usage["cache_creation_input_tokens"]
             + usage["cache_read_input_tokens"])
    assert gauge == 110

    # 청구는 전 턴 합계 그대로
    billed = usages[0]
    assert (billed.input_tokens, billed.cache_creation_input_tokens,
            billed.cache_read_input_tokens) == (120, 100, 200)
    assert billed.output_tokens == 13 and billed.total_tokens == 133
    assert billed.web_search_count == 2


async def test_anthropic_stream_single_turn_is_unchanged():
    """검색 없는 요청(1턴): 첫 턴 = 합계 — 이전과 바이트 단위로 같아야 한다."""
    out, usages = await _run_a_stream([_a_final(10, cr=100)])
    usage = _payloads(out, "message_delta")[-1]["usage"]
    assert (usage["input_tokens"], usage["cache_creation_input_tokens"],
            usage["cache_read_input_tokens"], usage["output_tokens"]) == (10, 0, 100, 3)
    assert (usages[0].input_tokens, usages[0].cache_read_input_tokens,
            usages[0].output_tokens) == (10, 100, 3)


# ─── Anthropic 비스트리밍 ─────────────────────────────────────────────────────


async def _run_a_nonstream(turns):
    """turns: [(body, TokenUsage)] — invoke 가 본문과 함께 그 턴의 usage 를 돌려준다."""
    queue = list(turns)
    usages: list[TokenUsage] = []

    async def invoke(_body):
        payload, usage = queue.pop(0)
        return 200, json.dumps(payload).encode(), {}, usage

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(), on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5,
    )
    return json.loads(bytes(resp.body)), usages


async def test_anthropic_nonstream_body_usage_is_first_turn_input_and_summed_output():
    turn1 = ({"stop_reason": "tool_use",
              "content": [{"type": "tool_use", "id": "tu_1", "name": GW, "input": {"query": "q"}}],
              "usage": {**_a_usage(10, cw=100), "output_tokens": 5}},
             TokenUsage(input_tokens=10, cache_creation_input_tokens=100, output_tokens=5))
    turn2 = ({"stop_reason": "end_turn",
              "content": [{"type": "text", "text": "answer"}],
              "usage": {**_a_usage(40, cr=100), "output_tokens": 3}},
             TokenUsage(input_tokens=40, cache_read_input_tokens=100, output_tokens=3))
    body, usages = await _run_a_nonstream([turn1, turn2])
    usage = body["usage"]
    assert usage["input_tokens"] == 10, f"입력이 첫 턴 값이 아니다: {usage}"
    assert usage["cache_creation_input_tokens"] == 100, usage
    assert usage["cache_read_input_tokens"] == 0, (
        f"마지막 턴(Bedrock 본문)의 캐시 읽기가 남았다: {usage}"
    )
    assert usage["output_tokens"] == 8, usage
    billed = usages[0]
    assert (billed.input_tokens, billed.cache_creation_input_tokens, billed.cache_read_input_tokens,
            billed.output_tokens) == (50, 100, 100, 8)


# ─── Responses 스트리밍 ───────────────────────────────────────────────────────


def _r_turn(text: str, *, fn_call: dict | None = None, inp: int = 12, out: int = 6,
            cached: int = 0) -> list[bytes]:
    """Responses 의 input_tokens 는 캐시 포함 총계(spec) — cached 만큼은 cache_read 로 갈린다."""
    events: list[dict] = [{"type": "response.created", "response": {"id": "resp_x"}}]
    oidx = 0
    if text:
        events += [
            {"type": "response.output_item.added", "output_index": oidx,
             "item": {"type": "message", "role": "assistant"}},
            {"type": "response.output_text.delta", "output_index": oidx, "delta": text},
            {"type": "response.output_item.done", "output_index": oidx,
             "item": {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]}},
        ]
        oidx += 1
    if fn_call:
        events += [
            {"type": "response.output_item.added", "output_index": oidx,
             "item": {"type": "function_call", "name": fn_call["name"],
                      "call_id": fn_call["call_id"]}},
            {"type": "response.function_call_arguments.delta", "output_index": oidx,
             "delta": json.dumps(fn_call["input"])},
            {"type": "response.function_call_arguments.done", "output_index": oidx},
            {"type": "response.output_item.done", "output_index": oidx,
             "item": {"type": "function_call", "name": fn_call["name"],
                      "call_id": fn_call["call_id"],
                      "arguments": json.dumps(fn_call["input"])}},
        ]
    events.append({"type": "response.completed", "response": {"id": "resp_x", "usage": {
        "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
        "input_tokens_details": {"cached_tokens": cached}}}})
    return [_raw(e) for e in events]


async def _run_r_stream(turns):
    queue = list(turns)
    usages: list[TokenUsage] = []

    async def invoke_stream(_body):
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(u):
        usages.append(u)

    out: list[str] = []
    async for chunk in wsl._responses_stream(
        invoke_stream=invoke_stream, base_body={"input": "hi"},
        mcp_client=_Mcp(), request=None, on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5,
    ):
        out.append(chunk.decode())
    return out, usages


async def test_responses_stream_terminal_usage_is_first_turn_wire_input_and_summed_output():
    out, usages = await _run_r_stream([
        _r_turn("", fn_call={"name": GW, "call_id": "c1", "input": {"query": "q"}}, inp=12, out=6),
        _r_turn("answer", inp=30, out=6, cached=20),
    ])
    usage = _payloads(out, "response.completed")[-1]["response"]["usage"]
    # wire input = 첫 턴의 캐시 포함 총계(12). 예전 방식이면 12+30=42 (Codex 게이지 3.5배).
    assert usage["input_tokens"] == 12, f"입력이 첫 턴 값이 아니다: {usage}"
    assert usage["input_tokens_details"]["cached_tokens"] == 0, usage
    assert usage["output_tokens"] == 12 and usage["total_tokens"] == 24, usage
    assert usage["input_tokens_details"]["cached_tokens"] <= usage["input_tokens"]
    billed = usages[0]
    assert (billed.input_tokens, billed.cache_read_input_tokens,
            billed.output_tokens) == (22, 20, 12), billed


# ─── Responses 비스트리밍 ─────────────────────────────────────────────────────


async def _run_r_nonstream(turns):
    queue = list(turns)
    usages: list[TokenUsage] = []

    async def invoke(_body):
        payload, usage = queue.pop(0)
        return 200, json.dumps(payload).encode(), {}, usage

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._responses_nonstream(
        invoke=invoke, base_body={"input": "hi"},
        mcp_client=_Mcp(), on_usage=on_usage,
        max_iterations=5, deadline=_deadline(), default_max_results=5,
    )
    return json.loads(bytes(resp.body)), usages


async def test_responses_nonstream_body_usage_is_first_turn_wire_input_and_summed_output():
    turn1 = ({"id": "resp_1", "status": "completed",
              "output": [{"type": "function_call", "call_id": "c1", "name": GW,
                          "arguments": json.dumps({"query": "q"})}],
              "usage": {"input_tokens": 12, "output_tokens": 6,
                        "input_tokens_details": {"cached_tokens": 0}}},
             TokenUsage(input_tokens=12, output_tokens=6))
    turn2 = ({"id": "resp_2", "status": "completed",
              "output": [{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": "answer"}]}],
              "usage": {"input_tokens": 30, "output_tokens": 6,
                        "input_tokens_details": {"cached_tokens": 20}}},
             TokenUsage(input_tokens=10, cache_read_input_tokens=20, output_tokens=6))
    body, usages = await _run_r_nonstream([turn1, turn2])
    usage = body["usage"]
    assert usage["input_tokens"] == 12, f"입력이 첫 턴 값이 아니다: {usage}"
    assert usage["output_tokens"] == 12 and usage["total_tokens"] == 24, usage
    assert usage["input_tokens_details"]["cached_tokens"] == 0, (
        "마지막 턴 본문의 cached_tokens(20)가 남았다 — input(12) 보다 커져 "
        f"불가능한 payload: {usage}"
    )
    billed = usages[0]
    assert (billed.input_tokens, billed.cache_read_input_tokens,
            billed.output_tokens) == (22, 20, 12), billed


# ─── 종료 객체 빌더 단위 ─────────────────────────────────────────────────────


def test_finalize_responses_obj_takes_prompt_buckets_from_first_turn():
    merged = TokenUsage(input_tokens=2904, output_tokens=700, cache_read_input_tokens=12_288,
                        reasoning_tokens=500)
    first = TokenUsage(input_tokens=904, cache_read_input_tokens=4_096)
    obj = wsl._finalize_responses_obj({}, merged, 0, "response.completed", set(), first_turn=first)
    u = obj["usage"]
    assert u["input_tokens"] == 5_000 and u["input_tokens_details"]["cached_tokens"] == 4_096, u
    assert u["output_tokens"] == 700 and u["total_tokens"] == 5_700, u
    assert u["output_tokens_details"]["reasoning_tokens"] == 500
    # first_turn 이 없으면(턴이 하나도 안 끝남) merged 로 폴백 — payload 자기모순 없음
    u2 = wsl._finalize_responses_obj({}, merged, 0, "response.completed", set())["usage"]
    assert u2["input_tokens"] == 2904 + 12_288
