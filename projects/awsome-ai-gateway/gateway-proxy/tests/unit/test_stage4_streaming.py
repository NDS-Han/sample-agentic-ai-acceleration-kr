# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Stage 4 회귀: 스트리밍 — SSE, disconnect 드레인, 과금 파이널라이즈.

커버하는 결함:
1. `/model/*` 패스스루에 disconnect 드레인 부재 — converse-stream 의 usage 는 종결
   `metadata` 이벤트에만 오므로 클라이언트가 끊으면 input/output 이 0 으로 정산돼
   상류가 이미 청구한 토큰이 무료가 됐다.
2. `_drain_remaining` 이 `async for` 로 상류 스톨 시 deadline 무시 무한 대기 —
   클라이언트도 사라진 드레인 태스크가 상류 커넥션·executor 스레드를 붙잡음.
3. botocore event-stream 제너레이터가 버려져도 stream.close() 가 안 불려
   executor 스레드가 blocking next() 에 영구 블록(풀 128 고갈 경로).
4. 비-chunk 미드스트림 예외 이벤트(internalServerException 등) silent drop,
   {"error":{...}} 형태 오류 청크가 `event: unknown`/`event: message` 로 오인.
5. web_search_loop 비스트림 early-return(F-7/MCP-fail)이 usage>0 일 때만
   on_usage 를 발화 — 비-2xx/zero-usage 시 보수적 입장 예약 누수.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.domain import TokenUsage
from app.services import web_search_loop as wsl
from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    drain_pending_tasks,
    responses_sse_stream,
)


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode()


async def _aiter(items: list[bytes]):
    for it in items:
        yield it


# ─────────────────────────────────────────────────────────────────────────────
# 1. 드레인 바운드 — 상류 스톨 시 drain_timeout 안에 반드시 끝난다
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_drain_bounded_when_upstream_stalls_mid_stream():
    """클라이언트 끊김 후 드레인이 상류 무응답에 무한 대기하지 않는다.

    구형 async-for 드레인은 __anext__ 가 resolve 될 때만 deadline 을 검사했다 —
    상류가 멈추면 드레인 태스크가 영구히 살아 상류 커넥션을 붙잡았다.
    """
    calls: list[TokenUsage] = []

    async def on_usage(usage, _ttft=None):
        calls.append(usage)

    async def stalling_iter():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        # 두 번째 청크는 영원히 오지 않는다 — 상류 스톨.
        await asyncio.Event().wait()

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), stalling_iter(), on_usage=on_usage,
        idle_timeout=60.0, drain_timeout=0.2,
    )
    async for _ in gen:  # 첫 프레임만 소비되고 두 번째 __anext__ 에서...
        break
    await gen.aclose()   # 클라이언트 끊김 시뮬레이션 → 백그라운드 드레인

    start = time.monotonic()
    await drain_pending_tasks(timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"드레인이 deadline 을 무시하고 매달렸다: {elapsed:.2f}s"
    assert len(calls) == 1, f"과금 유실/중복: {len(calls)}회"
    assert calls[0].input_tokens == 10


# ─────────────────────────────────────────────────────────────────────────────
# 2. 버려지는 이터레이터의 명시적 aclose
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_idle_timeout_closes_upstream_iterator():
    """idle timeout 으로 이터레이터를 포기할 때 aclose 가 호출돼야 한다.

    없으면 어댑터 제너레이터의 finally(상류 스트림 close)가 GC 시점까지 지연된다.
    """

    class TrackedIter:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            return b""

        async def aclose(self):
            self.closed = True

    it = TrackedIter()
    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), it, idle_timeout=0.1
    )
    frames = [f async for f in gen]
    assert any(b"timeout_error" in f for f in frames)
    assert it.closed, "idle-timeout 포기 경로가 업스트림 이터레이터를 닫지 않았다"


@pytest.mark.asyncio
async def test_drain_completion_closes_upstream_iterator():
    """드레인이 잔여 청크 소비/타임아웃으로 끝나면 이터레이터를 닫는다."""

    class TrackedIter:
        def __init__(self):
            self.closed = False
            self._items = iter([
                _b({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    calls: list[TokenUsage] = []

    async def on_usage(usage, _ttft=None):
        calls.append(usage)

    it = TrackedIter()
    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), it, on_usage=on_usage, idle_timeout=5.0, drain_timeout=0.5
    )
    async for _ in gen:
        break
    await gen.aclose()
    await drain_pending_tasks(timeout=2.0)
    assert it.closed, "드레인 완료 후 업스트림 이터레이터가 닫히지 않았다"
    assert len(calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. 오류 청크 재프레임 — {"error":{...}} 단일 형태
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_anthropic_stream_reframes_error_dict_chunk_as_error_event():
    """어댑터 미드스트림 오류({"error":{...}}, 최상위 type 없음)가
    `event: unknown` 이 아니라 `event: error` 로 나가야 한다."""
    chunks = [
        _b({"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _b({"error": {"type": "provider_error", "message": "upstream broke"}}),
    ]
    frames = [
        f async for f in bedrock_anthropic_sse_stream(
            FakeRequest(), _aiter(chunks), idle_timeout=5.0
        )
    ]
    assert any(f.startswith(b"event: error") for f in frames), (
        f"오류 청크가 error 이벤트로 재프레임되지 않았다: {frames}"
    )


@pytest.mark.asyncio
async def test_responses_stream_labels_error_dict_chunk_as_error_event():
    """Responses 방언도 같은 형태를 `event: message` 가 아니라 `event: error` 로."""
    chunks = [
        _b({"error": {"type": "provider_error", "message": "upstream broke"}}),
    ]
    frames = [
        f async for f in responses_sse_stream(
            FakeRequest(), _aiter(chunks), idle_timeout=5.0
        )
    ]
    assert any(f.startswith(b"event: error") for f in frames), (
        f"오류 청크가 error 이벤트로 재프레임되지 않았다: {frames}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. botocore event-stream 제너레이터 — 리소스 정리와 예외 이벤트
# ─────────────────────────────────────────────────────────────────────────────
class _FakeEventStream:
    """botocore EventStream 모사 — iterable + close()."""

    def __init__(self, events):
        self._events = events
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self):
        self.closed = True


class _BlockingEventStream:
    """next() 가 게이트 해제까지 블록 — close() 가 풀어준다."""

    def __init__(self):
        self.closed = False
        import threading

        self._gate = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        # close() 가 불리면 게이트가 열리고 StopIteration 으로 반환
        self._gate.wait(timeout=30)
        raise StopIteration

    def close(self):
        self.closed = True
        self._gate.set()


@pytest.mark.asyncio
async def test_bedrock_stream_gen_closes_upstream_on_abandon():
    """제너레이터가 aclose 되면 botocore 스트림도 닫힌다.

    없으면 executor 스레드가 blocking next() 에 영구히 묶인다(E2E S4).
    """
    from app.providers.bedrock_adapter import BedrockAdapter

    stream = _BlockingEventStream()
    adapter = BedrockAdapter(MagicMock())
    gen = adapter._bedrock_stream_gen(stream)
    # 첫 __anext__ 를 걸었다가 취소한다 — executor 스레드가 next() 에 블록된 상태에서
    # streaming 헬퍼의 wait_for 타임아웃과 같은 경로(CancelledError → 제너레이터 이탈).
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed, "버려진 제너레이터가 상류 스트림을 닫지 않았다"
    # close() 가 게이트를 열었으므로 블록된 next() 도 풀려 스레드가 회수된다
    # (close 자체가 _gate.set() — 스레드 복귀는 close 의 부수효과로 증명)


@pytest.mark.asyncio
async def test_bedrock_stream_gen_converts_exception_members_to_error_events():
    """비-chunk *Exception 멤버는 Anthropic-wire error 이벤트로 합성된다."""
    from app.providers.bedrock_adapter import BedrockAdapter

    events = [
        {"chunk": {"bytes": b'{"type":"message_start"}'}},
        {
            "internalServerException": {
                "message": "boom in arn:aws:iam::123456789012:role/secret"
            }
        },
    ]
    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._bedrock_stream_gen(iter(events))]
    assert len(received) == 2
    err = json.loads(received[1])
    assert err["type"] == "error"
    assert err["error"]["type"] == "internalServerException"
    # 인프라 식별자는 살균된다
    assert "arn:aws" not in err["error"]["message"]
    assert "123456789012" not in err["error"]["message"]


@pytest.mark.asyncio
async def test_bedrock_stream_gen_converts_eventstream_error_to_error_event():
    """botocore 는 미드스트림 예외 프레임을 EventStreamError(=ClientError)로
    raise 한다 — dict 멤버가 아니므로, catch 없으면 스트림이 에러 신호 없이
    잘긴다(R16-V5-M1)."""
    from botocore.exceptions import ClientError

    from app.providers.bedrock_adapter import BedrockAdapter

    def bad_iter():
        yield {"chunk": {"bytes": b'{"type":"message_start"}'}}
        raise ClientError(
            {
                "Error": {
                    "Code": "modelStreamErrorException",
                    "Message": "boom arn:aws:bedrock:us-east-1:123456789012:model/x",
                }
            },
            "InvokeModelWithResponseStream",
        )

    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._bedrock_stream_gen(bad_iter())]
    assert len(received) == 2
    err = json.loads(received[1])
    assert err["type"] == "error"
    assert err["error"]["type"] == "modelStreamErrorException"
    assert "arn:aws" not in err["error"]["message"]
    assert "123456789012" not in err["error"]["message"]


@pytest.mark.asyncio
async def test_converse_stream_gen_converts_eventstream_error_to_union_member():
    """converse-stream 은 예외를 유니온 멤버 모양 {code: {message}} 로 합성한다."""
    from botocore.exceptions import ClientError

    from app.providers.bedrock_adapter import BedrockAdapter

    def bad_iter():
        yield {"messageStart": {"role": "assistant"}}
        raise ClientError(
            {
                "Error": {
                    "Code": "throttlingException",
                    "Message": "slow down arn:aws:x:123456789012",
                }
            },
            "ConverseStream",
        )

    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._converse_stream_gen(bad_iter())]
    assert len(received) == 2
    payload = json.loads(received[1])
    assert "throttlingException" in payload
    assert "arn:aws" not in payload["throttlingException"]["message"]


@pytest.mark.asyncio
async def test_bedrock_stream_gen_skips_non_chunk_non_error_members():
    """metadata 같은 비오류 비-chunk 멤버는 기존처럼 건너뛴다."""
    from app.providers.bedrock_adapter import BedrockAdapter

    events = [
        {"chunk": {"bytes": b'{"type":"message_start"}'}},
        {"metadata": {"model": "x"}},
        {"chunk": {"bytes": b'{"type":"message_stop"}'}},
    ]
    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._bedrock_stream_gen(iter(events))]
    assert received == [b'{"type":"message_start"}', b'{"type":"message_stop"}']


# ─────────────────────────────────────────────────────────────────────────────
# 5. web_search_loop 비스트림 early-return — 예약 해제 콜백
# ─────────────────────────────────────────────────────────────────────────────
class _FailMcp:
    async def ensure_initialized(self) -> str:
        raise RuntimeError("handshake failed")


@pytest.mark.asyncio
async def test_ws_mcp_fallback_nonstream_fires_on_usage_on_provider_error():
    """MCP 핸드셰이크 실패 → no-search 폴백이 상류 비-2xx 를 받아도
    on_usage(0토큰) 를 발화해 입장 예약이 해제된다.

    구형은 usage>0 일 때만 발화해 보수적 예약(max_iterations 배)이 누수됐다.
    """
    calls: list[TokenUsage] = []

    async def on_usage(usage):
        calls.append(usage)

    async def invoke(_body):
        # 상류 거절 — usage 없음
        return 400, b'{"error":{"type":"invalid_request"}}', {}, TokenUsage()

    resp = await wsl.run_web_search_loop(
        dialect="anthropic",
        invoke=invoke,
        invoke_stream=AsyncMock(),
        initial_req_data={"messages": [{"role": "user", "content": "hi"}]},
        is_stream=False,
        mcp_client=_FailMcp(),
        request=FakeRequest(),
        on_usage=on_usage,
        handshake_timeout=1.0,
    )
    assert resp.status_code == 400
    assert len(calls) == 1, (
        f"비-2xx 경로가 on_usage 를 발화하지 않아 예약이 누수된다: {len(calls)}회"
    )
    assert calls[0].total_tokens == 0


@pytest.mark.asyncio
async def test_ws_client_owned_tool_nonstream_fires_on_usage_on_error():
    """F-7 클라이언트 소유 web_search 툴 패스스루도 같은 계약."""
    calls: list[TokenUsage] = []

    async def on_usage(usage):
        calls.append(usage)

    async def invoke(_body):
        return 500, b'{"error":{}}', {}, TokenUsage()

    resp = await wsl.run_web_search_loop(
        dialect="anthropic",
        invoke=invoke,
        invoke_stream=AsyncMock(),
        # 클라이언트 자체 web_search 툴 선언 → F-7 패스스루 분기
        initial_req_data={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "web_search", "description": "client's own"}],
        },
        is_stream=False,
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
    )
    assert resp.status_code == 500
    assert len(calls) == 1
    assert calls[0].total_tokens == 0


@pytest.mark.asyncio
async def test_ws_invoke_exception_releases_via_on_usage_then_reraises():
    """어댑터 계약 밖 예외도 on_usage(0) 발화 후 재전파 — 예약 누수 방지."""
    calls: list[TokenUsage] = []

    async def on_usage(usage):
        calls.append(usage)

    async def invoke(_body):
        raise ValueError("contract-out adapter bug")

    with pytest.raises(ValueError):
        await wsl.run_web_search_loop(
            dialect="anthropic",
            invoke=invoke,
            invoke_stream=AsyncMock(),
            initial_req_data={"messages": [{"role": "user", "content": "hi"}]},
            is_stream=False,
            mcp_client=_FailMcp(),
            request=FakeRequest(),
            on_usage=on_usage,
            handshake_timeout=1.0,
        )
    assert len(calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. /model/* 패스스루 — disconnect 드레인 + 단일 정산
# ─────────────────────────────────────────────────────────────────────────────
def _make_router_request(chunks):
    """stream_with_cost 를 끝까지 태울 수 있는 최소 요청 목."""
    from app.routers import bedrock as bedrock_mod

    state = {
        "auth_context": SimpleNamespace(
            user_id="u1", team_id="t1", sso_subject=None
        ),
        "_redis_effective": None,
        "_session_factory": None,
        "request_id": "req-stage4",
        "request_start_time": time.monotonic(),
        "client": "claude-code",
    }
    request = MagicMock()
    request.scope = {"state": state}
    request.body = AsyncMock(return_value=b'{"messages":[]}')

    finalize_calls: list[tuple[tuple, dict]] = []

    class _Recorder:
        async def finalize(self, *args, **kwargs):
            finalize_calls.append((args, kwargs))

    class _Adapter:
        async def invoke_stream(self, body, model_id, **kwargs):
            async def _gen():
                for c in chunks:
                    yield c

            return 200, _gen(), {"Content-Type": "application/vnd.amazon.eventstream"}, "req-1"

    registry = SimpleNamespace(get=lambda _pt: _Adapter())
    request.app = SimpleNamespace(
        state=SimpleNamespace(
            provider_registry=registry,
            cost_recorder=_Recorder(),
            metrics=None,
        )
    )
    model_config = SimpleNamespace(
        provider_model_id="anthropic.test-v1",
        alias="test-alias",
        provider=bedrock_mod.ProviderType.BEDROCK,
    )
    return bedrock_mod, request, model_config, finalize_calls


def _wire_router_mocks(bedrock_mod, monkeypatch, model_config):
    """resolve/scope/enforce 를 목으로 — 스트림 계약만 검증한다."""
    svc = bedrock_mod._router_service
    monkeypatch.setattr(
        svc, "resolve_bedrock_model",
        AsyncMock(return_value=model_config),
    )
    monkeypatch.setattr(svc, "check_key_scope", lambda *a, **k: None)
    monkeypatch.setattr(svc, "check_client_model_scope", lambda *a, **k: None)
    import app.services.rate_limit_enforcement as rle

    monkeypatch.setattr(rle, "enforce_rate_limits", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_passthrough_disconnect_drains_remaining_usage(monkeypatch):
    """클라이언트 끊김 후에도 드레인이 종결 usage 까지 스캔해 정산한다.

    구형은 finally 가 끊긴 시점까지의 usage 로만 정산했다 — converse-stream 은
    usage 가 종결 metadata 에만 와서 disconnect 시 0 토큰 정산(무료 생성)이었다.
    """
    chunks = [
        _b({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _b({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}),
        _b({"type": "message_delta", "usage": {"output_tokens": 7}}),
        _b({"type": "message_stop",
            "amazon-bedrock-invocationMetrics": {"inputTokenCount": 10,
                                                 "outputTokenCount": 42}}),
    ]
    bedrock_mod, request, model_config, finalize_calls = _make_router_request(chunks)
    _wire_router_mocks(bedrock_mod, monkeypatch, model_config)

    resp = await bedrock_mod._handle_bedrock(
        request, "test-alias", "invoke-with-response-stream", stream=True
    )
    # 클라이언트가 첫 청크만 받고 끊는다
    it = resp.body_iterator
    await it.__anext__()
    await it.aclose()
    await drain_pending_tasks(timeout=2.0)

    assert len(finalize_calls) == 1, f"정산 {len(finalize_calls)}회 — 유실/이중 정산"
    args, kwargs = finalize_calls[0]
    usage = args[3]  # finalize(redis, auth, model_config, usage, ...)
    # invocationMetrics 까지 드레인됐으므로 최종 authoritative 값
    assert usage.output_tokens == 42, f"드레인이 종결 usage 를 못 잡았다: {usage}"
    assert usage.input_tokens == 10
    assert kwargs["bedrock_request_id"] == "req-1"


@pytest.mark.asyncio
async def test_passthrough_normal_end_finalizes_once_with_authoritative_usage(
    monkeypatch,
):
    """정상 소비 — invocationMetrics 의 최종값으로 정확히 1회 정산."""
    chunks = [
        _b({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _b({"type": "message_delta", "usage": {"output_tokens": 7}}),
        _b({"type": "message_stop",
            "amazon-bedrock-invocationMetrics": {"inputTokenCount": 10,
                                                 "outputTokenCount": 42}}),
    ]
    bedrock_mod, request, model_config, finalize_calls = _make_router_request(chunks)
    _wire_router_mocks(bedrock_mod, monkeypatch, model_config)

    resp = await bedrock_mod._handle_bedrock(
        request, "test-alias", "invoke-with-response-stream", stream=True
    )
    async for _ in resp.body_iterator:
        pass
    assert len(finalize_calls) == 1
    args, _kwargs = finalize_calls[0]
    usage = args[3]
    assert (usage.input_tokens, usage.output_tokens) == (10, 42)
