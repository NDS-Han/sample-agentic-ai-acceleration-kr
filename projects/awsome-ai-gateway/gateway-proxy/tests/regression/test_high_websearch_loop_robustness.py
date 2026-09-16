# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""웹서치 루프의 여섯 가지 결함.

이 루프는 N 번의 모델 턴과 N 번의 유료 검색을 **하나의** SSE 응답으로 합친다. 그래서
마지막 단계에서 실패하면 앞서 과금된 모든 것이 버려지고, 종료 프레임을 잘못 만들면
클라이언트와 감사 로그가 **함께** 거짓말을 한다. 아래 여섯 개는 전부 그 성질을 가진다.

  1. force_final 턴이 ``tools`` 없이 dangling ``tool_use``/``tool_result`` 를 보내
     상류가 400 → 마지막 턴만 실패해 이미 과금된 N 턴 + N 검색이 전부 유실.
  2. 중첩 형태의 provider 오류 청크(``{"error": {...}}``)가 조용히 사라져, 잘린 답변이
     정상 완료로 보고됨.
  3. 검색 턴의 ``stop_reason: "tool_use"`` 가 최종 프레임으로 누출 — 클라이언트에는
     tool_use 블록이 하나도 없는데 SDK 는 도구 결과를 기다린다.
  4. Responses: 종료 이벤트를 못 받은 턴이 **직전 검색 턴의 객체**를 실은 조작된
     ``response.completed`` 를 받음(그 객체의 output 은 비어 있다).
  5. 검색 결과 바이트/턴당 검색 개수 상한 부재 — 청구서를 정하는 두 축이 무제한.
  6. MCP 핸드셰이크 무제한 — process-global 락 안에서 HTTP 3회, 죽은 게이트웨이의
     비용을 모든 요청이 상한만큼 되풀어 낸다.

⚠️ 검사는 **스티처를 실제로 돌려서** 한다. 이 파일의 대상은 "무엇이 배선됐는지" 가 아니라
   "클라이언트가 실제로 받는 프레임" 과 "상류로 나가는 요청 본문" 이다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.services.web_search_loop as wsl


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


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


def _anthropic_search_turn(i: int) -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": f"tu_{i}",
                                "name": wsl.GW_WEB_SEARCH_NAME, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]


_ANTHROPIC_FINAL = [
    _raw({"type": "message_start", "message": {"usage": {"input_tokens": 50}}}),
    _raw({"type": "content_block_start", "index": 0,
          "content_block": {"type": "text", "text": ""}}),
    _raw({"type": "content_block_delta", "index": 0,
          "delta": {"type": "text_delta", "text": "answer"}}),
    _raw({"type": "content_block_stop", "index": 0}),
    _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
          "usage": {"output_tokens": 3}}),
    _raw({"type": "message_stop"}),
]


async def _run_anthropic(turns, *, mcp=None, max_iterations=3, **kw):
    """스티처를 돌리고 (이벤트 목록, 상류로 나간 요청 본문 목록) 을 돌려준다."""
    queue = list(turns)
    bodies: list[dict] = []

    async def invoke_stream(body):
        bodies.append(json.loads(json.dumps(body)))
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
        mcp_client=mcp or _Mcp(),
        request=None,
        on_usage=on_usage,
        max_iterations=max_iterations,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
        **kw,
    ):
        out.append(chunk.decode())
    return out, bodies


def _events(out: list[str]) -> list[str]:
    return [c.split("\n", 1)[0].replace("event: ", "") for c in out]


def _data_of(out: list[str], name: str) -> dict | None:
    for c in out:
        if c.startswith(f"event: {name}\n"):
            return json.loads(c.split("data: ", 1)[1])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. force_final 턴의 요청 본문이 유효한가
# ─────────────────────────────────────────────────────────────────────────────


async def test_force_final_turn_has_no_dangling_tool_blocks():
    """⚠️ 이 파일에서 가장 비싼 결함.

    force_final 턴은 ``tools`` 를 빼고 보낸다. 그런데 대화에는 앞선 턴의
    ``tool_use``/``tool_result`` 가 남아 있어 Anthropic-on-Bedrock 이 **거부한다**.
    실패하는 것은 마지막 턴 하나인데, 그 시점에는 N 번의 모델 턴과 N 번의 검색이 이미
    과금됐고 사용자는 답을 하나도 받지 못한다. deadline 경로에서는 검색 한 번으로도 난다.
    """
    out, bodies = await _run_anthropic(
        [_anthropic_search_turn(1), _anthropic_search_turn(2), _ANTHROPIC_FINAL],
        max_iterations=2,
    )
    final_body = bodies[-1]
    assert "tools" not in final_body, "force_final 턴인데 tools 가 남았다 — 전제가 깨졌다"

    kinds = [
        b.get("type")
        for m in final_body["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
    ]
    assert "tool_use" not in kinds and "tool_result" not in kinds, (
        f"tools 없는 요청에 배관이 남았다: {kinds} — 상류가 400 을 준다"
    )


async def test_force_final_preserves_the_search_text_we_paid_for():
    """배관을 지우면서 웹 결과 텍스트까지 버리면 모델이 근거 없이 답한다.

    그 검색은 쿼리당 과금된 것이고, 답변을 실제로 근거지었다.
    """
    _out, bodies = await _run_anthropic(
        [_anthropic_search_turn(1), _ANTHROPIC_FINAL], max_iterations=1
    )
    texts = [
        b.get("text", "")
        for m in bodies[-1]["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert any("WEBTEXT" in t for t in texts), (
        f"검색 결과 텍스트가 사라졌다: {texts} — 모델이 검색 없이 답하게 된다"
    )


async def test_force_final_leaves_no_empty_content_and_keeps_alternation():
    """빈 ``content`` 도 거부된다 — 우리 tool_use 하나만 있던 assistant 메시지가 그 경우다."""
    _out, bodies = await _run_anthropic(
        [_anthropic_search_turn(1), _ANTHROPIC_FINAL], max_iterations=1
    )
    msgs = bodies[-1]["messages"]
    assert all(m.get("content") for m in msgs), f"빈 content 가 있다: {msgs}"
    roles = [m.get("role") for m in msgs]
    assert "user" in roles and "assistant" in roles
    # 같은 role 이 연속으로 두 번 나오면 Anthropic 이 거부한다.
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), f"role 교대 붕괴: {roles}"


async def test_a_request_with_no_search_is_untouched():
    """대조군 — 검색이 없었던 요청은 스트리핑을 타지 않는다(바이트 동일).

    ``our_tool_use_ids`` 가 비면 헬퍼가 **입력 객체를 그대로** 돌려준다.
    """
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert wsl._strip_anthropic_web_search_plumbing(msgs, set()) is msgs


def test_client_owned_tool_blocks_are_not_stripped():
    """클라이언트 소유 도구의 배관은 건드리지 않는다 — 그쪽은 클라이언트 루프의 상태다."""
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "ours", "name": wsl.GW_WEB_SEARCH_NAME, "input": {}},
            {"type": "tool_use", "id": "theirs", "name": "get_weather", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ours", "content": "web"},
            {"type": "tool_result", "tool_use_id": "theirs", "content": "sunny"},
        ]},
    ]
    out = wsl._strip_anthropic_web_search_plumbing(msgs, {"ours"})
    kinds = [b["type"] for m in out for b in m["content"]]
    assert kinds.count("tool_use") == 1, f"클라이언트 tool_use 가 사라졌다: {kinds}"
    assert kinds.count("tool_result") == 1
    ids = [b.get("id") or b.get("tool_use_id") for m in out for b in m["content"]]
    assert "ours" not in ids and "theirs" in ids


def test_responses_strip_removes_the_orphaned_reasoning_item():
    """⚠️ Responses 는 뒤따르는 쌍이 없는 ``reasoning`` 항목을 거부한다.

    배관만 지우면 그 reasoning 이 고아가 되어 다시 400 이다.
    """
    items = [
        {"type": "reasoning", "id": "r1"},
        {"type": "function_call", "call_id": "c1", "name": wsl.GW_WEB_SEARCH_NAME},
        {"type": "function_call_output", "call_id": "c1", "output": "web"},
    ]
    out = wsl._strip_responses_web_search_items(items, {"c1"})
    assert not any(i.get("type") == "reasoning" for i in out), f"고아 reasoning 이 남았다: {out}"
    assert not any(i.get("type") == "function_call" for i in out)
    assert any("web" in json.dumps(i) for i in out), "검색 결과가 사라졌다"


# ─────────────────────────────────────────────────────────────────────────────
# 2. 스트림 종료를 거짓으로 보고하지 않는가
# ─────────────────────────────────────────────────────────────────────────────

_NESTED_ERROR = _raw({"error": {"type": "provider_error", "message": "upstream died"}})


async def test_nested_provider_error_chunk_reaches_the_client():
    """⚠️ 이 레포의 **모든** 어댑터가 내보내는 형태는 type 이 중첩되어 있다.

    최상위 ``type`` 이 없어 알려진 분기에 걸리지 않고 그대로 사라졌다. 결과는 잘린 답변에
    붙은 정상 종료 — 클라이언트도, 본문 감사 로그도(종료 프레임 유무로 채점한다) "성공" 으로
    기록한다.
    """
    truncated = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 30}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "Partial"}}),
        _NESTED_ERROR,
    ]
    out, _ = await _run_anthropic([_anthropic_search_turn(1), truncated])
    evs = _events(out)
    assert "error" in evs, f"오류 프레임이 없다: {evs} — 잘린 답변이 성공으로 보고된다"
    err = _data_of(out, "error")
    assert err is not None and err.get("type") == "error", (
        f"최상위 type 이 없다: {err} — SSE 이벤트 이름으로 분기하는 SDK 가 예외를 던지지 않는다"
    )
    # ⚠️ 여기서 멈추면 검사가 공허하다. 이 시나리오는 종료 프레임도 없으므로, 중첩 오류를
    #    떨어뜨려도 "스트림이 잘렸다"(incomplete_stream)는 우리 자체 오류 프레임이 나가서
    #    위 단정이 통과한다. **상류가 말한 것**이 전달되는지를 봐야 한다.
    inner = err.get("error") or {}
    assert inner.get("type") == "provider_error", (
        f"상류 오류가 우리 일반 오류로 대체됐다: {inner} — 무엇이 실패했는지 알 수 없다"
    )
    assert "upstream died" in json.dumps(inner), (
        f"상류 메시지가 사라졌다: {inner}"
    )


async def test_a_truncated_stream_does_not_claim_a_stop_reason():
    """``message_delta`` 는 "이렇게 끝났다" 를 말하는 프레임이다. 잘렸으면 보내지 않는다."""
    truncated = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 30}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "Partial"}}),
    ]
    out, _ = await _run_anthropic([_anthropic_search_turn(1), truncated])
    evs = _events(out)
    assert "message_delta" not in evs, (
        f"잘린 스트림에 종료 프레임을 붙였다: {evs} — 클라이언트가 완료로 읽는다"
    )
    assert "error" in evs, "잘렸다는 신호가 없다"
    assert evs[-1] == "message_stop", "스트림이 닫히지 않으면 클라이언트가 매달린다"


async def test_open_content_block_is_closed_on_truncation():
    """열린 블록을 닫지 않으면 SDK 쪽에서 파싱 오류로 보이고 원인이 상류인지 우리인지 모른다."""
    truncated = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 30}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "Partial"}}),
    ]
    out, _ = await _run_anthropic([_anthropic_search_turn(1), truncated])
    evs = _events(out)
    assert evs.count("content_block_start") == evs.count("content_block_stop"), (
        f"start/stop 짝이 맞지 않는다: {evs}"
    )


async def test_search_turn_stop_reason_does_not_leak_into_the_terminal_frame():
    """⚠️ 클라이언트에는 tool_use 블록이 **하나도** 보이지 않았다(우리 것은 전부 억제).

    그런데 stop_reason 이 tool_use 면 Anthropic SDK / Claude Code 는 도구 결과를 기다리며
    없는 tool_use 를 찾다가 멈추거나 재요청한다.
    """
    # 답변 턴이 stop_reason 을 주지 않는 경우 — 옛 구현은 검색 턴의 값을 물려썼다.
    no_stop_reason = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 30}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]
    out, _ = await _run_anthropic([_anthropic_search_turn(1), no_stop_reason])
    md = _data_of(out, "message_delta")
    assert md is not None
    assert md["delta"]["stop_reason"] != "tool_use", (
        "검색 턴의 tool_use 가 최종 프레임으로 누출됐다 — 클라이언트가 없는 도구를 기다린다"
    )


async def test_a_normal_completion_is_still_reported_as_such():
    """⚠️ 대조군. 위 단정들이 "항상 오류" 로 통과하는 것이 아님을 보인다."""
    out, _ = await _run_anthropic([_anthropic_search_turn(1), _ANTHROPIC_FINAL])
    evs = _events(out)
    assert "error" not in evs, f"정상 완료인데 오류 프레임이 붙었다: {evs}"
    md = _data_of(out, "message_delta")
    assert md["delta"]["stop_reason"] == "end_turn"
    assert evs[-1] == "message_stop"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Responses 방언의 종료 프레임
# ─────────────────────────────────────────────────────────────────────────────

_RESP_SEARCH = [
    _raw({"type": "response.created", "response": {"id": "resp_1"}}),
    _raw({"type": "response.output_item.added", "output_index": 0,
          "item": {"type": "function_call", "call_id": "c1",
                   "name": wsl.GW_WEB_SEARCH_NAME, "arguments": ""}}),
    _raw({"type": "response.function_call_arguments.delta", "output_index": 0,
          "delta": '{"query":"q"}'}),
    _raw({"type": "response.output_item.done", "output_index": 0,
          "item": {"type": "function_call", "call_id": "c1",
                   "name": wsl.GW_WEB_SEARCH_NAME, "arguments": '{"query":"q"}'}}),
    _raw({"type": "response.completed",
          "response": {"id": "resp_1", "status": "completed", "output": []}}),
]


async def _run_responses(turns, **kw):
    queue = list(turns)

    async def invoke_stream(body):
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    out: list[str] = []
    async for chunk in wsl._responses_stream(
        invoke_stream=invoke_stream, base_body={"input": "hi"}, mcp_client=_Mcp(),
        request=None, on_usage=on_usage, max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999, default_max_results=5, **kw,
    ):
        out.append(chunk.decode())
    return out


async def test_responses_truncated_turn_is_not_a_fabricated_completed():
    """⚠️ 옛 구현은 **직전 검색 턴의 객체**를 실은 response.completed 를 내보냈다.

    그 객체의 ``output`` 은 우리 web_search 호출을 올바르게 제거했기 때문에 **비어 있다** —
    델타가 아니라 최종 객체로 답을 재구성하는 클라이언트(Codex 계열)는 빈 답변을 받고
    완료로 기록한다.
    """
    truncated = [
        _raw({"type": "response.created", "response": {"id": "resp_2"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"}),
    ]
    out = await _run_responses([_RESP_SEARCH, truncated])
    evs = _events(out)
    assert "response.completed" not in evs, (
        f"잘린 턴에 completed 를 붙였다: {evs}"
    )
    assert "response.incomplete" in evs, f"잘렸다는 신호가 없다: {evs}"
    assert "error" in evs


async def test_responses_provider_error_is_failed_not_incomplete():
    """오류로 끝난 것과 그냥 잘린 것은 다른 신호여야 한다.

    합치면 운영자가 provider 장애와 네트워크 절단을 구분할 수 없다.
    """
    errored = [
        _raw({"type": "response.created", "response": {"id": "resp_2"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"}),
        _NESTED_ERROR,
    ]
    out = await _run_responses([_RESP_SEARCH, errored])
    evs = _events(out)
    assert "response.failed" in evs, f"provider 오류가 failed 로 보고되지 않았다: {evs}"
    assert "response.completed" not in evs


async def test_responses_terminal_id_matches_the_envelope():
    """여러 턴을 하나의 응답으로 합치는 것이 이 스티처의 계약이다.

    종료 프레임이 마지막 턴의 id 를 쓰면 created(resp_1) … completed(resp_2) 가 되어,
    id 로 요청을 상관짓는 클라이언트와 로그가 그 응답을 찾지 못한다.
    """
    final = [
        _raw({"type": "response.created", "response": {"id": "resp_2"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "Full"}),
        _raw({"type": "response.completed",
              "response": {"id": "resp_2", "status": "completed",
                           "output": [{"type": "message"}]}}),
    ]
    out = await _run_responses([_RESP_SEARCH, final])
    created = _data_of(out, "response.created")
    completed = _data_of(out, "response.completed")
    assert created and completed
    assert completed["response"]["id"] == created["response"]["id"], (
        f"봉투 id={created['response']['id']!r} 인데 종료 id={completed['response']['id']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 비용 축의 상한
# ─────────────────────────────────────────────────────────────────────────────


def test_result_truncation_marks_itself():
    """조용한 절단이 최악이다 — JSON 이 레코드 중간에서 끊긴 것을 모델은 "결과 전체" 로
    읽고 단정적으로 답한다."""
    text, truncated = wsl._truncate_result("x" * 1000, 100)
    assert truncated
    assert len(text) > 100  # 표지가 붙었다
    assert "truncated" in text.lower(), f"절단 표지가 없다: {text[-80:]}"


def test_truncation_is_disabled_at_zero():
    """캡 이전 동작이 바이트 단위로 재현 가능해야 한다."""
    original = "y" * 5000
    text, truncated = wsl._truncate_result(original, 0)
    assert text == original and not truncated


def test_turn_allowance_caps_and_unlimited_at_zero():
    assert wsl._turn_search_allowance(20, 4) == 4
    assert wsl._turn_search_allowance(2, 4) == 2
    assert wsl._turn_search_allowance(20, 0) == 20


async def test_per_turn_fanout_is_capped_but_every_id_gets_a_result():
    """⚠️ 초과분도 **응답은 만들어 줘야 한다.**

    Anthropic 은 tool_use 하나당 정확히 하나의 tool_result 를 요구한다 — 개수가 어긋나면
    다음 턴이 400 이라, 상한을 지키려다 요청을 깨뜨리게 된다.
    """
    ids = [f"tu_{i}" for i in range(6)]
    fanout = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
    ]
    for i, tid in enumerate(ids):
        fanout += [
            _raw({"type": "content_block_start", "index": i,
                  "content_block": {"type": "tool_use", "id": tid,
                                    "name": wsl.GW_WEB_SEARCH_NAME, "input": {}}}),
            _raw({"type": "content_block_delta", "index": i,
                  "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}}),
            _raw({"type": "content_block_stop", "index": i}),
        ]
    fanout += [
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]

    mcp = _Mcp()
    _out, bodies = await _run_anthropic(
        [fanout, _ANTHROPIC_FINAL], mcp=mcp, max_searches_per_turn=2
    )
    assert mcp.calls == 2, f"검색이 {mcp.calls}회 실행됐다 — 상한 2 를 넘었다"

    # 두 번째 턴(continuation)의 user 메시지에 6개 tool_result 가 모두 있어야 한다.
    continuation = bodies[1]
    results = [
        b for m in continuation["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(results) == len(ids), (
        f"tool_result 가 {len(results)}개 — tool_use {len(ids)}개와 맞지 않으면 상류가 400 이다"
    )
    assert {r["tool_use_id"] for r in results} == set(ids)


async def test_result_cap_is_applied_on_the_wire():
    """캡이 실제로 다음 턴 입력의 바이트를 줄이는지."""
    mcp = _Mcp(text="Z" * 5000)
    _out, bodies = await _run_anthropic(
        [_anthropic_search_turn(1), _ANTHROPIC_FINAL], mcp=mcp, max_result_chars=200
    )
    payload = json.dumps(bodies[1])
    assert "Z" * 5000 not in payload, "원본 결과가 그대로 다음 턴에 들어갔다"
    assert "Z" * 200 in payload, "캡 이하 부분까지 사라졌다"


async def test_a_truncated_search_still_counts_as_a_successful_search():
    """잘린 검색도 과금됐고 답변을 근거지었다 — ok 플래그를 뒤집으면 귀속이 어긋난다."""
    mcp = _Mcp(text="Z" * 5000)
    text, ok, _trace = await wsl._do_search(mcp, {"query": "q"}, 5, 100)
    assert ok is True, "캡이 성공 플래그를 뒤집었다 — web_search_count 가 어긋난다"
    assert "truncated" in text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 5. MCP 핸드셰이크의 상한
# ─────────────────────────────────────────────────────────────────────────────


async def test_handshake_is_bounded_and_negatively_cached():
    """⚠️ 핸드셰이크는 process-global 락 안에서 HTTP 3회를 순차로 부른다.

    게이트웨이가 죽어 있으면 요청 하나가 3 × timeout 을 소모하고 동시 요청은 그 락에서
    직렬화된다 — 각자 자기 RPM/TPM/CPH 예약을 물고서. 음성 캐시가 없으면 죽어 있는 동안
    모든 요청이 상한을 되풀어 낸다.
    """
    from app.services.agentcore_mcp_client import AgentCoreMcpClient

    started = 0

    class _Hang(AgentCoreMcpClient):
        def __init__(self):
            super().__init__(
                http_client=None, gateway_url="https://example.invalid",
                handshake_timeout=0.05, handshake_negative_ttl=30.0,
            )

        async def _handshake(self):
            nonlocal started
            started += 1
            await asyncio.sleep(5)  # 상한이 없으면 여기서 매달린다
            return "WebSearch"

    c = _Hang()
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(Exception):
        await c.ensure_initialized()
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 1.0, f"{elapsed:.2f}s 걸렸다 — 상한이 걸리지 않았다"
    assert started == 1

    # 두 번째 호출은 음성 캐시로 즉시 실패해야 한다(핸드셰이크를 다시 시작하지 않는다).
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(Exception):
        await c.ensure_initialized()
    assert asyncio.get_event_loop().time() - t0 < 0.02, "음성 캐시가 없다 — 상한을 또 물었다"
    assert started == 1, f"핸드셰이크를 {started}회 시작했다 — 음성 캐시가 듣지 않았다"


async def test_negative_cache_clears_after_a_success():
    """대조군 — 음성 캐시가 성공을 영구히 막아서는 안 된다."""
    from app.services.agentcore_mcp_client import AgentCoreMcpClient

    class _Flaky(AgentCoreMcpClient):
        def __init__(self):
            super().__init__(
                http_client=None, gateway_url="https://example.invalid",
                handshake_timeout=1.0, handshake_negative_ttl=0.0,
            )
            self.attempts = 0

        async def _handshake(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("gateway down")
            self._tool_name = "WebSearch"
            self._initialized = True
            self._handshake_failed_at = None
            return "WebSearch"

    c = _Flaky()
    with pytest.raises(RuntimeError):
        await c.ensure_initialized()
    assert await c.ensure_initialized() == "WebSearch"
    assert c._handshake_failed_at is None, "성공했는데 음성 캐시가 남았다"
