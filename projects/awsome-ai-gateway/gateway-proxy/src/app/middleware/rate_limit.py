# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Rate Limit Middleware — Redis-down In-Memory Fallback 전담.

주 rate limit enforcement는 라우터의 enforce_rate_limits()가 담당 (모델 정보
해결 이후 full 3-scope RPM + TPM 체크). 이 미들웨어는 **Redis 장애 상황**에서만
USER/TEAM/GLOBAL 스코프 RPM을 in-memory로 근사 체크해 남용을 최소화.

Redis 장애 = 헬스체커의 REDIS_DEGRADED/BOTH_DEGRADED, 또는 (p1-hotpath-redis) 프로세스 공용
Redis 브레이커(hot_path.REDIS_BREAKER)가 열림 — 브레이커는 요청 경로 Redis 호출 하나가
타임아웃/연결 오류를 내면 바로 열리므로, 헬스체커가 강등하기(~수십 초) 전에도 fallback 한도가
걸린다. 한도는 스코프의 마지막으로 알던 값(rate_limit_config_loader), 없으면 설정 기본값.
"""

from __future__ import annotations

import json

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.schemas.errors import anthropic_error
from app.services.rate_limit_config_loader import last_known_limits
from app.services.rate_limit_scope import GLOBAL_WILDCARD, RateLimitScope, build_rl_key
from app.services.rate_limit_service import InMemoryRateLimiter

logger = structlog.get_logger(__name__)

# Redis-down fallback 의 in-memory USER RPM 카운터는 **프로세스마다 독립**이라
# 함대 전체엔 `replicas × uvicorn_workers` 개가 존재한다. divisor 를 그 곱으로
# 잡아야 각 카운터가 `limit // (replicas × workers)` 를 허용해 클러스터 총합이
# limit 에 맞는다(과거 하드코딩 4=uvicorn_workers 만, HPA replica 무시 → 부하테스트
# 429×6 배경, deepdive Q46/Q50). rl_fallback_replicas 기본 1 = 과거 동작 보존.
_settings = get_settings()
_fallback_worker_count = max(1, _settings.rl_fallback_replicas) * max(1, _settings.uvicorn_workers)
_in_memory_limiter = InMemoryRateLimiter(worker_count=_fallback_worker_count)

# Redis-down fallback 기본값 — 스코프의 마지막으로 알던 한도가 없거나 무제한일 때 쓴다(클러스터
# 총합, divisor 로 나뉜다). USER < TEAM < GLOBAL 로 계층 한도(deepdive Q50 Phase3 — 과거엔
# USER 만 검사해 한 팀이 fallback 용량을 독점할 수 있었다).
# 설정 rl_fallback_*_rpm(기본 60/600/6000).
_FALLBACK_USER_RPM = _settings.rl_fallback_user_rpm
_FALLBACK_TEAM_RPM = _settings.rl_fallback_team_rpm
_FALLBACK_GLOBAL_RPM = _settings.rl_fallback_global_rpm


def redis_fallback_active(state: dict) -> bool:
    """이 요청에 메모리 fallback 한도를 걸어야 하는가 — Redis 없음/열화/공용 브레이커 열림."""
    from app.schemas.domain import DegradationLevel

    if state.get("_redis") is None:
        return True
    dm = state.get("_degradation_manager")
    if dm and dm.level in (DegradationLevel.REDIS_DEGRADED, DegradationLevel.BOTH_DEGRADED):
        return True
    return redis_breaker_open()


def redis_breaker_open() -> bool:
    """공용 Redis 브레이커가 열려 있는가(rl_breaker_enabled=false 면 항상 False)."""
    if not get_settings().rl_breaker_enabled:
        return False
    from app.services.hot_path import REDIS_BREAKER

    return REDIS_BREAKER.is_open()


def _scope_limit(scope: RateLimitScope, scope_id, model_alias: str, default: int):
    """(한도, 카운터 키 alias). 마지막으로 알던 RPM 이 있으면 그 값(모델 무관 행이면 키는 `*`)."""
    known = last_known_limits(
        scope.value, None if scope == RateLimitScope.GLOBAL else scope_id, model_alias
    )
    if known is not None and known.rpm:
        return known.rpm, (GLOBAL_WILDCARD if known.model_agnostic else model_alias)
    return default, model_alias


def check_fallback_limits(state: dict, auth_context, model_alias: str, metrics=None):
    """메모리 fallback 한도 집행. 거절이면 (RateLimitResult, scope 이름), 통과면 None.

    미들웨어(모델 해석 전)와 라우터의 enforce_rate_limits(미들웨어 통과 뒤 브레이커가 열린 요청)가
    같이 쓴다 — ``_rl_fallback_checked`` 로 한 요청을 두 번 세지 않는다.
    """
    state["_rl_fallback_checked"] = True

    # Budget throttle 비율(있으면) 적용 — USER 한도에만(예산은 사용자 귀속).
    throttle_pct = None
    budget_status = state.get("budget_status")
    if budget_status and budget_status.throttle_active:
        throttle_pct = budget_status.throttle_rpm_pct or 50

    user_rpm, user_alias = _scope_limit(
        RateLimitScope.USER, auth_context.user_id, model_alias, _FALLBACK_USER_RPM
    )
    if throttle_pct is not None:
        user_rpm = max(1, int(user_rpm * throttle_pct / 100))
    team_rpm, team_alias = _scope_limit(
        RateLimitScope.TEAM, auth_context.team_id, model_alias, _FALLBACK_TEAM_RPM
    )
    global_rpm, global_alias = _scope_limit(
        RateLimitScope.GLOBAL, None, model_alias, _FALLBACK_GLOBAL_RPM
    )

    # (scope, scope_id, key alias, limit) — USER→TEAM→GLOBAL fast-fail 순서.
    checks = [
        (RateLimitScope.USER, auth_context.user_id, user_alias, user_rpm),
        (RateLimitScope.TEAM, auth_context.team_id, team_alias, team_rpm),
        (RateLimitScope.GLOBAL, GLOBAL_WILDCARD, global_alias, global_rpm),
    ]

    # 라우터가 넘기는 metrics 도 같은 GatewayMetrics 지만, 지표가 없는 스텁도 받아들인다.
    has_metrics = metrics is not None and hasattr(metrics, "rl_fallback_entered_total")
    if has_metrics:
        metrics.rl_fallback_entered_total.add(1)

    for sc, sid, alias, limit in checks:
        if not sid:  # team_id 누락 등 → 해당 scope 스킵
            continue
        key = build_rl_key(sc, sid, alias, "rpm")
        result = _in_memory_limiter.check(key, limit)
        state["rate_limit_result"] = result  # 마지막 결과 노출(거절이면 거절 결과)
        if not result.allowed:
            if has_metrics:
                metrics.rl_fallback_429_total.add(1, {"scope": sc.value})
            return result, sc.value
    return None


def fallback_429_parts(result, scope_name: str = "USER") -> tuple[bytes, list[tuple[bytes, bytes]]]:
    body = json.dumps(
        anthropic_error(
            "rate_limit_error",
            f"Rate limit exceeded (degraded mode). "
            f"Please retry after {result.retry_after} seconds.",
            code=f"{scope_name.lower()}_rpm_exceeded",
            scope=scope_name,
            limit_type="rpm",
            retry_after=result.retry_after,
            degraded=True,
        )
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"retry-after", str(result.retry_after or 60).encode()),
        (b"x-ratelimit-scope", scope_name.encode()),
        (b"x-ratelimit-type", b"rpm"),
        (b"x-ratelimit-remaining", b"0"),
        (b"x-ratelimit-limit", str(result.limit).encode()),
        (b"x-ratelimit-reset", str(result.window_reset).encode()),
        (b"x-ratelimit-degraded", b"true"),
    ]
    return body, headers


class RateLimitMiddleware:
    """Redis-down 시 USER 스코프 RPM In-Memory Fallback (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})

        is_rate_limited_path = path.startswith("/model/") or path in (
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/messages",
            "/v1/responses",  # Codex (Responses API) must be rate-limited (incl. degraded mode)
        )
        if not is_rate_limited_path:
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        if auth_context is None:
            await self.app(scope, receive, send)
            return

        # Redis 정상 → 라우터의 enforce_rate_limits 가 처리
        if not redis_fallback_active(state):
            await self.app(scope, receive, send)
            return

        # Redis 다운 → In-Memory Fallback. USER→TEAM→GLOBAL 계층 모두 근사 집행
        # (deepdive Q50 Phase3 — 과거 USER 만 검사해 한 팀이 fallback 용량 독점 가능).
        model_config = state.get("model_config")
        model_alias = (
            (model_config.alias or model_config.provider_model_id)
            if model_config
            else None
        )
        if model_alias is None:
            # 이 미들웨어는 라우터보다 먼저라 model_config 해석 전이다 — body 의
            # `model` 필드(/model/* 는 path segment)로라도 잡아야 "unknown" 한
            # 버킷에 모든 모델이 몰려 서로의 한도를 잡아먹지 않는다.
            from app.middleware.downgrade import (
                _drain_body,
                _model_from_bedrock_path,
                _replay,
            )

            if path.startswith("/model/"):
                from_path = _model_from_bedrock_path(path)
                model_alias = from_path[0] if from_path else "unknown"
            else:
                body_bytes = await _drain_body(receive)
                try:
                    parsed = json.loads(body_bytes) if body_bytes else None
                except json.JSONDecodeError:
                    parsed = None
                model_alias = (
                    parsed.get("model")
                    if isinstance(parsed, dict) and isinstance(parsed.get("model"), str)
                    else "unknown"
                )
                receive = _replay(body_bytes, receive)

        metrics = getattr(getattr(scope.get("app", None), "state", None), "metrics", None)
        rejected = check_fallback_limits(state, auth_context, model_alias, metrics)
        if rejected is not None:
            result, scope_name = rejected
            await self._send_429_fallback(scope, send, result, scope_name)
            return

        await self.app(scope, receive, send)

    async def _send_429_fallback(
        self, scope: Scope, send: Send, result, scope_name: str = "USER"
    ) -> None:
        body, headers = fallback_429_parts(result, scope_name)
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})
