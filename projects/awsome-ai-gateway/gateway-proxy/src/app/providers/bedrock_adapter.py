# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from urllib3.exceptions import ProtocolError as _U3ProtocolError
from urllib3.exceptions import ReadTimeoutError as _U3ReadTimeoutError

from app.providers.base import ATTEMPT_UNSETTLED_HEADER, ProviderAdapter
from app.schemas.domain import TokenUsage

_BEDROCK_POOL_SIZE = int(os.environ.get("BEDROCK_THREAD_POOL_SIZE", "128"))
_bedrock_executor = ThreadPoolExecutor(max_workers=_BEDROCK_POOL_SIZE, thread_name_prefix="bedrock")

logger = structlog.get_logger(__name__)

# 상류 오류 본문에서 인프라 식별자를 지우는 패턴 — cross-account assume role ARN,
# 추론프로파일 ARN, 12자리 계정 ID 가 클라이언트로 새어 나가지 않게 한다(E2E S3-M4).
_ARN_RE = re.compile(
    r"arn:aws[a-zA-Z-]*:[^\s'\"]+|assumed-role/[^\s'\"]+|(?<!\d)\d{12}(?!\d)"
)


def _redact_arns(text: str) -> str:
    # 전송 예외 문자열엔 ARN 이 URL 인코딩(arn%3Aaws%3A…)된 채 섞일 수 있다 —
    # 디코드 후 치환해 추론프로파일명까지 가린다(R16-V6-N1).
    from urllib.parse import unquote

    return _ARN_RE.sub("[redacted]", unquote(text))


# boto3 예외 → HTTP 상태 코드 매핑
_BOTO_ERROR_MAP: dict[str, int] = {
    "ValidationException": 400,
    # ⚠️ AccessDenied 는 403 으로 내보내지 않는다. (1) Anthropic 클라이언트는 403 을
    #    자격증명 문제로 읽어 재인증 루프를 돈다 — 실제로는 우리 IAM 의 미설정이라
    #    재인증으로 풀리지 않는다. (2) 본문에 assumed-role ARN/계정 ID 가 새어 나간다.
    #    상류 권한 문제는 우리 쪽 결함이므로 502 provider_error + 일반 문구로 환원한다
    #    (원문은 로그에만). 502 라 폴백 체인이 허용되는 다른 모델도 시도한다.
    "AccessDeniedException": 502,
    "ResourceNotFoundException": 404,
    "ThrottlingException": 429,
    "ModelTimeoutException": 504,
    "ServiceUnavailableException": 503,
    "ServiceQuotaExceededException": 429,
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
    # Anthropic 응답의 usage.cache_creation.ephemeral_1h_input_tokens —
    # 혼합 TTL 요청의 1h 캐시 쓰기만 분리해 정확 과금(R2-12).
    # 미보고 시 None 유지(삼값, A2-4) — 0 으로 채우면 "분해가 보고됐는데 0"과
    # 구분이 안 돼 요청 측 1h 플래그가 잘못 발동한다.
    cache_creation_1h = (usage.get("cache_creation") or {}).get(
        "ephemeral_1h_input_tokens"
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        cache_creation_1h_input_tokens=cache_creation_1h,
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
                    # ⚠️ Converse 의 usage 에는 캐시 카운터도 camelCase 로 온다
                    #    (cacheReadInputTokens/cacheWriteInputTokens). 읽지 않으면
                    #    캐시된 토큰이 usage_logs 와 과금 양쪽에서 0 이 된다.
                    cache_read_input_tokens=response.get("usage", {}).get(
                        "cacheReadInputTokens", 0
                    ),
                    cache_creation_input_tokens=response.get("usage", {}).get(
                        "cacheWriteInputTokens", 0
                    ),
                )
                usage.total_tokens = usage.input_tokens + usage.output_tokens
                body = json.dumps(response).encode()
                return 200, body, _request_id_headers(response), usage

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning("bedrock_client_error", error_code=code, model_id=model_id)
            if code == "AccessDeniedException":
                # str(e) 에 assumed-role ARN/계정이 들어 있다 — 일반 문구로 환원.
                error_body = json.dumps(
                    {"error": {"type": "provider_error",
                               "message": "Bedrock denied access to this model (upstream IAM)"}}
                ).encode()
            else:
                # 원문을 내보내되 ARN/계정 식별자는 삭제한다 — ValidationException 메시지는
                # 클라이언트가 요청을 고치는 데 필요하고, 대신 ResourceNotFound 계열에는
                # 우리가 쓴 추론프로파일 ARN 이 들어갈 수 있다(E2E S3-M4). 원문은 로그에.
                error_body = json.dumps(
                    {"error": {"type": "provider_error", "message": _redact_arns(str(e))}}
                ).encode()
            return status, error_body, {}, TokenUsage()
        except ReadTimeoutError:
            # 읽기 타임아웃 — 상류 추론이 끝났을 수도 있다(과금됨). 504 = 폴백 대상이지만
            # CB 실패는 아니라는 기존 ModelTimeoutException 의미론과 같게 둔다(E2E S3-M3).
            # 예전에는 catch-all 이 502 로 바꿔 CB 를 오염시키고, 실제 성공했을 수 있는
            # 추론을 다음 후보에 중복 청구했다.
            # ATTEMPT_UNSETTLED — 클라이언트측 소켓 타임아웃이라 상류가 이미 처리·과금
            # 했을 수 있다. 폴백 루프는 이 표시를 보고 다음 후보 호출을 중단한다(서버가
            # 명시 거부한 ModelTimeoutException 과는 다른 경우다 — 그것은 폴백 허용).
            logger.warning("bedrock_invoke_read_timeout", model_id=model_id)
            return (
                504,
                b'{"error":{"type":"provider_error","message":"Bedrock read timeout"}}',
                {ATTEMPT_UNSETTLED_HEADER: "1"},
                TokenUsage(),
            )
        except (ConnectTimeoutError, EndpointConnectionError, ConnectionClosedError):
            # 연결 수준 실패 — 상류에 도달하지 않아 과금이 없다. 503(가용성 신호)로
            # 명시한다 — catch-all 502 와 같은 폴백 대상이지만 의미가 정확하다.
            logger.warning("bedrock_invoke_connect_failed", model_id=model_id)
            return (
                503,
                b'{"error":{"type":"provider_error","message":"Bedrock connection failed"}}',
                {},
                TokenUsage(),
            )
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
                # invoke-with-response-stream 과 같은 이유 — 죽은 풀 연결의
                # 연결 수준 오류만 1회 재시도(응답 시작 전이라 중복 과금 없음).
                response = await self._call_with_connect_retry(
                    client, _bedrock_executor,
                    lambda: client.converse_stream(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                    model_id=model_id, event="converse_stream_connect_retry",
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
            if code == "AccessDeniedException":
                # ARN/계정 누출 방지 — invoke 경로와 같은 환원.
                error_msg = "Bedrock denied access to this model (upstream IAM)"
            else:
                # invoke 경로와 같은 이유 — 인프라 식별자만 지우고 메시지는 보존.
                error_msg = _redact_arns(error_msg)

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return status, error_gen(), {}, None
        except (
            ReadTimeoutError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ConnectionClosedError,
        ) as exc:
            # invoke() 와 같은 이유 — 타임아웃/연결 오류를 502 로 뭉개지 않는다.
            is_timeout = isinstance(exc, ReadTimeoutError)
            status = 504 if is_timeout else 503
            error_msg = "Bedrock read timeout" if is_timeout else "Bedrock connection failed"
            logger.warning(
                "bedrock_stream_connect_failed", model_id=model_id, status=status, error=str(exc)
            )

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            # 읽기 타임아웃은 상류 처리·과금 가능성이 있어 폴백을 멈추게 표시한다
            # (invoke() 의 같은 블록 주석 참조).
            return status, error_gen(), (
                {ATTEMPT_UNSETTLED_HEADER: "1"} if is_timeout else {}
            ), None
        except Exception as exc:
            error_msg = _redact_arns(str(exc))
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

        try:
            while True:
                # ⚠️ botocore 는 미드스트림 **예외 프레임**(internalServerException /
                #    modelStreamErrorException / throttlingException / modelTimeoutException)
                #    을 EventStreamError(=ClientError) 로 *raise* 한다 — dict 멤버로
                #    넘겨주지 않는다. catch 없이 두면 스트림이 에러 신호 없이 잘긴다.
                try:
                    event = await loop.run_in_executor(_bedrock_executor, _next)
                except (
                    ClientError,
                    ConnectionClosedError,
                    EndpointConnectionError,
                    ReadTimeoutError,
                    # 미드스트림 전송 실패는 botocore 예외로 감싸지지 않고 urllib3
                    # 원형 그대로 올라온다(_raw_stream.stream() 경유) — 같이 잡지
                    # 않으면 에러 프레임 없이 연결만 잘린다(R16-V6-SF1).
                    _U3ProtocolError,
                    _U3ReadTimeoutError,
                ) as exc:
                    err = getattr(exc, "response", {}).get("Error", {})
                    code = err.get("Code") or "stream_error"
                    msg = err.get("Message") or str(exc) or code
                    yield json.dumps(
                        {
                            "type": "error",
                            "error": {"type": code, "message": _redact_arns(msg)},
                        }
                    ).encode()
                    return
                if event is sentinel:
                    return
                chunk = event.get("chunk", {})
                if "bytes" in chunk:
                    yield chunk["bytes"]
                    continue
                # ⚠️ `chunk` 가 아닌 멤버 중 *Exception/*Error 는 미드스트림
                #    **예외 이벤트**다(internalServerException / modelStreamErrorException /
                #    throttlingException / modelTimeoutException 등). 예전엔 조용히
                #    드롭돼 클라이언트가 오류 신호 없이 잘린 스트림을 받았다(E2E S4).
                #    Anthropic-wire error 이벤트로 합성해 흘려보낸다 — 상류 멤버
                #    이름/메시지는 인프라 식별자를 담을 수 있어 살균한다.
                #    그 외의 비-chunk 멤버(예: metadata)는 기존처럼 건너뛴다.
                for key, val in event.items():
                    if key == "chunk" or not key.endswith(("Exception", "Error")):
                        continue
                    msg = val.get("message") if isinstance(val, dict) else None
                    payload = {
                        "type": "error",
                        "error": {
                            "type": key,
                            "message": _redact_arns(msg) if msg else key,
                        },
                    }
                    yield json.dumps(payload).encode()
        finally:
            # ⚠️ 제너레이터가 버려지거나(aclose/취소/idle-timeout/GC) 닫힐 때 botocore
            #    스트림도 반드시 닫는다. 닫지 않으면 executor 스레드가 죽은 상류
            #    커넥션의 blocking next() 에 영구히 묶여, 반복되면 128개 풀이 고갈돼
            #    모든 Bedrock 호출이 멈춘다(E2E S4). close() 하면 next() 가 즉시
            #    예외/반환으로 풀려 스레드가 회수된다.
            try:
                stream.close()
            except Exception:
                pass

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

        try:
            while True:
                # converse-stream 유니온의 예외 멤버는 {"internalServerException": …}
                # dict 로 오지만, 모델링되지 않은 예외 프레임은 EventStreamError 로
                # raise 된다 — 유니온 모양 그대로 합성해 클라이언트에 흘려보낸다.
                try:
                    event = await loop.run_in_executor(_bedrock_executor, _next)
                except (
                    ClientError,
                    ConnectionClosedError,
                    EndpointConnectionError,
                    ReadTimeoutError,
                    # _bedrock_stream_gen 과 동일 — 래핑되지 않는 urllib3 전송
                    # 예외도 유니온 모양 에러로 합성한다(R16-V6-SF1).
                    _U3ProtocolError,
                    _U3ReadTimeoutError,
                ) as exc:
                    err = getattr(exc, "response", {}).get("Error", {})
                    code = err.get("Code") or "internalServerException"
                    msg = err.get("Message") or str(exc) or code
                    yield json.dumps(
                        {code: {"message": _redact_arns(msg)}}
                    ).encode()
                    return
                if event is sentinel:
                    return
                yield json.dumps(event).encode()
        finally:
            # _bedrock_stream_gen 의 finally 와 동일 — 버려진 제너레이터가 executor
            # 스레드를 붙잡지 않도록 상류 스트림을 닫는다(E2E S4).
            try:
                stream.close()
            except Exception:
                pass
