# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Stage 5 회귀: 비용 파이프라인 — 요청 종결 상태 → usage_logs.status 전파.

배경: 워커의 usage_logs writer 가 status 를 "SUCCESS" 로 하드코딩해 monitoring 의
ERROR/TIMEOUT 집계가 영구 0%였다. 스트림 헬퍼 → outcome dict → 라우터 →
finalize → CostStreamEntry → 워커 INSERT 까지 상태가 끊기지 않아야 한다.

커버하는 계약:
1. usage_status_of / mark_outcome / outcome_from_http 판정 우선순위.
2. 200 스트림 내부 error 이벤트가 outcome "error" 로 기록되고 정상 종료가 그것을
   덮지 않는다(setdefault).
3. idle timeout 은 "timeout", 클라이언트 disconnect 는 "success"(과금된 정상 건).
4. finalize 의 zero-usage 분기 — 실패 종결은 0 토큰 마커 행을 발행하고 성공은
   발행하지 않는다.
5. enum 밖 status 값은 poison retry 루프 방지로 ERROR 로 정규화된다.
6. web_search_loop — 비-200 턴/예외/스트림 내 오류가 outcome "error" 로,
   정상 루프와 disconnect 가 "success" 로 기록된다.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import TokenUsage
from app.services import web_search_loop as wsl
from app.services.cost_recorder import (
    CostRecorder,
    _normalize_usage_status,
)
from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    drain_pending_tasks,
    mark_outcome,
    outcome_from_http,
    responses_sse_stream,
    usage_status_of,
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
# 1. usage_status_of / outcome_from_http 판정
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "outcome, http_status, expected",
    [
        (None, None, "SUCCESS"),
        ({"status": "success"}, 200, "SUCCESS"),
        ({"status": "error"}, 200, "ERROR"),        # 200 내부 오류 — http 무관
        ({"status": "timeout"}, 200, "TIMEOUT"),
        (None, 500, "ERROR"),
        (None, 400, "ERROR"),
        (None, 408, "TIMEOUT"),
        (None, 504, "TIMEOUT"),
        ({"status": "success"}, 500, "ERROR"),      # http 실패가 성공 마킹보다 우선
        ({"status": "error"}, 504, "TIMEOUT"),      # 504 는 TIMEOUT 으로 귀속
        ({"status": "timeout"}, 500, "TIMEOUT"),    # outcome 실패 판정 우선
        ({}, 200, "SUCCESS"),                        # 빈 outcome dict
    ],
)
def test_usage_status_of_precedence(outcome, http_status, expected):
    assert usage_status_of(outcome, http_status) == expected


@pytest.mark.parametrize(
    "http_status, failed, expected",
    [
        (200, False, "success"),
        (None, True, "error"),
        (500, False, "error"),
        (408, False, "timeout"),
        (504, True, "timeout"),
        (200, True, "error"),
    ],
)
def test_outcome_from_http_mapping(http_status, failed, expected):
    outcome: dict = {}
    outcome_from_http(outcome, http_status, failed)
    assert outcome["status"] == expected


def test_outcome_from_http_preserves_prior_error():
    """성공 종결이 선행 실패 마킹을 덮지 않는다(스트림 내 error 후 정상 종료)."""
    outcome: dict = {"status": "error"}
    outcome_from_http(outcome, 200, failed=False)
    assert outcome["status"] == "error"


def test_outcome_from_http_none_is_noop():
    outcome_from_http(None, 500, failed=True)  # 예외 없이 통과


def test_mark_outcome_none_is_noop():
    mark_outcome(None, "error")  # 예외 없이 통과


# ─────────────────────────────────────────────────────────────────────────────
# 2. 스트림 헬퍼 — 종결 경로별 outcome 마킹
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_anthropic_stream_midstream_error_marks_outcome():
    """200 스트림 내부 error 이벤트 → outcome 'error'. 정상 종료가 덮지 않는다."""
    outcome: dict = {}
    chunks = [
        _b({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _b({"type": "error", "error": {"type": "api_error", "message": "boom"}}),
        _b({"type": "message_stop"}),
    ]
    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), idle_timeout=5.0, outcome=outcome
    ):
        pass
    assert outcome["status"] == "error"
    assert usage_status_of(outcome) == "ERROR"


@pytest.mark.asyncio
async def test_anthropic_stream_error_dict_chunk_marks_outcome():
    """어댑터의 {"error":{...}} 단일 형태도 error 로 마킹된다."""
    outcome: dict = {}
    chunks = [
        _b({"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _b({"error": {"type": "provider_error", "message": "broke"}}),
    ]
    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), idle_timeout=5.0, outcome=outcome
    ):
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_anthropic_stream_idle_timeout_marks_timeout():
    """상류 idle timeout → outcome 'timeout' → TIMEOUT."""
    outcome: dict = {}

    async def stalling():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 5}}})
        await asyncio.Event().wait()

    frames = [
        f
        async for f in bedrock_anthropic_sse_stream(
            FakeRequest(), stalling(), idle_timeout=0.05, outcome=outcome
        )
    ]
    assert outcome["status"] == "timeout"
    assert usage_status_of(outcome) == "TIMEOUT"
    assert any(b"timeout_error" in f for f in frames)


@pytest.mark.asyncio
async def test_anthropic_stream_disconnect_is_success():
    """클라이언트 끊김 → 게이트웨이/상류 실패가 아니라 과금된 정상 건 → SUCCESS."""
    outcome: dict = {}
    calls: list[TokenUsage] = []

    async def on_usage(usage, _ttft=None):
        calls.append(usage)

    async def gen_chunks():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        yield _b({"type": "message_delta", "usage": {"output_tokens": 7}})
        yield _b({"type": "message_stop"})

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), gen_chunks(), on_usage=on_usage,
        idle_timeout=5.0, drain_timeout=0.2, outcome=outcome,
    )
    async for _ in gen:
        break
    await gen.aclose()          # 클라이언트 끊김
    await drain_pending_tasks(timeout=2.0)

    assert outcome["status"] == "success"
    assert usage_status_of(outcome) == "SUCCESS"


@pytest.mark.asyncio
async def test_anthropic_stream_disconnect_after_error_stays_error():
    """끊기기 전에 받은 상류 error 이벤트는 disconnect 가 성공으로 덮지 못한다."""
    outcome: dict = {}

    async def gen_chunks():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
        yield _b({"type": "error", "error": {"type": "api_error", "message": "x"}})
        yield _b({"type": "message_stop"})

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), gen_chunks(), idle_timeout=5.0,
        drain_timeout=0.2, outcome=outcome,
    )
    async for _ in gen:
        break  # error 프레임까지는 소비됨 — 그다음 끊김
    await gen.aclose()
    await drain_pending_tasks(timeout=2.0)
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_anthropic_stream_upstream_exception_marks_error():
    """이터레이터가 던지는 상류 예외 → outcome 'error'."""
    outcome: dict = {}

    async def exploding():
        yield _b({"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
        raise ConnectionError("upstream died")

    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(), exploding(), idle_timeout=5.0, outcome=outcome
    ):
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_responses_stream_failed_terminal_marks_error():
    """response.failed 종결 이벤트 → outcome 'error'."""
    outcome: dict = {}
    chunks = [
        _b({"type": "response.failed",
            "response": {"status": "failed",
                         "usage": {"input_tokens": 3, "output_tokens": 2}}}),
    ]
    async for _ in responses_sse_stream(
        FakeRequest(), _aiter(chunks), idle_timeout=5.0, outcome=outcome
    ):
        pass
    assert outcome["status"] == "error"
    assert usage_status_of(outcome) == "ERROR"


@pytest.mark.asyncio
async def test_responses_stream_normal_completion_is_success():
    outcome: dict = {}
    chunks = [
        _b({"type": "response.output_text.delta", "delta": "hi"}),
        _b({"type": "response.completed",
            "response": {"status": "completed",
                         "usage": {"input_tokens": 3, "output_tokens": 2}}}),
    ]
    async for _ in responses_sse_stream(
        FakeRequest(), _aiter(chunks), idle_timeout=5.0, outcome=outcome
    ):
        pass
    assert outcome["status"] == "success"
    assert usage_status_of(outcome) == "SUCCESS"


# ─────────────────────────────────────────────────────────────────────────────
# 3. cost_recorder.finalize — zero-usage 분기와 status 정규화
# ─────────────────────────────────────────────────────────────────────────────
def test_normalize_usage_status():
    assert _normalize_usage_status("SUCCESS") == "SUCCESS"
    assert _normalize_usage_status("error") == "ERROR"
    assert _normalize_usage_status(None) == "SUCCESS"
    assert _normalize_usage_status("PARTIAL") == "ERROR"   # enum 밖 → ERROR
    assert _normalize_usage_status("garbage") == "ERROR"


@pytest.mark.asyncio
async def test_finalize_zero_usage_failure_publishes_marker_row(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """0 토큰 실패 종결 → monitoring 이 읽을 ERROR 마커 행을 발행한다."""
    import json as _json

    mock_redis.xadd = AsyncMock()
    recorder = CostRecorder()

    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(),
        request_id="req-fail",
        is_stream=False,
        duration_ms=120,
        status="ERROR",
        rate_limit_state=None,
    )
    mock_redis.xadd.assert_awaited_once()
    fields = mock_redis.xadd.call_args.args[1]
    payload = _json.loads(fields["payload"])
    assert payload["status"] == "ERROR"
    assert payload["input_tokens"] == 0
    assert payload["cost_usd"] == "0"


@pytest.mark.asyncio
async def test_finalize_zero_usage_success_publishes_nothing(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """0 토큰 성공 종결 → 예전처럼 행을 만들지 않는다(마커 행 오염 방지)."""
    mock_redis.xadd = AsyncMock()
    recorder = CostRecorder()

    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(),
        request_id="req-empty",
        is_stream=False,
        duration_ms=100,
        status="SUCCESS",
        rate_limit_state=None,
    )
    mock_redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_nonzero_usage_carries_status(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """토큰 있는 실패/타임아웃 종결 → status 가 XADD payload 까지 간다."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"
    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()
    recorder = CostRecorder()

    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        request_id="req-partial-fail",
        is_stream=True,
        duration_ms=300,
        status="TIMEOUT",
    )
    fields = mock_redis.xadd.call_args.args[1]
    payload = _json.loads(fields["payload"])
    assert payload["status"] == "TIMEOUT"


# ─────────────────────────────────────────────────────────────────────────────
# 4. web_search_loop — 멀티턴 종결 상태
# ─────────────────────────────────────────────────────────────────────────────
def _anthropic_turn(text: str = "", usage_in: int = 10, usage_out: int = 5):
    """test_web_search_loop 과 같은 형태의 단일 턴(검색 없이 종결)."""
    events = [
        {"type": "message_start",
         "message": {"id": "msg_x", "type": "message", "role": "assistant",
                     "content": [],
                     "usage": {"input_tokens": usage_in, "output_tokens": 0}}},
    ]
    if text:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": usage_out}},
        {"type": "message_stop"},
    ]
    return [_b(e) for e in events]


@pytest.mark.asyncio
async def test_ws_anthropic_stream_normal_end_marks_success():
    outcome: dict = {}
    usages: list[TokenUsage] = []

    async def on_usage(u):
        usages.append(u)

    async def invoke_stream(_body):
        return 200, _aiter(_anthropic_turn("done")), {}, None

    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "success"
    assert usages and usages[0].output_tokens == 5


@pytest.mark.asyncio
async def test_ws_anthropic_stream_non200_turn_marks_error():
    """멀티턴 중간 상류 비-200 → outcome 'error'(0 토큰이어도 마커는 나간다)."""
    outcome: dict = {}
    usages: list[TokenUsage] = []

    async def on_usage(u):
        usages.append(u)

    calls = {"n": 0}

    async def invoke_stream(_body):
        calls["n"] += 1
        if calls["n"] == 1:
            # 첫 턴: 검색 요청 tool_use
            turn = [
                _b({"type": "message_start",
                    "message": {"id": "m1", "type": "message", "role": "assistant",
                                "content": [],
                                "usage": {"input_tokens": 10, "output_tokens": 0}}}),
                _b({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "tu_1",
                                      "name": "web_search", "input": {}}}),
                _b({"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta",
                              "partial_json": '{"query":"x"}'}}),
                _b({"type": "content_block_stop", "index": 0}),
                _b({"type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5}}),
                _b({"type": "message_stop"}),
            ]
            return 200, _aiter(turn), {}, None
        # 두 번째 턴: 상류 500
        return 500, _aiter([_b({"type": "error",
                                "error": {"type": "api_error", "message": "boom"}})]), {}, None

    from app.services.agentcore_mcp_client import WebSearchResponse

    class _Mcp:
        async def search(self, query, max_results=10):
            return WebSearchResponse(results=[], raw_text="result")

    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "error"
    # 1턴 사용량은 계상되고 종결은 실패 — 과금과 상태가 동시에 진실이다
    assert usages and usages[0].input_tokens == 10


@pytest.mark.asyncio
async def test_ws_anthropic_stream_midstream_error_event_marks_error():
    """200 스트림 안의 error 이벤트(error_seen) → outcome 'error'."""
    outcome: dict = {}

    async def on_usage(u):
        pass

    chunks = [
        _b({"type": "message_start",
            "message": {"id": "m", "type": "message", "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        _b({"type": "error", "error": {"type": "api_error", "message": "mid"}}),
    ]

    async def invoke_stream(_body):
        return 200, _aiter(chunks), {}, None

    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_ws_anthropic_stream_exception_marks_error():
    """invoke_stream 계약 밖 예외 → outcome 'error'."""
    outcome: dict = {}

    async def on_usage(u):
        pass

    async def invoke_stream(_body):
        raise RuntimeError("adapter contract violation")

    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_ws_anthropic_stream_truncated_turn_marks_error():
    """종결 프레임(message_delta) 없이 끊긴 상류 → '성공'이 아니라 'error'.

    saw_message_delta=False 경로가 error_seen 도 아니고 예외도 아니어서
    구형은 SUCCESS 로 오기록했다 — 클라이언트에는 error 프레임이 나가는데
    usage_logs/monitoring 은 성공으로 본다.
    """
    outcome: dict = {}

    async def on_usage(u):
        pass

    chunks = [
        _b({"type": "message_start",
            "message": {"id": "m", "type": "message", "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        # message_delta/message_stop 없이 스트림 종료 — 소켓 절단.
    ]

    async def invoke_stream(_body):
        return 200, _aiter(chunks), {}, None

    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_ws_responses_stream_truncated_turn_marks_error():
    """상류가 response.completed 를 보내지 않고 끊김 → 합성 incomplete 는 실패.

    진짜 상류 response.incomplete(max_tokens 유효 종결)는 SUCCESS 이고,
    게이트웨이가 합성한 incomplete(무종결 절단)는 ERROR 다 — 둘을 구분해야
    한다(E2E S5).
    """
    outcome: dict = {}

    async def on_usage(u):
        pass

    chunks = [
        _b({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}}),
        _b({"type": "response.output_text.delta", "delta": "partial"}),
        # terminal event 없이 종료 — 상류 절단.
    ]

    async def invoke_stream(_body):
        return 200, _aiter(chunks), {}, None

    gen = wsl._responses_stream(
        invoke_stream=invoke_stream,
        base_body={"input": "hi"},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "error"


@pytest.mark.asyncio
async def test_ws_responses_stream_real_incomplete_is_success():
    """상류가 보낸 진짜 response.incomplete — 유효 종결 → SUCCESS."""
    outcome: dict = {}

    async def on_usage(u):
        pass

    chunks = [
        _b({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}}),
        _b({"type": "response.output_text.delta", "delta": "partial"}),
        _b({"type": "response.incomplete",
            "response": {"id": "r1", "status": "incomplete",
                         "usage": {"input_tokens": 5, "output_tokens": 3}}}),
    ]

    async def invoke_stream(_body):
        return 200, _aiter(chunks), {}, None

    gen = wsl._responses_stream(
        invoke_stream=invoke_stream,
        base_body={"input": "hi"},
        mcp_client=AsyncMock(),
        request=FakeRequest(),
        on_usage=on_usage,
        max_iterations=5,
        deadline=time.monotonic() + 60,
        default_max_results=10,
        outcome=outcome,
    )
    async for _ in gen:
        pass
    assert outcome["status"] == "success"


@pytest.mark.asyncio
async def test_ws_nonstream_passthrough_marks_error_on_non200():
    """MCP 실패 → no-search 패스스루가 상류 비-2xx 를 받으면 outcome 'error'."""
    outcome: dict = {}
    usages: list[TokenUsage] = []

    async def on_usage(u):
        usages.append(u)

    class _FailMcp:
        async def ensure_initialized(self):
            raise RuntimeError("handshake failed")

    async def invoke(_body):
        return 500, b'{"error":{"type":"server"}}', {}, TokenUsage()

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
        outcome=outcome,
    )
    assert resp.status_code == 500
    assert outcome["status"] == "error"
    assert usage_status_of(outcome, 500) == "ERROR"
    assert len(usages) == 1  # 예약 해제용 0 토큰 발화는 유지


@pytest.mark.asyncio
async def test_ws_nonstream_passthrough_marks_success_on_2xx():
    outcome: dict = {}

    async def on_usage(u):
        pass

    class _FailMcp:
        async def ensure_initialized(self):
            raise RuntimeError("handshake failed")

    body = (
        b'{"content":[{"type":"text","text":"hi"}],"usage":{"input_tokens":1,'
        b'"output_tokens":1}}'
    )

    async def invoke(_body):
        return 200, body, {}, TokenUsage(
            input_tokens=1, output_tokens=1, total_tokens=2
        )

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
        outcome=outcome,
    )
    assert resp.status_code == 200
    assert outcome["status"] == "success"


@pytest.mark.asyncio
async def test_ws_nonstream_passthrough_marks_error_on_invoke_exception():
    """어댑터 계약 밖 예외 → outcome 'error' + 예약 해제 콜백 후 재전파."""
    outcome: dict = {}
    usages: list[TokenUsage] = []

    async def on_usage(u):
        usages.append(u)

    class _FailMcp:
        async def ensure_initialized(self):
            raise RuntimeError("handshake failed")

    async def invoke(_body):
        raise ValueError("contract-out bug")

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
            outcome=outcome,
        )
    assert outcome["status"] == "error"
    assert len(usages) == 1
