# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.degradation.manager import DegradationManager
from app.schemas.domain import DegradationLevel
from app.schemas.responses import HealthComponent, HealthResponse

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/health")
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe — **항상 200** (프로세스가 응답 가능한 한).

    BOTH_DEGRADED 에 503 을 내던 이전 계약은 restart cascade 를 유발했다:
    양중 장애는 플릿 전체가 동시에 들어가는 상태라, kubelet 이 모든 파드를
    재시작하면서 ① 인메모리 cost 스풀/버퍼가 유실되고 ② 새 파드는 HEALTHY 로
    시작해 failure 임계(~75s)까지 재탐지를 기다린 뒤 다시 재시작되는 루프가
    된다. 의존성 장애는 재시작으로 고칠 수 없다 — 살아있는 프로세스를 죽이지
    말고, 트래픽 배제는 아래 /health/ready 가 담당한다.
    열화 상태는 본문의 status/components/degradation_level 에 그대로 보고한다.
    """
    dm: DegradationManager = request.app.state.degradation_manager
    level = dm.level

    components = {
        "postgresql": HealthComponent(
            status="down"
            if level in (DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED)
            else "up"
        ),
        "redis": HealthComponent(
            status="down"
            if level in (DegradationLevel.REDIS_DEGRADED, DegradationLevel.BOTH_DEGRADED)
            else "up"
        ),
    }

    if level == DegradationLevel.BOTH_DEGRADED:
        status = "unhealthy"
    elif level != DegradationLevel.HEALTHY:
        status = "degraded"
    else:
        status = "healthy"

    return JSONResponse(
        status_code=200,
        content={
            "status": status,
            "components": {k: v.model_dump() for k, v in components.items()},
            "degradation_level": level.value,
        },
    )


@router.get("/health/ready")
async def readiness_check(request: Request) -> JSONResponse:
    """Readiness probe — **process-local only** (wave 1 hot-path isolation).

    Ready = this process finished startup and is not shutting down. If this handler
    runs at all, the event loop is alive.

    DB / Redis / pool state is **reported, never routed on**: those dependencies are
    shared by every pod, so gating readiness on them took the whole fleet out of the ALB
    at once when the shared database got slow (e.g. admin analytics load) — a dependency
    slowdown became a full outage. The proxy now serves through such slowdowns from its
    local caches (auth, downgrade, web-search policy) and fails individual requests with a
    clear 503 only when it truly cannot answer. Dependency health is exported through
    the degradation metrics/logs and the fields below.
    """
    state = request.app.state
    dm: DegradationManager | None = getattr(state, "degradation_manager", None)
    level = dm.level if dm is not None else DegradationLevel.HEALTHY

    pool_info: dict = {}
    pool_saturated = False
    engine = getattr(state, "db_engine", None)
    if engine is not None:
        try:
            pool = engine.pool
            checked_out = pool.checkedout()
            hard_cap = pool.size() + getattr(get_settings(), "db_max_overflow", 0)
            pool_saturated = hard_cap > 0 and checked_out >= hard_cap
            pool_info = {
                "checked_out": checked_out,
                "regular_size": pool.size(),
                "hard_cap": hard_cap,
                "overflow_open": max(0, pool.overflow()),
            }
        except Exception:
            pool_info = {"error": "pool_introspection_failed"}
    if pool_saturated:
        logger.warning("readiness_report_pool_saturated", **pool_info)

    started = bool(getattr(state, "started", False))
    shutting_down = bool(getattr(state, "shutting_down", False))
    ready = started and not shutting_down
    listener = getattr(state, "auth_invalidation", None)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else ("shutting_down" if shutting_down else "starting"),
            # informational only — not part of the decision
            "degradation_level": level.value,
            "db_degraded": level
            in (DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED),
            "redis_degraded": level
            in (DegradationLevel.REDIS_DEGRADED, DegradationLevel.BOTH_DEGRADED),
            "pool_saturated": pool_saturated,
            "pool": pool_info,
            "auth_invalidation_connected": bool(getattr(listener, "connected", False)),
        },
    )
