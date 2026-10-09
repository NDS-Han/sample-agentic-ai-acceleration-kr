# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`_scan_bedrock_stream_chunk` — /model/* 패스스루 스트림 usage 추출.

배경: stream_with_cost 가 OpenAI SSE 전용 파서만 써서 invoke-with-response-stream
(Anthropic 이벤트 JSON)과 converse-stream(Converse `metadata.usage`)의 usage 가
영원히 None 이었다 — zero-usage 가드가 XADD 를 생략해 usage_logs 행조차 없는
무료 스트리밍이었다.
"""

from __future__ import annotations

import json

from app.routers.bedrock import _scan_bedrock_stream_chunk


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class TestInvokeWithResponseStream:
    """Anthropic 이벤트 JSON (chunk["bytes"])."""

    def test_message_start_sets_input_and_cache(self):
        state: dict = {}
        _scan_bedrock_stream_chunk(
            _b(
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 1500,
                            "output_tokens": 1,
                            "cache_creation_input_tokens": 800,
                            "cache_read_input_tokens": 400,
                        }
                    },
                }
            ),
            "invoke-with-response-stream",
            state,
        )
        assert state["hit"] is True
        assert state["input_tokens"] == 1500
        assert state["cache_write"] == 800
        assert state["cache_read"] == 400

    def test_message_delta_updates_output(self):
        state: dict = {}
        _scan_bedrock_stream_chunk(
            _b(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 100, "output_tokens": 1}},
                }
            ),
            "invoke-with-response-stream",
            state,
        )
        _scan_bedrock_stream_chunk(
            _b({"type": "message_delta", "usage": {"output_tokens": 42}}),
            "invoke-with-response-stream",
            state,
        )
        assert state["output_tokens"] == 42
        assert state["input_tokens"] == 100

    def test_invocation_metrics_is_authoritative(self):
        state: dict = {}
        _scan_bedrock_stream_chunk(
            _b(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 999, "output_tokens": 1}},
                }
            ),
            "invoke-with-response-stream",
            state,
        )
        _scan_bedrock_stream_chunk(
            _b(
                {
                    "type": "message_stop",
                    "amazon-bedrock-invocationMetrics": {
                        "inputTokenCount": 1200,
                        "outputTokenCount": 77,
                        "cacheReadInputTokenCount": 300,
                        "cacheWriteInputTokenCount": 500,
                    },
                }
            ),
            "invoke-with-response-stream",
            state,
        )
        assert state["input_tokens"] == 1200
        assert state["output_tokens"] == 77
        assert state["cache_read"] == 300
        assert state["cache_write"] == 500

    def test_non_usage_events_ignored(self):
        state: dict = {}
        _scan_bedrock_stream_chunk(
            _b({"type": "content_block_delta", "delta": {"text": "hi"}}),
            "invoke-with-response-stream",
            state,
        )
        assert not state.get("hit")


class TestConverseStream:
    """Converse 이벤트 JSON — metadata.usage camelCase."""

    def test_metadata_usage(self):
        state: dict = {}
        _scan_bedrock_stream_chunk(
            _b({"contentBlockDelta": {"delta": {}}}), "converse-stream", state
        )
        assert not state.get("hit")

        _scan_bedrock_stream_chunk(
            _b(
                {
                    "metadata": {
                        "usage": {
                            "inputTokens": 900,
                            "outputTokens": 60,
                            "totalTokens": 960,
                            "cacheReadInputTokens": 200,
                            "cacheWriteInputTokens": 100,
                        },
                        "metrics": {"latencyMs": 1234},
                    }
                }
            ),
            "converse-stream",
            state,
        )
        assert state["hit"] is True
        assert state["input_tokens"] == 900
        assert state["output_tokens"] == 60
        assert state["cache_read"] == 200
        assert state["cache_write"] == 100


def test_malformed_chunk_ignored():
    state: dict = {}
    _scan_bedrock_stream_chunk(b"\x00\xff not json", "converse-stream", state)
    _scan_bedrock_stream_chunk(b'"just a string"', "converse-stream", state)
    assert not state.get("hit")
