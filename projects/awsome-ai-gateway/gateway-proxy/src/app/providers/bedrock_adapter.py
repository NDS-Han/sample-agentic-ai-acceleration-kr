# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError

from app.providers.base import ProviderAdapter
from app.schemas.domain import TokenUsage

_BEDROCK_POOL_SIZE = int(os.environ.get("BEDROCK_THREAD_POOL_SIZE", "128"))
_bedrock_executor = ThreadPoolExecutor(max_workers=_BEDROCK_POOL_SIZE, thread_name_prefix="bedrock")

logger = structlog.get_logger(__name__)

# boto3 예외 → HTTP 상태 코드 매핑
_BOTO_ERROR_MAP: dict[str, int] = {
    "ValidationException": 400,
    "AccessDeniedException": 403,
    "ResourceNotFoundException": 404,
    "ThrottlingException": 429,
    "ModelTimeoutException": 504,
    "ServiceException": 502,
    "InternalServerException": 502,
}


def _extract_bedrock_usage(response_body: dict) -> TokenUsage:
    """Bedrock invoke/converse 응답에서 TokenUsage 추출 (cache 토큰 포함)."""
    usage = response_body.get("usage", {})
    input_tokens = usage.get("input_tokens", usage.get("inputTokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("outputTokens", 0))
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _request_id_headers(response: dict) -> dict:
    """``{"x-amzn-requestid": <id>}`` from a boto3 response, or ``{}`` if absent.

    Empty dict rather than ``{"x-amzn-requestid": None}`` on purpose: the caller does
    ``headers.get("x-amzn-requestid")`` and stores the result, and a NULL column means
    "no record to join to". A key present with a None value would be indistinguishable
    downstream but would make any future ``in headers`` check lie.
    """
    rid = (response or {}).get("ResponseMetadata", {}).get("RequestId")
    return {"x-amzn-requestid": rid} if rid else {}


class BedrockAdapter(ProviderAdapter):
    """AWS Bedrock Runtime adapter (boto3 기반).

    두 모드:
    - in-account(기본): bedrock_client 고정. resolver/fallback None → 기존과 동일 동작.
    - cross-account(claude-code→374): client_resolver = async () -> boto3 client
      (STS assume 후 대상 계정 bedrock-runtime). assume/build 실패 시 fallback_client
      (in-account 859)로 **투명 폴백** → claude-code 절대 안 죽음(사용자 결정).
    """

    def __init__(self, bedrock_client, client_resolver=None, fallback_client=None) -> None:
        self._client = bedrock_client
        self._client_resolver = client_resolver
        self._fallback_client = fallback_client

    async def _get_client(self):
        """resolver 있으면 cross-account 클라이언트, 실패 시 in-account fallback."""
        if self._client_resolver is None:
            return self._client
        try:
            return await self._client_resolver()
        except Exception:
            logger.warning("bedrock_xacct_resolve_failed_fallback_inaccount", exc_info=True)
            if self._fallback_client is not None:
                return self._fallback_client
            raise

    async def _call_with_connect_retry(self, client, executor, fn, *, model_id: str,
                                       event: str = "bedrock_connect_retry"):
        """Run one blocking boto call; on a CONNECTION-level error retry it once.

        Why: the gateway keeps its connections to Bedrock alive, and the network path drops
        idle ones silently (no FIN). The next request that picks such a connection fails with
        "Connection was closed before we received a valid response". ``bedrock_max_attempts``
        is 1, so botocore does not retry — the caller got a 502 (2026-09-16: three times in a
        day on the streaming path; 2026-09-19: a non-streaming request after 2.5 idle hours).
        The pool hands out the most recently used connections first, so the rest go stale
        while the gateway is busy and surface together when requests pile up: more users make
        this MORE frequent, not less.

        Rule:
        - only ``ConnectionClosedError`` / ``EndpointConnectionError`` — nothing reached
          Bedrock, so nothing is billed twice. An error Bedrock answered with (``ClientError``)
          is never retried here.
        - before the retry the client's idle pooled connections are dropped: they went idle
          together, so the next one from the pool would most likely be dead as well.
          In-flight requests keep their own connections. Best effort — botocore has no public
          API for it, and a failure to reset must not cost us the retry.
        - one retry only; the second error propagates (no retry storm).
        """
        import asyncio

        loop = asyncio.get_event_loop()
        for attempt in (1, 2):
            try:
                return await loop.run_in_executor(executor, fn)
            except (ConnectionClosedError, EndpointConnectionError) as exc:
                if attempt == 2:
                    raise
                logger.warning(event, model_id=model_id, error=str(exc)[:160])
                try:
                    client._endpoint.http_session.close()   # drop idle pooled connections
                except Exception:
                    logger.debug("bedrock_pool_reset_skipped", model_id=model_id)

    async def invoke(
        self, request_body: bytes, model_id: str, path_suffix: str = "invoke", **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        """Non-streaming Bedrock call.

        The returned headers dict carries ``x-amzn-requestid`` — the join key to this
        call's Bedrock model-invocation log record, persisted as
        ``usage_logs.bedrock_request_id``. ``invoke_stream`` has surfaced it since the
        streaming rewrite (see :meth:`invoke_stream`), but this branch dropped it, so
        every NON-streaming Claude call was unauditable: the record exists in AWS and
        nothing in our data pointed at it. Returned in the headers dict (rather than as
        a 5th tuple element) to keep the 4-tuple contract in ``ProviderAdapter`` and to
        match ``BedrockOpenAIAdapter``, which uses the same convention.

        Safe to put here because no route forwards this dict to the client — every caller
        (``routers/messages.py``, ``routers/bedrock.py``, ``routers/openai_compat.py``)
        constructs its own ``JSONResponse``/``StreamingResponse`` headers rather than
        passing this dict through, so the AWS id is not leaked outward.
        """
        try:
            client = await self._get_client()
            if path_suffix in ("invoke", ""):
                response = await self._call_with_connect_retry(
                    client, _bedrock_executor,
                    lambda: client.invoke_model(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                    model_id=model_id,
                )
                body = response["body"].read()
                try:
                    parsed = json.loads(body)
                    usage = _extract_bedrock_usage(parsed)
                except Exception:
                    usage = TokenUsage()
                return 200, body, _request_id_headers(response), usage

            elif path_suffix == "converse":
                parsed_req = json.loads(request_body)
                response = await self._call_with_connect_retry(
                    client, _bedrock_executor,
                    lambda: client.converse(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                    model_id=model_id,
                )
                usage = TokenUsage(
                    input_tokens=response.get("usage", {}).get("inputTokens", 0),
                    output_tokens=response.get("usage", {}).get("outputTokens", 0),
                )
                usage.total_tokens = usage.input_tokens + usage.output_tokens
                body = json.dumps(response).encode()
                return 200, body, _request_id_headers(response), usage

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning("bedrock_client_error", error_code=code, model_id=model_id)
            error_body = json.dumps(
                {"error": {"type": "provider_error", "message": str(e)}}
            ).encode()
            return status, error_body, {}, TokenUsage()
        except Exception:
            logger.exception("bedrock_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Bedrock call failed"}}',
                {},
                TokenUsage(),
            )

    async def count_tokens(self, request_body: bytes, model_id: str) -> tuple[int, int]:
        """Bedrock CountTokens API — returns (status, input_tokens). No cost, no inference."""
        try:
            client = await self._get_client()
            response = await self._call_with_connect_retry(
                client, None,
                lambda: client.count_tokens(
                    modelId=model_id,
                    input={"invokeModel": {"body": request_body}},
                ),
                model_id=model_id,
            )
            return 200, int(response.get("inputTokens", 0))
        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning("bedrock_count_tokens_error", error_code=code, model_id=model_id)
            return status, 0
        except Exception:
            logger.exception("bedrock_count_tokens_failed", model_id=model_id)
            return 502, 0

    async def invoke_stream(
        self,
        request_body: bytes,
        model_id: str,
        path_suffix: str = "invoke-with-response-stream",
        **kwargs,
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            client = await self._get_client()
            if path_suffix == "invoke-with-response-stream":
                # ⚠️ 연결 수준 오류(응답 0바이트) 만 1회 재시도한다. botocore 풀의 죽은 연결로
                #    "Connection was closed before we received a valid response" 가 2026-09-16
                #    하루 3번(첫 턴) 났고, bedrock_max_attempts=1 이라 그대로 502 로 나갔다.
                #    응답이 시작되기 전이라 중복 과금이 없다 — 스트림 도중 끊김은 재시도하지 않는다.
                response = await self._call_with_connect_retry(
                    client, _bedrock_executor,
                    lambda: client.invoke_model_with_response_stream(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                    model_id=model_id, event="bedrock_stream_connect_retry",
                )
                aws_request_id: str | None = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("body")
                return (
                    200,
                    self._bedrock_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

            elif path_suffix == "converse-stream":
                parsed_req = json.loads(request_body)
                response = await loop.run_in_executor(
                    _bedrock_executor,
                    lambda: client.converse_stream(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                )
                aws_request_id = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("stream")
                return (
                    200,
                    self._converse_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            error_msg = str(e)
            logger.warning(
                "bedrock_stream_client_error", error_code=code, model_id=model_id, error=error_msg
            )

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return status, error_gen(), {}, None
        except Exception as exc:
            error_msg = str(exc)
            logger.exception("bedrock_stream_failed", model_id=model_id)

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return 502, error_gen(), {}, None

    async def _bedrock_stream_gen(self, stream) -> AsyncIterator[bytes]:
        """Adapt botocore's blocking EventStream iterator to an async generator.

        Each `next()` call can block waiting for the next event to arrive over
        the wire, so we execute it in the default thread pool via
        `run_in_executor` to keep the event loop responsive.

        Errors propagate to the caller so the streaming helper can surface
        them as SSE `event: error` to the client rather than silently cutting
        the connection.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(_bedrock_executor, _next)
            if event is sentinel:
                return
            chunk = event.get("chunk", {})
            if "bytes" in chunk:
                yield chunk["bytes"]

    async def _converse_stream_gen(self, stream) -> AsyncIterator[bytes]:
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(_bedrock_executor, _next)
            if event is sentinel:
                return
            yield json.dumps(event).encode()
