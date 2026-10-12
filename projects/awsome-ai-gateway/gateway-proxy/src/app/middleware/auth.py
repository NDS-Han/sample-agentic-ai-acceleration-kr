# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.schemas.domain import AuthType
from app.schemas.errors import anthropic_error
from app.services.auth_service import AuthBackendUnavailableError, resolve_auth_strategy

logger = structlog.get_logger(__name__)

EXEMPT_PATHS = {"/health", "/health/ready"}


class AuthMiddleware:
    """경로 기반 인증 전략 선택 + 실행 Middleware (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})
        degradation_manager = state.get("degradation_manager") or scope.get("app", {})

        # 인증 면제 경로
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        strategy = resolve_auth_strategy(path)
        if strategy is None:
            await self._send_401(scope, send, "Unknown route")
            return

        # request 객체에서 Authorization 헤더 추출
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode()
        if not authorization:
            # Claude Code 는 ANTHROPIC_API_KEY 경로로 x-api-key 헤더를 보낸다 —
            # VK 를 그 헤더로 제시하는 클라이언트도 Bearer 와 동등하게 취급한다.
            api_key = headers.get(b"x-api-key", b"").decode()
            if api_key:
                authorization = f"Bearer {api_key}"

        dm = state.get("_degradation_manager")
        redis = state.get("_redis")
        session_factory = state.get("_session_factory")

        # Degradation 상태에 따라 DB/Redis 가용 여부 결정
        degradation_level = None
        if dm is not None:
            degradation_level = dm.level

        from app.schemas.domain import DegradationLevel

        if degradation_level == DegradationLevel.BOTH_DEGRADED:
            await self._send_503(scope, send)
            return

        effective_redis = (
            redis if (degradation_level != DegradationLevel.REDIS_DEGRADED) else None
        )
        use_db = (
            degradation_level != DegradationLevel.DB_DEGRADED and session_factory is not None
        )

        # inner app 의 send 를 감싸 응답 시작 여부를 추적한다 — 응답이 이미 시작된
        # 뒤(SSE 스트림 중)에는 어떤 에러도 두 번째 http.response.start 를 쓸 수
        # 없으므로, 아래 모든 except 절은 이 플래그로 "응답 작성"과 "재전파"를
        # 결정한다. 라우터의 의도된 PermissionError 거부(모델/클라이언트 ACL)는
        # 응답 전에만 발생하므로 401 계약은 그대로 보존된다.
        response_started = False

        async def send_tracking(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            if use_db:
                # short-lived session: VK 인증 쿼리만 수행 후 즉시 반환.
                # SSE 응답 동안 pool 을 점유하지 않아 pool 회전율 크게 개선.
                async with session_factory() as db:
                    auth_context = await strategy.authenticate(
                        authorization, effective_redis, db
                    )
            else:
                auth_context = await strategy.authenticate(
                    authorization, effective_redis, None
                )
            state["auth_context"] = auth_context
            structlog.contextvars.bind_contextvars(
                user_id=auth_context.user_id,
                sso_subject=auth_context.sso_subject,
            )
            await self.app(scope, receive, send_tracking)

        except AuthBackendUnavailableError as e:
            # 인프라 장애(DB/Redis 열화)로 인증 자체가 불가 — 자격증명 거부가 아니므로
            # 401 이 아니라 503 을 내고 security 이벤트에도 기록하지 않는다
            # (PermissionError 서브클래스라 순서상 여기가 먼저 와야 한다).
            # inner app 구간에서 올라와도 인프라 장애인 사실은 변하지 않는다 —
            # 응답 미시작이면 503, 시작 후면 재전파.
            if response_started:
                raise
            logger.warning("auth_backend_unavailable", path=path, reason=str(e))
            await self._send_503(scope, send)
            return

        except (PermissionError, ValueError) as e:
            if response_started:
                raise
            await self._handle_auth_failure(scope, send, state, path, e)

        except (
            RedisError,
            SQLAlchemyError,
            asyncio.TimeoutError,
            ConnectionError,
        ) as e:
            # 헬스체커가 열화를 마킹하기 전의 미드요청 백엔드 단절 — 자격증명
            # 문제가 아니라 인프라 장애라 503.
            # builtin ConnectionError 포함 이유: SQLAlchemy 는 dbapi.Error 만 래핑
            # 하므로 asyncpg 커넥트 단계의 소켓 오류(ConnectionRefusedError 등)는
            # 미래핑으로 새나올 수 있다. PermissionError ⊄ ConnectionError 라
            # 절 순서 충돌은 없고, inner app 응답 시작 후엔 어차피 재전파된다.
            if response_started:
                raise
            logger.warning("auth_backend_error", path=path, error=str(e))
            await self._send_503(scope, send)
            return

    async def _handle_auth_failure(
        self, scope: Scope, send: Send, state: dict, path: str, e: Exception
    ) -> None:
        """자격증명 거부 공통 경로 — security 이벤트 기록 + 401."""
        error_msg = str(e)
        logger.info("auth_failed", path=path, reason=error_msg)

        # 보안 이벤트 감지
        sec_detector = state.get("_security_detector")
        if sec_detector is not None:
            # XFF 의 가장 오른쪽 홉 = 직접 프록시(ALB)가 기록한 진짜 클라이언트 IP.
            # 왼쪽 홉은 클라이언트가 임의로 위조할 수 있어 브루트포스 감지에 쓰면
            # 위조 IP 로 회피/오남용이 가능하다. XFF 없으면 직접 피어를 쓴다.
            xff = dict(scope.get("headers", [])).get(b"x-forwarded-for", b"").decode()
            if xff:
                source_ip = xff.split(",")[-1].strip() or "unknown"
            else:
                peer = scope.get("client")
                source_ip = peer[0] if peer else "unknown"
            auth_type = AuthType.JWT if path.startswith("/v1/") else AuthType.VIRTUAL_KEY
            from app.services.streaming import _spawn

            # bare create_task 는 약한 참조라 태스크가 GC 되어 실패 기록이 유실될 수 있다.
            _spawn(sec_detector.record_auth_failure(source_ip, auth_type))

        await self._send_401(scope, send, error_msg)

    async def _send_401(self, scope: Scope, send: Send, detail: str) -> None:
        body = json.dumps(
            anthropic_error("authentication_error", "Unauthorized", code="auth_failed")
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_503(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            anthropic_error("service_unavailable", "Service temporarily unavailable")
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
