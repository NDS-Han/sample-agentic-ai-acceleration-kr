# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.exceptions import NotFoundError, ValidationError
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
    """서비스 계정 목록 + 발급 준비 상태 — /keys 페이지의 관리 패널."""
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
                select(VirtualKey).where(
                    VirtualKey.user_id.in_(uids),
                    VirtualKey.status == KeyStatus.ACTIVE,
                    VirtualKey.kind == VK_KIND_SERVICE,
                )
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

    return ServiceAccountListResponse(
        items=items, max_ttl_days=get_settings().APP_KEY_MAX_TTL_DAYS
    )


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
    # FOR UPDATE — issue_key 도 같은 행을 잡고 플래그를 확인하므로, 잠금으로
    # 표지↔발급이 직렬화된다: 표지가 먼저면 발급은 flag=True 를 보고 거부되고,
    # 발급이 먼저면 표지의 폐기 수집이 그 신규 키를 본다.
    user = await UserRepository(session).get_by_email(
        body.email.strip(), for_update=True
    )
    if user is None:
        raise NotFoundError("User", body.email)
    if user.is_service_account:
        return {
            "user_id": str(user.id),
            "email": user.email,
            "is_service_account": True,
        }
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
    return {
        "user_id": str(user.id),
        "email": user.email,
        "is_service_account": True,
    }


@router.delete("/{user_id}")
async def unmark_service_account(
    user_id: uuid.UUID,
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """서비스 계정 표지 해제 — 활성 service 키가 남아 있으면 거부한다."""
    # FOR UPDATE — issue_key 의 같은 잠금과 직렬화해 "활성 service 키 확인
    # 후 해제" 와 "service 키 발급" 의 경합을 막는다.
    user = await UserRepository(session).get_user(user_id, for_update=True)
    if user is None:
        raise NotFoundError("User", str(user_id))
    if not user.is_service_account:
        return {"user_id": str(user.id), "is_service_account": False}

    active = (
        await session.execute(
            select(func.count())
            .select_from(VirtualKey)
            .where(
                VirtualKey.user_id == user.id,
                VirtualKey.kind == VK_KIND_SERVICE,
                VirtualKey.status == KeyStatus.ACTIVE,
            )
        )
    ).scalar_one()
    if active:
        raise ValidationError(
            "revoke the active service key before unflagging this account"
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

    user = await UserRepository(session).get_user(user_id)
    if user is None:
        raise NotFoundError("User", str(user_id))
    svc: KeyService = request.app.state.key_service
    # 세션을 넘기지 않는다 — repair 는 키별 commit 의 전용 세션을 연다
    # (요청 세션의 트랜잭션 경계와 분리).
    repaired = await svc.repair_service_key_redis(user_id=user_id)
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
    """서비스 계정의 키 단위 사용량 — /apps 상세 페이지.

    계정이 소유한 service 키들의 prefix 로 usage_logs 를 조인해 집계한다.
    재발급으로 prefix 가 바뀌어도 계정의 전체 이력이 보이고, 키별 분할도
    응답에 포함된다. key_prefix 귀속은 배포(USAGE_ATTRIBUTION_SINCE) 이후
    행에만 존재하므로 이전 사용은 집계에 나타나지 않는다.
    """
    period = period or current_kst_period()
    start_utc, end_utc = period_to_utc_range(period)

    cache_key = f"sa-usage:{user_id}:{period}"
    from app.routers.dashboard import _cache_get, _cache_set

    cached = await _cache_get(request, cache_key)
    if cached is not None:
        return cached

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

    vks = (
        await session.execute(
            select(VirtualKey).where(
                VirtualKey.user_id == user_id,
                VirtualKey.kind == VK_KIND_SERVICE,
            )
        )
    ).scalars().all()
    prefixes = [v.key_prefix for v in vks if v.key_prefix]

    usage_conds = [
        UsageLog.user_id == user_id,
        UsageLog.requested_at >= start_utc,
        UsageLog.requested_at < end_utc,
        UsageLog.key_prefix.in_(prefixes),
    ]
    success = UsageLog.status == "SUCCESS"
    token_sum = (
        UsageLog.input_tokens
        + UsageLog.output_tokens
        + UsageLog.cache_creation_tokens
        + UsageLog.cache_read_tokens
    )

    if prefixes:
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
                    func.count().filter(success),
                    func.sum(UsageLog.cost_usd).filter(success),
                    func.max(UsageLog.requested_at),
                )
                .where(*usage_conds)
                .group_by(UsageLog.key_prefix)
            )
        ).all()
        per_key = {p: (c, float(cost or 0), last) for p, c, cost, last in per_key_rows}

        by_model_rows = (
            await session.execute(
                select(
                    UsageLog.model_alias,
                    func.count().filter(success),
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
    else:
        summary_row = None
        per_key = {}
        by_model_rows = []
        daily_rows = []

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
    ) = summary_row if summary_row else (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None, None)

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
        ),
        by_model=by_model,
        daily=daily,
    )
    await _cache_set(request, cache_key, payload.model_dump(mode="json"), ttl=30)
    return payload
