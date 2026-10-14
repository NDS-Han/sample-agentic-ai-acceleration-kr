# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import json

import structlog
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)


class MaxBodySizeMiddleware:
    """요청 본문 상한 (R16 — dead config 배선).

    ``max_body_size`` 설정값이 선언만 되고 어디에도 적용되지 않았다.
    두 경로로 차단한다:
      - Content-Length 헤더가 상한 초과면 읽기 전 413(초과 본문이 메모리에
        올라가기 전에 차단되는 게 핵심).
      - 헤더가 없거나 거짓이면(chunked/무길이) ``receive`` 를 감싸 누적 바이트가
        상한을 넘는 순간 disconnect 로 앱을 끊고 413 을 대신 보낸다.

    인증보다 앞에 두어 비인증 대형 요청도 일찍 끊는다.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def _send_413(self, scope: Scope, send: Send, *, received: int) -> None:
        logger.warning(
            "request_body_too_large",
            path=scope.get("path", ""),
            received=received,
            max=self._max,
        )
        body = json.dumps(
            {
                "error": {
                    "type": "request_too_large",
                    "message": f"Request body exceeds {self._max} bytes",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._max <= 0:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"content-length")
        if raw is not None:
            try:
                declared = int(raw)
            except ValueError:
                declared = 0
            if declared > self._max:
                await self._send_413(scope, send, received=declared)
                return

        state = {
            "received": 0,
            "rejected": False,
            "responded": False,
            "response_started": False,
        }

        async def limited_receive() -> Message:
            if state["rejected"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body") or b"")
                if state["received"] > self._max:
                    state["rejected"] = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not state["rejected"]:
                # response.start 가 실제로 클라이언트에 나간 경우만 추적한다 —
                # 억제된 start 를 '시작됨'으로 세면 이후 빈 body 만 나가
                # 프로토콜 위반이 된다.
                if message["type"] == "http.response.start":
                    state["response_started"] = True
                await send(message)
                return
            # 거부 이후의 앱 출력.
            if state["response_started"]:
                # 이미 response.start 가 클라이언트로 나갔다 — 상태 코드 교체가
                # 불가하므로 두 번째 start(프로토콜 위반)를 보내는 대신 남은 앱
                # 출력을 끊고 빈 body 로 응답을 종결해 hang 을 막는다.
                if not state["responded"]:
                    state["responded"] = True
                    logger.warning(
                        "request_body_too_large_after_response_start",
                        path=scope.get("path", ""),
                        received=state["received"],
                        max=self._max,
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                return
            if state["responded"]:
                return
            state["responded"] = True
            # 앱이 거부 후에도 응답을 시도하면 413 으로 대체한다.
            await self._send_413(scope, send, received=state["received"])

        try:
            await self.app(scope, limited_receive, guarded_send)
        except ClientDisconnect:
            # Request.body() 는 disconnect 를 예외로 올린다 — 상한 초과 disconnect
            # 면 413 으로 마무리하고, 그 외(실제 클라이언트 단절)는 그대로 전파.
            if state["rejected"] and not state["responded"]:
                if state["response_started"]:
                    # start 는 이미 나갔다 — 두 번째 start 대신 종결 body 만.
                    state["responded"] = True
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                else:
                    await self._send_413(
                        scope, send, received=state["received"]
                    )
            else:
                raise
        except Exception:
            # 상한 초과 disconnect 를 읽은 앱이 ClientDisconnect 외의 예외로
            # 나와도(자체 스트림 처리 등) 응답이 안 나갔으면 413 으로 마무리 —
            # ExceptionMiddleware 에서 500 으로 오인되는 것을 막는다.
            if state["rejected"] and not state["responded"]:
                logger.warning(
                    "request_body_too_large_app_error", exc_info=True
                )
                if state["response_started"]:
                    # start 는 이미 나갔다 — 남길 수 있는 건 종결 body 뿐.
                    state["responded"] = True
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"",
                            "more_body": False,
                        }
                    )
                else:
                    await self._send_413(
                        scope, send, received=state["received"]
                    )
            else:
                raise
