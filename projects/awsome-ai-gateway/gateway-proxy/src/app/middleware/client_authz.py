# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json

import structlog

from app.schemas.errors import anthropic_error
from app.services.router_service import check_client_scope

logger = structlog.get_logger(__name__)


class ClientAuthorizationMiddleware:
    """Deny (400) when the identified client is not in the user's allowed_clients.

    Runs after Auth + ClientId so both state["auth_context"] and state["client"]
    are present. Soft entitlement gating: policy keyed on trusted user_id; client
    (UA) is untrusted — prevents escalation to a disallowed client, not spoofing
    within the allowed set.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        auth_context = state.get("auth_context")
        client = state.get("client")

        # auth_context 없는 경로(예: /health)는 통과 — Auth 가 이미 처리.
        if auth_context is not None:
            allowed = getattr(auth_context, "allowed_clients", None)
            try:
                check_client_scope(allowed, client)
            except PermissionError as e:
                logger.info("client_authz_denied", client=client, message=str(e))
                # ⚠️ 403 이 아니라 400 — Anthropic 클라이언트는 403 을 자격증명
                #    문제로 읽고 재인증 루프를 돌지만, 이 거부는 재시도로 풀리지
                #    않는다(fallback_loop.scope_denial_body 와 같은 근거).
                body = json.dumps(
                    anthropic_error("permission_error", str(e))
                ).encode()
                await send({
                    "type": "http.response.start", "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)
