# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""B1 regression — client cancel at a random chunk on both streaming paths.

실측(wave 3 이전): 로컬 mock 부하에서 클라이언트가 끊은 스트림 185/185 가 ``output_tokens=1``
(message_start 자리표시자)·``estimated_usage=false`` 로 기록됐다. uvicorn 은 ASGI spec 2.3 을
알리므로 Starlette 는 끊김 시 응답 태스크를 **취소**하고, 그 취소가 대기 중이던 상류
``__anext__`` 를 함께 죽여 disconnect 드레인이 빈 이터레이터를 읽었다.

이 테스트는 그 경로를 그대로 재현한다: 실제 ``StreamingResponse.__call__`` 을 spec 2.3 scope
로 돌리고, 클라이언트가 무작위 k 번째 청크 뒤에 ``http.disconnect`` 를 보낸다(상류가 다음
청크를 기다리는 중). 두 경로 모두:

  * 드레인이 끝까지 가면 → 최종 usage(message_delta) 로 정확히 청구, estimated=False
  * 드레인 타임아웃이면 → 상류가 실제로 만든 출력 델타 수만큼(토크나이저) 역산, estimated=True
  * 상태 = CANCELLED, 정산은 정확히 1회
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.responses import StreamingResponse

from app.services import streaming as streaming_mod
from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    drain_pending_tasks,
    usage_status_of,
)

#: 상류가 보내는 출력 델타 수 — 델타 하나 = 출력 1 토큰(" the"), mock-bedrock 과 같은 계약.
N_DELTAS = 24
INPUT_TOKENS = 100
_KINDS = ("text_delta", "thinking_delta", "input_json_delta")


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class FakeUpstream:
    """``_bedrock_stream_gen`` 모형 — 청크 사이에 상류 대기, 닫히면 finally 가 스트림을 닫는다."""

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.produced_deltas = 0
        self.closed_early = False
        self.finished = False

    async def gen(self):
        try:
            yield _b({"type": "message_start", "message": {
                "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 1}}})
            yield _b({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}})
            for i in range(N_DELTAS):
                await asyncio.sleep(self.interval)  # 상류 대기 — 여기서 취소가 들어온다
                kind = _KINDS[i % 3]
                key = {"text_delta": "text", "thinking_delta": "thinking",
                       "input_json_delta": "partial_json"}[kind]
                self.produced_deltas += 1
                yield _b({"type": "content_block_delta", "index": 0,
                          "delta": {"type": kind, key: " the"}})
            await asyncio.sleep(self.interval)
            yield _b({"type": "content_block_stop", "index": 0})
            yield _b({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                      "usage": {"output_tokens": N_DELTAS}})
            yield _b({"type": "message_stop"})
            self.finished = True
        finally:
            if not self.finished:
                self.closed_early = True


async def _count_the(text: str) -> int:
    """토크나이저 모형 — " the" 하나 = 1 토큰."""
    return text.count(" the")


async def _serve_and_cancel(resp: StreamingResponse, cancel_after: int) -> int:
    """spec 2.3 scope 로 응답을 돌리고, 본문 청크 ``cancel_after`` 개 뒤 클라이언트가 끊는다."""
    sent = 0
    disconnect = asyncio.Event()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        nonlocal sent
        if msg["type"] == "http.response.body" and msg.get("body"):
            sent += 1
            if sent >= cancel_after:
                disconnect.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "POST", "path": "/", "headers": []}
    await resp(scope, receive, send)
    return sent


# ── /v1/messages 경로 (services/streaming.bedrock_anthropic_sse_stream) ────────


async def _messages_case(cancel_after: int, drain_timeout: float):
    up = FakeUpstream()
    outcome: dict = {}
    usages = []

    async def on_usage(u, _ttft=None):
        usages.append(u)

    gen = bedrock_anthropic_sse_stream(
        MagicMock(), up.gen(), on_usage=on_usage, idle_timeout=5.0,
        drain_timeout=drain_timeout, tokenizer_hook=_count_the, outcome=outcome,
    )
    await _serve_and_cancel(StreamingResponse(gen, media_type="text/event-stream"),
                            cancel_after)
    await drain_pending_tasks(timeout=5.0)
    return up, outcome, usages


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(8))
async def test_messages_random_chunk_cancel_bills_final_usage(seed):
    k = random.Random(seed).randint(1, N_DELTAS)  # message_start/start 프레임 이후 아무 데서나
    up, outcome, usages = await _messages_case(k, drain_timeout=5.0)
    assert len(usages) == 1, "정산이 유실/중복됐다"
    u = usages[0]
    assert up.finished, "드레인이 상류를 끝까지 읽지 못했다(취소가 상류 읽기까지 전파)"
    assert (u.input_tokens, u.output_tokens) == (INPUT_TOKENS, N_DELTAS)
    assert u.estimated is False
    assert usage_status_of(outcome) == "CANCELLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_messages_random_chunk_cancel_drain_timeout_estimates_delivered(seed):
    k = random.Random(100 + seed).randint(1, N_DELTAS // 2)
    up, outcome, usages = await _messages_case(k, drain_timeout=0.03)
    u = usages[0]
    assert not up.finished and up.closed_early
    # 상류가 실제로 만든 출력(전달 + 드레인) 전부 — text·thinking·input_json 모두 센다.
    assert u.output_tokens == up.produced_deltas
    assert u.estimated is True
    assert u.input_tokens == INPUT_TOKENS
    assert usage_status_of(outcome) == "CANCELLED"


# ── Bedrock /model/* 패스스루 (routers/bedrock._handle_bedrock) ───────────────


def _passthrough_request(up: FakeUpstream):
    from app.routers import bedrock as bedrock_mod

    state = {
        "auth_context": SimpleNamespace(user_id="u1", team_id="t1", sso_subject=None),
        "_redis_effective": None,
        "_session_factory": None,
        "request_id": "req-b1",
        "request_start_time": time.monotonic(),
        "client": "claude-code",
    }
    request = MagicMock()
    request.scope = {"state": state}
    request.body = AsyncMock(return_value=b'{"messages":[]}')
    calls: list = []

    class _Recorder:
        async def finalize(self, *args, **kwargs):
            calls.append((args, kwargs))

    class _Adapter:
        async def invoke_stream(self, body, model_id, **kwargs):
            return 200, up.gen(), {"Content-Type": "application/vnd.amazon.eventstream"}, "brid-1"

    class _Tok:
        async def estimate_output_tokens(self, text, *, provider, model_id):
            return await _count_the(text)

    request.app = SimpleNamespace(state=SimpleNamespace(
        provider_registry=SimpleNamespace(get=lambda _pt: _Adapter()),
        cost_recorder=_Recorder(), metrics=None, tokenizer=_Tok(),
    ))
    model_config = SimpleNamespace(provider_model_id="anthropic.test-v1", alias="test-alias",
                                   provider=bedrock_mod.ProviderType.BEDROCK)
    return bedrock_mod, request, model_config, calls


async def _passthrough_case(monkeypatch, cancel_after: int, drain_timeout: float):
    up = FakeUpstream()
    bedrock_mod, request, model_config, calls = _passthrough_request(up)
    svc = bedrock_mod._router_service
    monkeypatch.setattr(svc, "resolve_bedrock_model", AsyncMock(return_value=model_config))
    monkeypatch.setattr(svc, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(svc, "check_client_model_scope", lambda *a, **k: None)
    import app.services.rate_limit_enforcement as rle

    monkeypatch.setattr(rle, "enforce_rate_limits", AsyncMock(return_value=None))
    monkeypatch.setattr(streaming_mod, "_resolve_timeouts", lambda *_a: (5.0, drain_timeout))
    resp = await bedrock_mod._handle_bedrock(
        request, "test-alias", "invoke-with-response-stream", stream=True)
    await _serve_and_cancel(resp, cancel_after)
    await drain_pending_tasks(timeout=5.0)
    return up, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(8))
async def test_passthrough_random_chunk_cancel_bills_final_usage(monkeypatch, seed):
    k = random.Random(200 + seed).randint(1, N_DELTAS)
    up, calls = await _passthrough_case(monkeypatch, k, drain_timeout=5.0)
    assert len(calls) == 1, "정산이 유실/중복됐다"
    args, kwargs = calls[0]
    u = args[3]
    assert up.finished
    assert (u.input_tokens, u.output_tokens) == (INPUT_TOKENS, N_DELTAS)
    assert u.estimated is False
    assert kwargs["status"] == "CANCELLED"
    assert kwargs["bedrock_request_id"] == "brid-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_passthrough_random_chunk_cancel_drain_timeout_estimates(monkeypatch, seed):
    k = random.Random(300 + seed).randint(1, N_DELTAS // 2)
    up, calls = await _passthrough_case(monkeypatch, k, drain_timeout=0.03)
    args, kwargs = calls[0]
    u = args[3]
    assert not up.finished and up.closed_early
    # message_start 의 자리표시자(1)가 아니라 상류가 만든 출력 델타 전부.
    assert u.output_tokens == up.produced_deltas
    assert u.estimated is True
    assert kwargs["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_passthrough_midstream_upstream_error_is_error_with_estimate(monkeypatch):
    """상류가 중간에 예외 프레임을 내면 ERROR + 전달된 출력 역산(estimated)."""
    from app.routers import bedrock as bedrock_mod

    async def gen():
        yield _b({"type": "message_start", "message": {
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 1}}})
        for _ in range(3):
            yield _b({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": " the"}})
        yield _b({"type": "error", "error": {"type": "internalServerException", "message": "x"}})

    up = FakeUpstream()
    up.gen = gen  # type: ignore[method-assign]
    bedrock_mod, request, model_config, calls = _passthrough_request(up)
    svc = bedrock_mod._router_service
    monkeypatch.setattr(svc, "resolve_bedrock_model", AsyncMock(return_value=model_config))
    monkeypatch.setattr(svc, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(svc, "check_client_model_scope", lambda *a, **k: None)
    import app.services.rate_limit_enforcement as rle

    monkeypatch.setattr(rle, "enforce_rate_limits", AsyncMock(return_value=None))
    resp = await bedrock_mod._handle_bedrock(
        request, "test-alias", "invoke-with-response-stream", stream=True)
    async for _ in resp.body_iterator:
        pass
    args, kwargs = calls[0]
    u = args[3]
    assert kwargs["status"] == "ERROR"
    assert (u.input_tokens, u.output_tokens, u.estimated) == (INPUT_TOKENS, 3, True)


@pytest.mark.asyncio
async def test_shielded_upstream_keeps_pending_read_across_cancel():
    """ShieldedUpstream — 소비자 취소 후 다음 __anext__ 가 같은 읽기 결과를 받는다."""
    produced = []

    async def slow():
        for i in range(3):
            await asyncio.sleep(0.02)
            produced.append(i)
            yield i

    it = streaming_mod.shield_upstream(slow())
    t = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.005)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert [x async for x in it] == [0, 1, 2]
    await it.aclose()


# ── proof 가 찾은 세 경로(wave 3 load proof) ───────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_during_settlement_still_records():
    """상류가 끝난 뒤 정산(토크나이저 → finalize) 도중 클라이언트 취소가 와도 정산은 끝난다.

    proof 실측: /v1/messages 미드스트림 오류 3건이 이 창에서 usage 행 없이 사라졌다.
    """
    recorded = []
    entered = asyncio.Event()

    async def slow_tokenizer(text):
        entered.set()
        await asyncio.sleep(0.05)
        return text.count(" the")

    async def on_usage(u, _t=None):
        recorded.append(u)

    async def up():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 5}}})
        yield _b({"type": "content_block_delta", "delta": {"type": "text_delta", "text": " the"}})
        yield _b({"type": "error", "error": {"type": "internalServerException", "message": "x"}})

    outcome: dict = {}
    gen = bedrock_anthropic_sse_stream(MagicMock(), up(), on_usage=on_usage, idle_timeout=5.0,
                                       drain_timeout=1.0, tokenizer_hook=slow_tokenizer,
                                       outcome=outcome)

    async def consume():
        async for _ in gen:
            pass

    t = asyncio.ensure_future(consume())
    await entered.wait()
    t.cancel()  # 정산(토크나이저) 도중 응답 태스크 취소
    with pytest.raises(asyncio.CancelledError):
        await t
    await drain_pending_tasks(timeout=2.0)
    assert len(recorded) == 1
    assert (recorded[0].input_tokens, recorded[0].output_tokens) == (5, 1)
    assert usage_status_of(outcome) == "ERROR"


@pytest.mark.asyncio
async def test_unstarted_generator_is_woken_and_settled(monkeypatch):
    """첫 바이트 전 끊김(spec<2.4: 제너레이터 미시작) — guard 가 깨워 드레인 정산을 태운다."""
    from app.routers import bedrock as bedrock_mod
    from app.services.fallback_loop import unsettled_reservation_guard

    up = FakeUpstream()
    _bm, request, model_config, calls = _passthrough_request(up)
    svc = bedrock_mod._router_service
    monkeypatch.setattr(svc, "resolve_bedrock_model", AsyncMock(return_value=model_config))
    monkeypatch.setattr(svc, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(svc, "check_client_model_scope", lambda *a, **k: None)
    import app.services.rate_limit_enforcement as rle

    monkeypatch.setattr(rle, "enforce_rate_limits", AsyncMock(return_value=None))
    resp = await bedrock_mod._handle_bedrock(
        request, "test-alias", "invoke-with-response-stream", stream=True)
    # 본문을 한 번도 돌리지 않고(미시작) background 만 실행 — Starlette 의 첫 바이트 전 끊김.
    await resp.background()
    await drain_pending_tasks(timeout=5.0)
    assert len(calls) == 1, "미시작 스트림이 정산 없이 버려졌다"
    args, kwargs = calls[0]
    assert (args[3].input_tokens, args[3].output_tokens) == (INPUT_TOKENS, N_DELTAS)
    assert kwargs["status"] == "CANCELLED"
    assert unsettled_reservation_guard  # import 확인


@pytest.mark.asyncio
async def test_web_search_cancel_while_next_turn_is_connecting_bills_that_turn():
    """검색 뒤 다음 턴 호출(invoke_stream) 도중 끊김 — 상류는 그 턴을 받았다. 드레인이 회수한다.

    proof 실측: 취소된 웹 검색 요청 53건 중 10건의 답 턴이 청구에서 빠졌다.
    """
    from app.services import web_search_loop as wsl

    turn = {"n": 0}
    gate = asyncio.Event()

    def _events(tool: bool):
        evs = [{"type": "message_start", "message": {"usage": {"input_tokens": 100}}}]
        if tool:
            evs += [{"type": "content_block_start", "index": 0,
                     "content_block": {"type": "tool_use", "id": "t1", "name": "web_search",
                                       "input": {}}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                     "usage": {"output_tokens": 5}}]
        else:
            evs += [{"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": " the"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                     "usage": {"output_tokens": 40}}]
        evs.append({"type": "message_stop"})
        return evs

    async def invoke_stream(_body):
        turn["n"] += 1
        if turn["n"] == 2:
            gate.set()
            await asyncio.sleep(0.05)  # 연결 중 — 여기서 클라이언트가 끊는다

        async def it(evs=_events(turn["n"] == 1)):
            for e in evs:
                yield _b(e)
        return 200, it(), {}, None

    class _Mcp:
        async def ensure_initialized(self):
            return "web-search-tool___WebSearch"

        def tool_input_schema(self):
            return None

        async def search(self, query, max_results=10):
            from app.services.agentcore_mcp_client import WebSearchResponse

            return WebSearchResponse(results=[], raw_text='{"results": []}')

    usages = []

    async def on_usage(u):
        usages.append(u)

    outcome: dict = {}
    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(), request=MagicMock(), on_usage=on_usage, max_iterations=3,
        deadline=time.monotonic() + 30, default_max_results=5, outcome=outcome)

    async def consume():
        async for _ in gen:
            pass

    t = asyncio.ensure_future(consume())
    await gate.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await drain_pending_tasks(timeout=5.0)
    assert len(usages) == 1
    u = usages[0]
    assert (u.input_tokens, u.output_tokens, u.web_search_count) == (200, 45, 1)
    assert usage_status_of(outcome) == "CANCELLED"
