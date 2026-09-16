"""루프가 넣는 검색 결과에 prompt-cache 표시 하나 — 뒤 턴이 앞 턴 결과를 캐시로 읽게.

2026-09-16 US 실측: 검색 N회 요청은 Bedrock 턴 N+1 번이고 뒤 턴마다 앞 결과(검색당 4~6k 토큰)를
정가로 다시 보냈다(검색 3회 = 결과 토큰 6R 과금). 클라이언트 접두는 클라이언트 표시로 캐시되지만
우리 tool_result 엔 표시가 없었다. 계약:
- 표시는 가장 새 tool_result 하나에만(앞 라운드의 우리 표시는 뗀다).
- 요청당 4개 한도: 넘으면 가장 오래된 **메시지** 표시를 뗀다. system/tools 표시는 건드리지 않고,
  넷이 전부 거기 있으면 우리 표시를 포기한다.
- 클라이언트 원본(base_body) 은 바꾸지 않는다.
- Anthropic 두 경로만(Responses 는 OpenAI 자동 캐시). settings.web_search_cache_results 로 끈다.
"""

from __future__ import annotations

import asyncio
import copy
import json

import app.services.web_search_loop as wsl

GW = wsl.GW_WEB_SEARCH_NAME
CC = {"type": "ephemeral"}


def _tr(tid: str, text: str = "r") -> dict:
    return {"type": "tool_result", "tool_use_id": tid, "content": text}


def test_places_one_breakpoint_on_newest_result_and_moves_it_each_round():
    base = {"system": [{"type": "text", "text": "s", "cache_control": CC}]}
    conv = [{"role": "user", "content": [{"type": "text", "text": "q", "cache_control": CC}]}]
    r1 = [_tr("t1")]
    conv = wsl._place_cache_breakpoint(base, conv, r1, {"t1"})
    assert r1[-1]["cache_control"] == CC
    conv = conv + [{"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": GW, "input": {}}]},
                   {"role": "user", "content": r1}]
    r2 = [_tr("t2a"), _tr("t2b")]
    conv2 = wsl._place_cache_breakpoint(base, conv, r2, {"t1", "t2a", "t2b"})
    assert r2[-1]["cache_control"] == CC and "cache_control" not in r2[0]
    # 앞 라운드의 우리 표시는 떼였고(사본에서), 클라이언트 표시는 그대로
    assert "cache_control" not in conv2[2]["content"][0], conv2[2]
    assert conv2[0]["content"][0]["cache_control"] == CC
    n, _ = wsl._count_cache_breakpoints(base, conv2 + [{"role": "user", "content": r2}])
    assert n == 3  # system 1 + client user 1 + ours 1


def test_respects_the_four_limit_by_evicting_the_oldest_message_breakpoint():
    base = {"system": [{"type": "text", "text": "s", "cache_control": CC},
                       {"type": "text", "text": "s2", "cache_control": CC}]}
    conv = [{"role": "user", "content": [{"type": "text", "text": "q1", "cache_control": CC}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            {"role": "user", "content": [{"type": "text", "text": "q2", "cache_control": CC}]}]
    original = copy.deepcopy(conv)
    r = [_tr("t1")]
    out = wsl._place_cache_breakpoint(base, conv, r, {"t1"})
    assert r[-1]["cache_control"] == CC
    assert "cache_control" not in out[0]["content"][0], "가장 오래된 메시지 표시가 떼여야 한다"
    assert out[2]["content"][0]["cache_control"] == CC, "최근 클라이언트 표시는 유지"
    n, _ = wsl._count_cache_breakpoints(base, out + [{"role": "user", "content": r}])
    assert n == 4
    assert conv == original, "클라이언트 원본 dict 는 바뀌지 않는다(copy-on-write)"


def test_gives_up_when_all_four_live_in_system_or_tools():
    base = {"system": [{"type": "text", "text": "s", "cache_control": CC}] * 2,
            "tools": [{"name": "a", "cache_control": CC}, {"name": "b", "cache_control": CC}]}
    conv = [{"role": "user", "content": "q"}]
    r = [_tr("t1")]
    out = wsl._place_cache_breakpoint(base, conv, r, {"t1"})
    assert "cache_control" not in r[-1] and out == conv


def test_no_results_means_no_change():
    conv = [{"role": "user", "content": "q"}]
    assert wsl._place_cache_breakpoint({}, conv, [], set()) is conv


# ─── 루프 단위: 턴 본문에 실제로 실리는지 ────────────────────────────────────


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


def _cc_positions(body: dict) -> list[tuple[int, str, str]]:
    out = []
    for i, m in enumerate(body["messages"]):
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("cache_control"):
                    out.append((i, m["role"], b.get("type")))
    return out


async def _run(base, cache_results=True):
    turns = [_search_turn(1), _search_turn(2), _final()]
    bodies: list[dict] = []

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
        invoke_stream=invoke_stream, base_body=base, mcp_client=_Mcp(), request=None,
        on_usage=on_usage, max_iterations=5, deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5, cache_results=cache_results,
    ):
        pass
    return bodies


async def test_stream_turn_bodies_carry_exactly_one_of_our_breakpoints_on_the_newest_result():
    base = {"system": [{"type": "text", "text": "s", "cache_control": CC}],
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": "q", "cache_control": CC}]}]}
    bodies = await _run(base)
    assert len(bodies) == 3
    assert _cc_positions(bodies[0]) == [(0, "user", "text")]
    assert _cc_positions(bodies[1]) == [(0, "user", "text"), (2, "user", "tool_result")], bodies[1]
    assert _cc_positions(bodies[2]) == [(0, "user", "text"), (4, "user", "tool_result")], bodies[2]
    assert base["messages"][0]["content"][0]["cache_control"] == CC, "클라이언트 원본 불변"


async def test_stream_flag_off_leaves_bodies_untouched():
    base = {"messages": [{"role": "user", "content": "q"}]}
    bodies = await _run(base, cache_results=False)
    assert all(_cc_positions(b) == [] for b in bodies)
