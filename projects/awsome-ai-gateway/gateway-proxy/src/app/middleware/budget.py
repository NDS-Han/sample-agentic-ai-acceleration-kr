# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.schemas.errors import anthropic_error
from app.periods import (
    period_at,
    reset_request_period,
    reset_request_started_at,
    set_request_period,
    set_request_started_at,
)
from app.services.budget_service import BudgetService

logger = structlog.get_logger(__name__)

_budget_service = BudgetService()

BUDGET_EXEMPT_PATHS = {"/health", "/health/ready", "/v1/models"}


class BudgetMiddleware:
    """예산 정책 확인 Middleware (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # D-20/§6-4: 비용·사용량 카운터는 **요청 시작 시각**이 속한 월에 귀속된다.
        # 월 경계를 넘겨 끝나는 스트리밍 요청이 다음 달 카운터에 새지 않도록
        # 시작 시점의 period 를 ContextVar 에 심어 cost_recorder 가 읽는다.
        # request_started_at 도 함께 심는다 — cost:stream 엔트리의
        # requested_at/period/date 가 이 시작 시각에서 파생된다.
        request_start = datetime.now(UTC)
        tok_started = set_request_started_at(request_start)
        tok_period = set_request_period(period_at(request_start))
        try:
            await self._dispatch(scope, receive, send, request_start)
        finally:
            # 요청 처리 후 이전 값으로 복원 — uvicorn 은 요청마다 새 태스크를
            # 만들어 전제상 오염이 없지만, 태스크를 재사용하는 커스텀 ASGI
            # 하네스/테스트에서도 이전 요청 시각이 새지 않게 방어한다.
            # (백그라운드 drain 태스크는 생성 시점에 context 를 복사하므로
            # 여기서 리셋해도 그쪽에는 영향이 없다.)
            reset_request_started_at(tok_started)
            reset_request_period(tok_period)

    async def _dispatch(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_start: datetime,
    ) -> None:

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})

        if path in BUDGET_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        if auth_context is None:
            await self.app(scope, receive, send)
            return

        is_budget_path = (
            path.startswith("/model/")
            or path.startswith("/v1/messages")
            or path.startswith("/v1/chat")
            or path.startswith("/v1/completions")
            or path.startswith("/v1/responses")  # Codex (Responses API) must be budget-gated
        )
        if not is_budget_path:
            await self.app(scope, receive, send)
            return

        redis = state.get("_redis")
        session_factory = state.get("_session_factory")
        # KST 월 — 예산 카운터를 쓰는 쪽(cost_recorder.py)과 같은 경계여야 한다.
        # UTC 였을 때는 매월 1일 KST 00:00~09:00 동안 지난달 카운터를 계속 조회해,
        # 지난달 예산을 소진한 사용자/팀이 새 달 첫 9시간 동안 차단된 채로 남았다.
        # 위에서 심은 request_start 와 같은 시각을 써야 체크와 차감이 같은 버킷을 본다.
        period = period_at(request_start)

        from app.schemas.domain import DegradationLevel

        dm = state.get("_degradation_manager")
        is_redis_degraded = dm and dm.level in (
            DegradationLevel.REDIS_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )
        is_db_degraded = dm and dm.level in (
            DegradationLevel.DB_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )

        effective_redis = None if is_redis_degraded else redis
        use_db = not is_db_degraded and session_factory is not None

        client = state.get("client")

        try:
            # short-lived session: budget 조회 후 즉시 반환.
            if use_db:
                async with session_factory() as db:
                    budget_status = await _budget_service.check_budget(
                        effective_redis,
                        db,
                        auth_context.user_id,
                        auth_context.team_id,
                        period,
                        client=client,
                    )
            else:
                budget_status = await _budget_service.check_budget(
                    effective_redis,
                    None,
                    auth_context.user_id,
                    auth_context.team_id,
                    period,
                    client=client,
                )
            state["budget_status"] = budget_status
            if budget_status.warning_tiers:
                state["budget_soft_warning"] = True

            await self.app(scope, receive, send)

        except PermissionError as e:
            reason = str(e)
            await self._send_429_budget(scope, send, reason)

    async def _send_429_budget(self, scope: Scope, send: Send, reason: str) -> None:
        if reason == "no_team_assigned":
            # §6-5 단계 0 — 팀 미배정 유저. cognito sync 가 전원 팀을 배정하므로
            # 실질적으로 발생하지 않지만, 발생 시 fail-closed 로 둔다.
            message = "No team assigned. Contact your admin."
            code = "no_team_assigned"
        elif reason == "no_budget_assigned":
            # Q 정책 적용 후 거의 발생하지 않지만 호환 위해 유지
            message = "No budget assigned. Contact your admin."
            code = "no_budget_assigned"
        elif reason == "team_budget_unset":
            message = "팀 예산이 설정되지 않았습니다. 관리자에게 문의하세요."
            code = "team_budget_unset"
        elif reason in ("team_budget_exceeded", "user_budget_exceeded"):
            message = "Budget limit exceeded."
            code = reason
        elif reason == "hard_block":
            message = "Monthly budget exhausted. Contact your team leader or admin."
            code = "hard_block"
        elif reason == "client_budget_exceeded":
            message = "App (client) budget limit exceeded. 앱별 예산을 초과했습니다."
            code = "client_budget_exceeded"
        else:
            message = "Budget limit exceeded."
            code = reason

        body = json.dumps(
            anthropic_error("budget_exceeded", message, code=code)
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
