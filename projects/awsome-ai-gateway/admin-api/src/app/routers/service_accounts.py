# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.usage_filters import (
    current_kst_period,
    period_to_utc_range,
    reporting_tz_sql,
)
from app.models.auth import (
    Department,
    KeyStatus,
    OrgAllowedClient,
    Team,
    TeamAllowedClient,
    User,
    UserAllowedClient,
    UserRole,
    VirtualKey,
)
from app.models.budget import BudgetScope
from app.models.model import ModelAlias, ModelStatus
from app.models.usage import UsageLog
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.schemas.keys import (
    IssueServiceAccountKeyRequest,
    ServiceAccountCreateRequest,
    ServiceAccountItem,
    ServiceAccountKeyUsageItem,
    ServiceAccountListResponse,
    ServiceAccountReadiness,
    ServiceAccountUsageByModel,
    ServiceAccountUsageDaily,
    ServiceAccountUsageResponse,
    ServiceAccountUsageSummary,
)
from app.services.key_service import VK_KIND_SERVICE

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/service-accounts", tags=["Service Accounts"])


async def _readiness(session: AsyncSession, user: User) -> ServiceAccountReadiness:
    """발급 준비 상태 — POST /admin/keys 의 하드 블로커와 같은 판정을
    발급 전에 UI 에 노출한다(운영자는 문서가 아니라 화면을 본다).

    - team_budget_ok: TEAM 예산이 없으면 모든 호출이 429.
    - allowed_clients_clear: client 'other' 인 앱은 user/team/org 어느 계층의
      allowed_clients 화이트리스트에도 들어갈 수 없다 — 행이 하나라도
      있으면 전 요청이 400 이라 발급 자체가 무의미.
    - usable_models: allowed_clients IS NULL 인 ACTIVE alias 수 — 0 이면
      호출 가능한 모델이 없다.
    """
    team_budget_ok = False
    if user.team_id is not None:
        cfg = await BudgetRepository(session).get_active_config(
            BudgetScope.TEAM, user.team_id
        )
        team_budget_ok = cfg is not None and cfg.max_budget_usd is not None

    uac = (
        await session.execute(
            select(func.count())
            .select_from(UserAllowedClient)
            .where(UserAllowedClient.user_id == user.id)
        )
    ).scalar_one()
    allowed_clients_clear = uac == 0
    if allowed_clients_clear and user.team_id is not None:
        tac = (
            await session.execute(
                select(func.count())
                .select_from(TeamAllowedClient)
                .where(TeamAllowedClient.team_id == user.team_id)
            )
        ).scalar_one()
        if tac:
            allowed_clients_clear = False
        else:
            org_id = (
                await session.execute(
                    select(Department.org_id)
                    .select_from(Team)
                    .join(Department, Department.id == Team.dept_id)
                    .where(Team.id == user.team_id)
                )
            ).scalar_one_or_none()
            if org_id is not None:
                oac = (
                    await session.execute(
                        select(func.count())
                        .select_from(OrgAllowedClient)
                        .where(OrgAllowedClient.org_id == org_id)
                    )
                ).scalar_one()
                if oac:
                    allowed_clients_clear = False

    usable_models = (
        await session.execute(
            select(func.count())
            .select_from(ModelAlias)
            .where(
                ModelAlias.status == ModelStatus.ACTIVE,
                ModelAlias.allowed_clients.is_(None),
            )
        )
    ).scalar_one()

    return ServiceAccountReadiness(
        team_budget_ok=team_budget_ok,
        allowed_clients_clear=allowed_clients_clear,
        usable_models=usable_models,
    )


@router.get("", response_model=ServiceAccountListResponse)
async def list_service_accounts(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정 목록 + 발급 준비 상태 — /service-accounts 페이지의 관리 패널."""
    rows = (
        await session.execute(
            select(User, Team.name)
            .outerjoin(Team, User.team_id == Team.id)
            .where(User.is_service_account.is_(True))
            .order_by(User.email)
        )
    ).all()

    uids = [u.id for u, _ in rows]
    vk_by_user: dict = {}
    usage_by_user: dict = {}
    key_last_req: dict = {}
    if uids:
        # 활성 서비스 키를 계정 전체에 대해 1쿼리로 — 계정당 N+1 이 아니다.
        vks = (
            await session.execute(
                select(VirtualKey)
                .where(
                    VirtualKey.user_id.in_(uids),
                    VirtualKey.status == KeyStatus.ACTIVE,
                    VirtualKey.kind == VK_KIND_SERVICE,
                )
                # 발급순 정렬 → dict 덮어쓰기로 최신 키가 남는다(활성 2개 이상인
                # 비정상 상태에서 결정적).
                .order_by(VirtualKey.issued_at)
            )
        ).scalars().all()
        vk_by_user = {v.user_id: v for v in vks}

        # 현재 리포팅 월 집계를 (user_id, key_prefix) 그룹 1쿼리로 — 비용·토큰은
        # §59 규칙대로 SUCCESS 행만 합산한다. key_prefix=NULL(구 attribution
        # 이전 행)도 유저 월간 총계에는 포함되고, 키별 last_request 에는 안 잡힌다.
        start_utc, end_utc = period_to_utc_range(current_kst_period())
        success = UsageLog.status == "SUCCESS"
        token_sum = (
            UsageLog.input_tokens
            + UsageLog.output_tokens
            + UsageLog.cache_creation_tokens
            + UsageLog.cache_read_tokens
        )
        agg_rows = (
            await session.execute(
                select(
                    UsageLog.user_id,
                    UsageLog.key_prefix,
                    func.count(),
                    func.sum(token_sum).filter(success),
                    func.sum(UsageLog.cost_usd).filter(success),
                    func.max(UsageLog.requested_at),
                )
                .where(
                    UsageLog.user_id.in_(uids),
                    UsageLog.requested_at >= start_utc,
                    UsageLog.requested_at < end_utc,
                )
                .group_by(UsageLog.user_id, UsageLog.key_prefix)
            )
        ).all()
        for uid, prefix, cnt, toks, cost, last_at in agg_rows:
            agg = usage_by_user.setdefault(
                uid, {"requests": 0, "tokens": 0, "cost": 0.0, "last": None}
            )
            agg["requests"] += cnt
            agg["tokens"] += toks or 0
            agg["cost"] += float(cost or 0)
            if last_at and (agg["last"] is None or last_at > agg["last"]):
                agg["last"] = last_at
            if prefix:
                key_last_req[(uid, prefix)] = last_at

    items: list[ServiceAccountItem] = []
    for user, team_name in rows:
        vk = vk_by_user.get(user.id)
        agg = usage_by_user.get(user.id, {})
        readiness = await _readiness(session, user)
        items.append(
            ServiceAccountItem(
                user_id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                team_id=str(user.team_id) if user.team_id else None,
                team_name=team_name,
                is_active=user.is_active,
                readiness=readiness,
                active_key_id=str(vk.id) if vk else None,
                active_key_prefix=vk.key_prefix if vk else None,
                active_key_issued_at=vk.issued_at if vk else None,
                active_key_expires_at=vk.expires_at if vk else None,
                last_request_at=agg.get("last"),
                active_key_last_request_at=(
                    key_last_req.get((user.id, vk.key_prefix)) if vk else None
                ),
                month_requests=agg.get("requests", 0),
                month_tokens=agg.get("tokens", 0),
                month_cost_usd=agg.get("cost", 0.0),
            )
        )

    settings = get_settings()
    return ServiceAccountListResponse(
        items=items,
        max_ttl_days=settings.APP_KEY_MAX_TTL_DAYS,
        service_keys_enabled=settings.SERVICE_KEYS_ENABLED,
    )


def _require_service_keys_enabled() -> None:
    """features.service_keys=false 배포는 표지·발급을 전부 403 거부.

    발급이 막힌 상태에서 표지만 허용하면 사람 계정이 service_account 로 잠기고
    (SSO·단기키 차단) 키는 못 받는 사고만 남는다. 해지(DELETE)는 복구 경로라
    열어둔다. GET 목록은 열려 있고 응답의 service_keys_enabled 로 UI 가 버튼을
    숨긴다.
    """
    if not get_settings().SERVICE_KEYS_ENABLED:
        raise ForbiddenError(
            "service account keys are disabled for this deployment "
            "(SERVICE_KEYS_ENABLED)"
        )


def _forbid_service_token_admin(admin: CurrentUser) -> None:
    """svc 토큰은 에이전트 생명주기를 못 건드린다 — 사람 admin(JWT) 전용.

    서비스 토큰은 require_admin 을 통과하지만(service_tokens.py 같은 규칙),
    장기 VK 발급/계정 표지는 또 다른 자격증명을 찍어내는 행위라 토큰이
    자기 권한을 확장할 수 없게 막는다.
    """
    if admin.is_service_token:
        raise ForbiddenError(
            "Service tokens cannot manage service accounts"
        )


async def _mark_user_as_service_account(
    session: AsyncSession,
    request: Request,
    user: User,
    admin: CurrentUser,
) -> bool:
    """표지 공통 로직 — 호출자는 user 를 FOR UPDATE 로 이미 잡은 상태여야 한다.

    Returns True if the flag was newly set (False when already flagged).
    """
    if user.is_service_account:
        return False
    if user.role == UserRole.ADMIN:
        raise ValidationError("cannot flag ADMIN users as service accounts")
    if not user.is_active:
        raise ValidationError("cannot flag a deactivated user")
    if user.id == admin.user_id:
        raise ValidationError("cannot flag yourself as a service account")

    user.is_service_account = True

    # 표지와 함께 기존 사람용 키(exchange/sts 등)를 폐기한다 — 남겨두면
    # 서비스 계정이 된 계정의 단기키가 잔여 TTL 동안 계속 인증된다.
    # revoke_key 가 FOR UPDATE 잠금 + commit 후 Redis DEL enqueue 를 처리한다.
    from app.repositories.key_repository import KeyRepository
    from app.services.key_service import KeyService

    svc: KeyService = request.app.state.key_service
    revocable = await KeyRepository(session).list_revocable_for_users([user.id])
    for old_vk in revocable:
        if old_vk.kind == VK_KIND_SERVICE:
            continue
        try:
            await svc.revoke_key(
                session,
                key_id=old_vk.id,
                actor=admin,
                ip_address=request.client.host if request.client else "0.0.0.0",
                request_id=getattr(request.state, "request_id", "")
                or request.headers.get("x-request-id", ""),
                # EXPIRED 행은 수신자에게 의미 없는 폐기 메일이 나간다 —
                # force_reauth 와 같은 규칙으로 ACTIVE 만 통지, 표지 일괄
                # 폐기는 bulk 로 표시해 admin 수신자 fan-out 을 막는다.
                bulk=True,
                notify=old_vk.status == KeyStatus.ACTIVE,
            )
        except (NotFoundError, ValidationError):
            # 조회 후 잠금 전에 다른 요청이 같은 키를 폐기/삭제한 경우 —
            # 이미 목표 상태이므로 건너뛰고 표지는 진행한다.
            continue

    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="MARK_SERVICE_ACCOUNT",
        resource_type="User",
        resource_id=str(user.id),
        changes={"after": {"email": user.email, "is_service_account": True}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return True


@router.post("", status_code=201)
async def mark_service_account(
    body: ServiceAccountCreateRequest,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """기존 사용자를 서비스 계정으로 표지 — 장기 VK 발급 대상이 된다.

    계정 자체는 Cognito/IdP 에서 생성·동기화된다(조직 규약). 이 엔드포인트는
    그 계정에 '서비스 계정' 표지만 단다. ADMIN 과 비활성 계정은 거부.
    """
    _forbid_service_token_admin(admin)
    _require_service_keys_enabled()
    # FOR UPDATE — issue_key 도 같은 행을 잡고 플래그를 확인하므로, 잠금으로
    # 표지↔발급이 직렬화된다: 표지가 먼저면 발급은 flag=True 를 보고 거부되고,
    # 발급이 먼저면 표지의 폐기 수집이 그 신규 키를 본다.
    user = await UserRepository(session).get_by_email(
        body.email.strip(), for_update=True
    )
    if user is None:
        raise NotFoundError("User", body.email)
    await _mark_user_as_service_account(session, request, user, admin)
    return {
        "user_id": str(user.id),
        "email": user.email,
        "is_service_account": True,
    }


@router.post("/{user_id}/issue", status_code=201)
async def mark_and_issue_service_key(
    user_id: uuid.UUID,
    body: IssueServiceAccountKeyRequest,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """표지+발급을 한 트랜잭션으로 — 부분 실패로 사람 계정이 잠기지 않는다.

    별도 요청으로 나뉘면 표지 commit 후 발급이 실패(팀/예산 미비)할 때
    계정이 service_account 로 남아 사람 로그인·단기키가 모두 차단된 채
    갇힌다. CommittingRoute 가 응답 뒤 한 번만 commit 하므로 어느 단계든
    예외면 표지·폐기·발급이 함께 롤백된다.
    """
    from app.services.key_service import KeyService, assert_service_key_ready

    _forbid_service_token_admin(admin)
    _require_service_keys_enabled()

    settings = get_settings()
    max_days = settings.APP_KEY_MAX_TTL_DAYS
    ttl_days = body.ttl_days if body.ttl_days is not None else max_days
    if not 1 <= ttl_days <= max_days:
        raise ValidationError(
            f"ttl_days must be between 1 and {max_days} (APP_KEY_MAX_TTL_DAYS)"
        )

    user = await UserRepository(session).get_user(user_id, for_update=True)
    if user is None:
        raise NotFoundError("User", str(user_id))

    # 표지 먼저 — 실패하면 발급 없이 전체 롤백. 이미 표지면 no-op.
    await _mark_user_as_service_account(session, request, user, admin)

    # 발급 readiness — 실패해도 위 표지/폐기가 함께 롤백된다.
    await assert_service_key_ready(session, user)

    svc: KeyService = request.app.state.key_service
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    key_result = await svc.issue_key(
        session,
        user_id=user.id,
        actor=admin,
        expires_at=expires_at,
        kind=VK_KIND_SERVICE,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "")
        or request.headers.get("x-request-id", ""),
        user=user,
    )
    return {
        "key_id": key_result.key_id,
        "key_prefix": key_result.key_prefix,
        "user_id": key_result.user_id,
        "status": key_result.status,
        "expires_at": key_result.expires_at.isoformat() if key_result.expires_at else None,
        "virtual_key": key_result.virtual_key,
    }


@router.delete("/{user_id}")
async def unmark_service_account(
    user_id: uuid.UUID,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정 표지 해제 — 활성 service 키가 남아 있으면 거부한다."""
    _forbid_service_token_admin(admin)
    # FOR UPDATE — issue_key 의 같은 잠금과 직렬화해 "활성 service 키 확인
    # 후 해제" 와 "service 키 발급" 의 경합을 막는다.
    user = await UserRepository(session).get_user(user_id, for_update=True)
    if user is None:
        raise NotFoundError("User", str(user_id))
    if not user.is_service_account:
        return {"user_id": str(user.id), "is_service_account": False}

    # ACTIVE 외에 status=EXPIRED·expires_at>now 도 게이트웨이가 여전히 인증한다
    # (Redis key:vk TTL = expires_at) — list_revocable_for_users 와 같은 술어.
    now = datetime.now(UTC)
    live = (
        await session.execute(
            select(func.count())
            .select_from(VirtualKey)
            .where(
                VirtualKey.user_id == user.id,
                VirtualKey.kind == VK_KIND_SERVICE,
                or_(
                    VirtualKey.status == KeyStatus.ACTIVE,
                    and_(
                        VirtualKey.status == KeyStatus.EXPIRED,
                        VirtualKey.expires_at > now,
                    ),
                ),
            )
        )
    ).scalar_one()
    if live:
        raise ValidationError(
            "revoke the live service key before unflagging this account"
        )

    user.is_service_account = False
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="UNMARK_SERVICE_ACCOUNT",
        resource_type="User",
        resource_id=str(user.id),
        changes={"after": {"email": user.email, "is_service_account": False}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "") or request.headers.get("x-request-id", ""),
    )
    return {"user_id": str(user.id), "is_service_account": False}


@router.post("/{user_id}/repair")
async def repair_service_account_keys(
    user_id: uuid.UUID,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """해당 계정의 ACTIVE service 키 ``key:vk:{hash}`` Redis 엔트리 재기록.

    gateway 의 VK→user 매핑은 Redis 가 유일한 소스 — 축출/failover 로
    유실되면 DB 에 유효한 키가 있어도 401 이다. 대시보드의 수동 복구 버튼
    (스케줄러가 주기적으로 같은 코드를 돌린다).
    """
    from app.services.key_service import KeyService

    _forbid_service_token_admin(admin)

    user = await UserRepository(session).get_user(user_id)
    if user is None:
        raise NotFoundError("User", str(user_id))
    svc: KeyService = request.app.state.key_service
    # 세션을 넘기지 않는다 — repair 는 키별 commit 의 전용 세션을 연다
    # (요청 세션의 트랜잭션 경계와 분리).
    repaired = await svc.repair_service_key_redis(user_id=user_id)
    # 자격증명 재기록은 인증 경로를 만지는 쓰기라 감사 대상 — 별도 세션의
    # 키별 commit 과 무관하게 요청 트랜잭션의 한 행으로 남긴다.
    await audit_logger.log(
        session,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="REPAIR_SERVICE_ACCOUNT_KEYS",
        resource_type="User",
        resource_id=str(user.id),
        changes={"after": {"email": user.email, "repaired": repaired}},
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "")
        or request.headers.get("x-request-id", ""),
    )
    return {"repaired": repaired}


# 키 단위 귀속(usage_logs.key_prefix)이 켜진 시점 — 이보다 이전 행은
# prefix=NULL 이라 키별 집계에서 제외된다. 배포 시점 상수로, UI 가
# "집계 개시일" 안내에 쓴다.
USAGE_ATTRIBUTION_SINCE = datetime(2026, 11, 4, tzinfo=UTC)


def _kst_day_expr():
    """usage_logs.requested_at → 리포팅 타임존 'YYYY-MM-DD' 문자열 식.

    GROUP BY/SELECT 전용(WHERE 절은 period_to_utc_range 바인드를 쓴다 —
    kst_month_expr 주석 참조).
    """
    return func.to_char(
        func.timezone(reporting_tz_sql(), UsageLog.requested_at),
        literal("YYYY-MM-DD", literal_execute=True),
    )


@router.get("/{user_id}/usage", response_model=ServiceAccountUsageResponse)
async def service_account_usage(
    user_id: uuid.UUID,
    request: Request,
    period: str | None = Query(
        default=None, description="YYYY-MM (리포팅 타임존). 미지정 시 현재 월"
    ),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정의 키 단위 사용량 — /service-accounts/{id} 상세 페이지.

    계정 총계(summary/by_model/daily)는 user_id+기간 스코프라 목록의 month_*
    와 정합하고, 키별 분할(per_key)은 이 계정이 소유한 service 키 prefix 로
    집계한다. 0052 백필이 service 키 수명 구간으로 구 행을 귀속했으므로
    재발급 이력도 표시된다. 어느 키에도 안 잡힌 행(귀속 이전/다른 키)은
    unattributed_* 에 모인다.
    """
    period = period or current_kst_period()
    start_utc, end_utc = period_to_utc_range(period)

    # 검증이 캐시보다 먼저 — 언플래그된 계정의 캐시 잔재가 최대 TTL 동안
    # 응답되는 것을 막는다.
    row = (
        await session.execute(
            select(User, Team.name)
            .outerjoin(Team, User.team_id == Team.id)
            .where(User.id == user_id, User.is_service_account.is_(True))
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("ServiceAccount", str(user_id))
    user, team_name = row

    cache_key = f"sa-usage:{user_id}:{period}"
    from app.routers.dashboard import _cache_get, _cache_set

    cached = await _cache_get(request, cache_key)
    if cached is not None:
        return cached

    vks = (
        await session.execute(
            select(VirtualKey).where(
                VirtualKey.user_id == user_id,
                VirtualKey.kind == VK_KIND_SERVICE,
            )
        )
    ).scalars().all()

    # 계정 총계는 user_id+기간 스코프 — key_prefix=NULL(배포 공백/구 행)도
    # 포함해 목록 API 의 month_* 와 정합하게 만든다. prefix 조건은 키별 분할
    # (per_key)에만 적용하고, NULL 버킷은 unattributed 필드로 드러낸다.
    usage_conds = [
        UsageLog.user_id == user_id,
        UsageLog.requested_at >= start_utc,
        UsageLog.requested_at < end_utc,
    ]
    success = UsageLog.status == "SUCCESS"
    token_sum = (
        UsageLog.input_tokens
        + UsageLog.output_tokens
        + UsageLog.cache_creation_tokens
        + UsageLog.cache_read_tokens
    )

    summary_row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(UsageLog.status == "SUCCESS"),
                func.count().filter(UsageLog.status == "ERROR"),
                func.count().filter(UsageLog.status == "TIMEOUT"),
                func.sum(UsageLog.input_tokens).filter(success),
                func.sum(UsageLog.output_tokens).filter(success),
                func.sum(UsageLog.cache_creation_tokens).filter(success),
                func.sum(UsageLog.cache_read_tokens).filter(success),
                func.sum(UsageLog.reasoning_tokens).filter(success),
                func.sum(UsageLog.web_search_count).filter(success),
                func.sum(UsageLog.cost_usd).filter(success),
                func.avg(UsageLog.latency_ms).filter(success),
                func.max(UsageLog.requested_at),
            ).where(*usage_conds)
        )
    ).one()

    per_key_rows = (
        await session.execute(
            select(
                UsageLog.key_prefix,
                func.count(),
                func.sum(UsageLog.cost_usd).filter(success),
                func.max(UsageLog.requested_at),
            )
            .where(*usage_conds)
            .group_by(UsageLog.key_prefix)
        )
    ).all()
    per_key = {p: (c, float(cost or 0), last) for p, c, cost, last in per_key_rows if p}
    # unattributed = 계정 총계 − 이 계정의 service 키에 매칭된 분 — NULL 행
    # (귀속 이전)뿐 아니라, 표지 전에 쓰던 exchange 키 등 "다른 키" 사용분도
    # 전부 여기 잡힌다. 어느 쪽이든 키 표에 나타나지 않는 나머지다.
    matched_req = sum(per_key.get(vk.key_prefix, (0, 0.0, None))[0] for vk in vks)
    matched_cost = sum(per_key.get(vk.key_prefix, (0, 0.0, None))[1] for vk in vks)

    by_model_rows = (
        await session.execute(
            select(
                UsageLog.model_alias,
                func.count(),
                func.sum(token_sum).filter(success),
                func.sum(UsageLog.cost_usd).filter(success),
            )
            .where(*usage_conds)
            .group_by(UsageLog.model_alias)
        )
    ).all()
    daily_rows = (
        await session.execute(
            select(
                _kst_day_expr().label("d"),
                func.count(),
                func.sum(token_sum).filter(success),
                func.sum(UsageLog.cost_usd).filter(success),
            )
            .where(*usage_conds)
            .group_by("d")
            .order_by("d")
        )
    ).all()

    (
        total_req,
        n_success,
        n_error,
        n_timeout,
        in_tok,
        out_tok,
        cc_tok,
        cr_tok,
        rsn_tok,
        ws_cnt,
        cost_sum,
        avg_lat,
        last_req,
    ) = summary_row

    total_cost = float(cost_sum or 0)
    by_model = [
        ServiceAccountUsageByModel(
            model_alias=alias,
            requests=cnt,
            tokens=toks or 0,
            cost_usd=float(cost or 0),
            cost_share=(float(cost or 0) / total_cost if total_cost else 0.0),
        )
        for alias, cnt, toks, cost in sorted(
            by_model_rows, key=lambda r: float(r[3] or 0), reverse=True
        )
    ]
    daily = [
        ServiceAccountUsageDaily(
            date=d, requests=cnt, tokens=toks or 0, cost_usd=float(cost or 0)
        )
        for d, cnt, toks, cost in daily_rows
    ]
    keys = [
        ServiceAccountKeyUsageItem(
            key_id=str(vk.id),
            key_prefix=vk.key_prefix,
            status=vk.status,
            issued_at=vk.issued_at,
            expires_at=vk.expires_at,
            revoked_at=vk.revoked_at,
            last_used_at=vk.last_used_at,
            requests=per_key.get(vk.key_prefix, (0, 0.0, None))[0],
            cost_usd=per_key.get(vk.key_prefix, (0, 0.0, None))[1],
            last_request_at=per_key.get(vk.key_prefix, (0, 0.0, None))[2],
        )
        for vk in vks
    ]

    payload = ServiceAccountUsageResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        team_id=str(user.team_id) if user.team_id else None,
        team_name=team_name,
        period=period,
        attribution_since=USAGE_ATTRIBUTION_SINCE,
        keys=keys,
        summary=ServiceAccountUsageSummary(
            requests=total_req,
            success=n_success,
            error=n_error,
            timeout=n_timeout,
            input_tokens=in_tok or 0,
            output_tokens=out_tok or 0,
            cache_creation_tokens=cc_tok or 0,
            cache_read_tokens=cr_tok or 0,
            reasoning_tokens=rsn_tok or 0,
            web_search_count=ws_cnt or 0,
            cost_usd=total_cost,
            avg_latency_ms=float(avg_lat) if avg_lat is not None else None,
            last_request_at=last_req,
            unattributed_requests=max(0, total_req - matched_req),
            # float 합산 잔차로 요청 0건인데 $0.000001 미귀속이 뜨는 걸 막는다 —
            # 0건이면 비용도 0으로 고정한다.
            unattributed_cost_usd=(
                max(0.0, total_cost - matched_cost)
                if total_req - matched_req > 0
                else 0.0
            ),
        ),
        by_model=by_model,
        daily=daily,
    )
    # 과거 월은 데이터가 불변이므로 더 길게 캐시한다.
    ttl = 30 if period == current_kst_period() else 600
    await _cache_set(request, cache_key, payload.model_dump(mode="json"), ttl=ttl)
    return payload
