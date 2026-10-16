# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import hmac

from fastapi import APIRouter, Depends, Request
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.auth import UserRole

logger = structlog.get_logger()

router = APIRouter(tags=["Internal"])


async def _require_internal_token(request: Request) -> None:
    """`/internal/*` 비-health 엔드포인트의 공유 시크릿 게이트.

    admin-api 는 ALB 로 전 경로가 공개되므로, ops/test 용도의 internal 경로는
    `is_production` 게이트만으로는 부족하다 — dev 도 공개 도메인이라 무인증
    VK 발급(`/internal/test/issue-key`)이 라이브로 확인됐다(R3-1).

    토큰 미설정이면 항상 403(fail-closed). 호출자는 `X-Internal-Token` 헤더로
    `INTERNAL_API_TOKEN` 과 일치하는 값을 보낸다(통합 테스트·smoke-test 용).
    비교는 hmac.compare_digest 로 타이밍 상수화.
    """
    settings = get_settings()
    expected = settings.INTERNAL_API_TOKEN
    if not expected:
        raise HTTPException(status_code=403, detail="Internal endpoint disabled")
    provided = request.headers.get("x-internal-token", "")
    # bytes 로 비교 — str 끼리는 비-ASCII 포함 시 TypeError → 500 이 된다
    # (R16). 바이트 비교는 어떤 입력도 False 로 안전하게 거절한다.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid internal token")


async def _require_admin_or_internal(request: Request) -> None:
    """X-Internal-Token(머신·자동화) 또는 admin JWT(운영자 수동 작업) 게이트.

    다른 `/internal/*` 경로는 토큰만 받지만 cache/retry 는 스케줄러·수동 ops
    양쪽에서 호출될 수 있어 둘 중 하나면 통과한다. 토큰 불일치 시 admin
    인증으로 폴백 — 둘 다 아니면 401/403."""
    settings = get_settings()
    expected = settings.INTERNAL_API_TOKEN
    provided = request.headers.get("x-internal-token", "")
    if expected and hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        return
    user = await get_current_user(request)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")


@router.post("/internal/cache/retry", dependencies=[Depends(_require_admin_or_internal)])
async def retry_cache_invalidation(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Retry all unresolved cache invalidation failures.

    ⚠️ 인증 필요 — 예전엔 무인증이었는데 admin-api 가 ALB 로 전 경로 공개되어
       누구나 캐시 무효화 재시도를 발동할 수 있었다(R3-1). admin JWT 와
       X-Internal-Token 둘 다 받는다(스케줄러/수동 ops 양용)."""
    from app.core.cache_invalidation import CacheInvalidationManager

    cache_mgr: CacheInvalidationManager = request.app.state.cache_mgr
    resolved = await cache_mgr.retry_failed(session)
    return {"resolved": resolved}


@router.post("/internal/scheduler/run", dependencies=[Depends(_require_internal_token)])
async def trigger_aggregation(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Manual trigger for ROI aggregation (testing/debug only)."""
    settings = get_settings()
    if settings.is_production:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Not available in production")

    from app.core.usage_filters import current_kst_period
    from app.scheduler.roi_aggregator import aggregate_usage

    # 스케줄러(scheduler/main.py)와 **같은 월 정의**를 써야 한다 — 수동 트리거가 UTC
    # 월로 집계하면 월초 9시간 동안 cron 과 다른 달의 행을 만든다(§59).
    period = current_kst_period()
    await aggregate_usage(session, period)
    return {"status": "ok", "period": period}


@router.post("/internal/test/issue-key", dependencies=[Depends(_require_internal_token)])
async def test_issue_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """[DEV ONLY] Issue a VK without STS auth — for integration testing.

    ⚠️ prod 게이트 + `X-Internal-Token` 게이트 이중. dev 의 admin-api 도 ALB 로
       공개되어 있으므로 토큰 없이 무인증 VK 발급이 가능했던 것이 확인됐다."""
    settings = get_settings()
    if settings.is_production:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Not available in production")

    import uuid
    from datetime import date
    from decimal import Decimal, InvalidOperation

    from app.core.auth import CurrentUser
    from app.models.auth import User, UserRole
    from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType
    from app.repositories.budget_repository import BudgetRepository
    from app.repositories.user_repository import UserRepository
    from app.services.budget_service import DEFAULT_ALERT_THRESHOLDS
    from app.services.key_service import KeyService

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    email = body.get("email", "test@example.com")
    display_name = body.get("display_name", "Test User")
    # 비문자열(123, {})이 get_by_email 에 들어가면 DB 드라이버 오류로 500 —
    # DEV 전용이라도 입력 검증한다(R21 r3 N5).
    if not isinstance(email, str) or not email:
        raise HTTPException(400, "email must be a non-empty string")
    if not isinstance(display_name, str) or not display_name:
        raise HTTPException(400, "display_name must be a non-empty string")
    budget_usd_raw = body.get("budget_usd", settings.DEFAULT_USER_BUDGET_USD)
    expires_seconds = body.get("expires_seconds")  # optional: short-lived VK for testing

    # 숫자 필드는 명시 검증 — "abc" 같은 값이 Decimal()/비교 연산에서
    # InvalidOperation/TypeError 로 500 을 냈던 결함(R21 N7).
    try:
        budget_usd = Decimal(str(budget_usd_raw))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "budget_usd must be numeric") from None
    # NaN/Infinity 는 Decimal 으로 만들어지지만 `> 0` 비교에서 InvalidOperation
    # (→500) 을 낸다 — 유한성까지 확인한다(R21 r2 N7).
    if not budget_usd.is_finite():
        raise HTTPException(400, "budget_usd must be a finite number")
    if expires_seconds is not None:
        try:
            expires_seconds = int(expires_seconds)
        except (TypeError, ValueError):
            raise HTTPException(400, "expires_seconds must be a positive integer") from None
        # timedelta 오버플로(→500)도 400 으로.
        if not 0 < expires_seconds <= 365 * 24 * 3600:
            raise HTTPException(400, "expires_seconds must be a positive integer")

    # Find or create user
    user_repo = UserRepository(session)
    user = await user_repo.get_by_email(email)

    # 테스트 엔드포인트가 기존 ADMIN 계정의 VK 를 발급하면 내부 토큰 하나로
    # 관리자 가장 크리덴셜이 만들어진다 — DEV 전용이라도 차단(R21 N7).
    if user is not None and user.role == UserRole.ADMIN:
        raise HTTPException(400, "cannot issue test keys for ADMIN users")

    if user is None:
        team_id = uuid.UUID(settings.DEFAULT_TEAM_ID) if settings.DEFAULT_TEAM_ID else None
        # Bedrock metadata.user_id 가 이메일 형태를 거부하므로 UUID5 로 변환.
        # 동일 email → 동일 sub (재실행 idempotent), prod OIDC `sub` 와 동일한 UUID 형태.
        sso_subject = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"local-test:{email}"))
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name,
            role=UserRole.DEVELOPER,
            sso_subject=sso_subject,
            team_id=team_id,
            is_active=True,
        )
        user = await user_repo.create_user(user)

        # Create budget
        if budget_usd > 0 and team_id:
            budget_repo = BudgetRepository(session)
            budget = BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.USER,
                scope_id=user.id,
                max_budget_usd=budget_usd,
                period_type=PeriodType.MONTHLY,
                policy=BudgetPolicy.HARD_BLOCK,
                alert_thresholds=list(DEFAULT_ALERT_THRESHOLDS),
                allocated_by=user.id,
                effective_from=date.today(),
                is_active=True,
            )
            await budget_repo.upsert_config(budget)

    # Issue VK
    actor = CurrentUser(
        user_id=user.id,
        email=user.email,
        role=user.role,
        team_id=user.team_id,
    )
    key_service: KeyService = request.app.state.key_service
    key_result = await key_service.issue_key(
        session,
        user_id=user.id,
        actor=actor,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_seconds) if expires_seconds else None,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )

    # Invalidate budget config in Redis — H3: 이 키는 SET 하지 않는다.
    # app_clients 없는 페이로드 클로버 + 하드코딩 thresholds + ex= 없는 SET
    # (TTL 까지 폐기)이라 로그인마다 그 사용자의 앱별 예산 게이트와 운영자
    # 임계값을 깎았다. 게이트웨이가 미스 시 DB 에서 완전히 재도출하므로 DEL 만
    # 한다 — 예전 버전이 남긴 TTL 없는 잔존 키의 자가치유 경로이기도 하다.
    redis = request.app.state.redis
    budget_repo = BudgetRepository(session)
    user_budget = await budget_repo.get_active_config(BudgetScope.USER, user.id)
    if user_budget:
        try:
            await redis.delete(f"budget:config:user:{{{user.id}}}")
        except Exception:
            logger.warning("internal_budget_config_invalidate_failed", user_id=str(user.id))

    # NOTE: 모델 config Redis 캐시는 gateway-proxy 의 router_service 가 DB 에서
    # 동적 로드/관리. 여기서 미리 쓰던 fallback 은 옛 스키마 (model_id / BEDROCK / active)
    # 라 gateway-proxy 가 ModelConfigSchema validation 실패 → 401. 제거.

    return {
        "virtual_key": key_result.virtual_key,
        "key_id": key_result.key_id,
        "user_id": str(user.id),
        "team_id": str(user.team_id) if user.team_id else None,
        "email": user.email,
        "budget_usd": str(user_budget.max_budget_usd) if user_budget else "0",
        "expires_at": key_result.expires_at.isoformat() if key_result.expires_at else None,
        "gateway_endpoint": "http://localhost:8000",
    }


@router.get("/health")
async def health_check():
    """Liveness — 프로세스 alive 여부만 확인 (의존성 검사 X).

    DB/Redis 등 외부 의존성이 순간 느려지면 probe timeout → pod kill → cascade.
    pod 재시작으로 외부 의존성 장애가 해결되지 않으므로 liveness 엔 부적절.
    외부 의존성 상태는 /health/ready (readinessProbe 용) 에서 확인.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness_check():
    """Readiness — 프로세스 alive 여부만 확인 (의존성 검사 X).

    이전 구현은 매 probe 마다 DB `SELECT 1` + Redis ping 을 실행했는데,
    10k 동접 boot storm 부하테스트 (t3-vk-issue-10k-v2) 에서 15 pod 이
    **동시에 readiness probe timeout → 전원 not-ready → Service endpoint
    텅 빔 → 새 요청 전부 dial timeout** 의 cascade 가 관찰됨.

    gateway-proxy 가 이전에 같은 이유로 degradation 기준을 완화한 전례
    (`values-eks-fargate-prod.yaml` 의 C2 fix) 와 동일 맥락. DB/Redis 가
    실제로 죽으면 요청이 500 을 돌려주므로 k8s 수준의 빠른 failover 가
    오히려 burst 에 해로움.
    """
    return {"status": "ready"}
