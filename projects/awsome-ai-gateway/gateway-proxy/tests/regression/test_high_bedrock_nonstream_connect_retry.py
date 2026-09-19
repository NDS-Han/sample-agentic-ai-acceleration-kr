"""A dead pooled connection must not turn a NON-streaming request into a 502.

2026-09-19 US dev (gateway log, 13:07Z). The pod had been idle for ~2.5 hours; the kept-alive
connections to Bedrock had been closed by the network path in the meantime. The next two
requests picked dead connections:

    13:07:41  bedrock_invoke_failed          -> POST /v1/messages 502   (non-streaming)
    13:07:43  bedrock_stream_connect_retry   -> retried, 200            (streaming)

``invoke_stream`` has retried a connection-level error once since 2026-09-16; ``invoke`` never
got the same treatment, and ``bedrock_max_attempts`` is 1, so botocore does not retry either.
The pool reuses the most recently used connections first, so the others go stale while the
gateway is busy and come out together when requests pile up — more users make it more frequent,
not less. Streaming clients never saw it; API callers, batch jobs and some sub-agents do.

Contract (same rule as the streaming path):
- a connection-level error (``ConnectionClosedError`` / ``EndpointConnectionError``) is retried
  ONCE — nothing reached Bedrock, so nothing is billed twice;
- a second connection error gives up with 502 (no retry storm);
- an error Bedrock actually answered with (``ClientError``: 400/429/5xx) is NOT retried;
- ``converse`` and ``count_tokens`` follow the same rule.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError

from app.providers.bedrock_adapter import BedrockAdapter

OK_BODY = json.dumps({"content": [{"type": "text", "text": "hi"}],
                      "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()


def _ok(request_id="req-2"):
    return {"ResponseMetadata": {"RequestId": request_id}, "body": io.BytesIO(OK_BODY)}


def _closed():
    return ConnectionClosedError(endpoint_url="https://bedrock")


async def test_invoke_retries_once_on_connection_closed():
    client = MagicMock()
    client.invoke_model.side_effect = [_closed(), _ok()]
    status, body, headers, usage = await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert status == 200 and body == OK_BODY
    assert client.invoke_model.call_count == 2
    assert usage.input_tokens == 3 and usage.output_tokens == 2
    assert headers.get("x-amzn-requestid") == "req-2", "the retry's request id is the audit key"


async def test_invoke_retries_once_on_endpoint_connection_error():
    client = MagicMock()
    client.invoke_model.side_effect = [EndpointConnectionError(endpoint_url="https://bedrock"),
                                       _ok()]
    status, _body, _h, _u = await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert status == 200 and client.invoke_model.call_count == 2


async def test_invoke_gives_up_after_the_second_connection_error():
    client = MagicMock()
    client.invoke_model.side_effect = _closed()
    status, body, _h, usage = await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert status == 502 and client.invoke_model.call_count == 2, "exactly one retry, no storm"
    assert b"provider_error" in body and usage.input_tokens == 0


async def test_invoke_does_not_retry_an_error_bedrock_answered_with():
    client = MagicMock()
    client.invoke_model.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeModel")
    status, body, _h, _u = await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert client.invoke_model.call_count == 1, "Bedrock replied — a retry could bill twice"
    assert status == 429 and b"provider_error" in body


async def test_converse_retries_once_on_connection_closed():
    client = MagicMock()
    client.converse.side_effect = [
        _closed(),
        {"ResponseMetadata": {"RequestId": "req-c"},
         "usage": {"inputTokens": 4, "outputTokens": 1}},
    ]
    status, _body, _h, usage = await BedrockAdapter(client).invoke(
        json.dumps({"messages": []}).encode(), "model-id", path_suffix="converse")
    assert status == 200 and client.converse.call_count == 2
    assert (usage.input_tokens, usage.output_tokens) == (4, 1)


async def test_count_tokens_retries_once_on_connection_closed():
    client = MagicMock()
    client.count_tokens.side_effect = [_closed(), {"inputTokens": 11}]
    status, n = await BedrockAdapter(client).count_tokens(b"{}", "model-id")
    assert (status, n) == (200, 11) and client.count_tokens.call_count == 2


async def test_idle_pooled_connections_are_dropped_before_the_retry():
    client = MagicMock()
    client.invoke_model.side_effect = [_closed(), _ok()]
    await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert client._endpoint.http_session.close.call_count == 1, \
        "the other pooled connections went idle together — the retry must not pick one"

    quiet = MagicMock()
    quiet.invoke_model.side_effect = [_ok()]
    await BedrockAdapter(quiet).invoke(b"{}", "model-id")
    assert quiet._endpoint.http_session.close.call_count == 0, "no reset without an error"


async def test_a_failing_pool_reset_does_not_cost_the_retry():
    client = MagicMock()
    client.invoke_model.side_effect = [_closed(), _ok()]
    client._endpoint.http_session.close.side_effect = RuntimeError("botocore internals changed")
    status, _b, _h, _u = await BedrockAdapter(client).invoke(b"{}", "model-id")
    assert status == 200 and client.invoke_model.call_count == 2
