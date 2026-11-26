# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import read_cache
from app.core.auth import CurrentUser, require_admin
from app.core.db import get_analytics_session
from app.core.usage_filters import current_kst_period, reporting_timezone
from app.models.auth import KeyStatus, User, VirtualKey
from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.models.model import ModelAlias, ModelStatus
from app.services import usage_reads as reads
from app.services import usage_rollup as ur
from app.services.web_search_cost import (
    WebSearchUsage,
    elapsed_days,
    get_price,
    reporting_today,
    web_search_deployed,
    ws_from_totals,
    ws_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/dashboard", tags=["Dashboard"])

#: 대시보드 응답 캐시 TTL(초). 30초를 고른 이유: 관리자가 예산/모델/키를 바꾼 뒤 화면에
#: 반영되기까지의 최대 지연이 그만큼이라는 뜻이므로, 체감상 "즉시" 로 남는 상한이다.
#: 더 늘리면 편집 후 화면이 낡아 보여 운영자가 새로고침을 반복하게 된다.
_DASHBOARD_CACHE_TTL = 30


def _cache_key(name: str, **parts: object) -> str:
    """`dashboard:<name>:<k=v>...` 형태의 캐시 키.

    ⚠️ 값 정규화가 중요하다. 이 라우터의 필터는 미지정이 `None` 인데 admin-ui 는 전체를
    뜻할 때 `'all'` 을 보낸다. 정규화하지 않으면 같은 질의가 두 키에 나뉘어 캐시 적중률이
    반토막 나고, 더 나쁘게는 한쪽만 무효화되어 두 값이 갈린다.
    """
    norm = "&".join(
        f"{k}={'all' if v in (None, '', 'all') else v}" for k, v in sorted(parts.items())
    )
    return f"dashboard:{name}:{norm}"


async def _cache_get(request: Request, key: str):
    """캐시 조회. **어떤 실패도 삼킨다**(fail-open).

    ⚠️ Redis 장애가 대시보드를 죽여서는 안 된다 — 캐시는 성능 장치일 뿐 정합성의
    근거가 아니다. 그래서 예외를 올리지 않고 miss 로 취급한다. 다만 조용히 넘기면
    "캐시가 영원히 안 맞는" 상태를 아무도 모르므로 debug 로 흔적은 남긴다.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 — 캐시 실패는 요청 실패가 아니다
        logger.debug("dashboard cache get failed key=%s err=%s", key, exc)
        return None


async def _cache_set(
    request: Request, key: str, value: object, *, ttl: int = _DASHBOARD_CACHE_TTL
) -> None:
    """캐시 저장. 조회와 같은 이유로 실패를 삼킨다."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.debug("dashboard cache set failed key=%s err=%s", key, exc)


async def _cached(request: Request, key: str, rng: ur.DayRange, compute):
    """Redis 캐시 + single-flight(core/read_cache) — 같은 키의 동시 요청은 질의 한 번을 공유한다.

    TTL: 오늘이 포함된 기간은 _DASHBOARD_CACHE_TTL(30s), 다 지난 기간은 더 길게.
    """
    return await read_cache.cached(
        getattr(request.app.state, "redis", None), key, compute,
        ttl=read_cache.ttl_for(rng.end, ur.reporting_today()),
    )


def _month(period: str | None) -> tuple[str, ur.DayRange]:
    period = period or current_kst_period()
    return period, ur.month_range(period)  # 형식 오류 400


@router.get("/summary")
async def dashboard_summary(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    period, rng = _month(period)

    # ⚠️ 캐시가 안전한 근거: 이 라우터의 모든 핸들러가 `require_admin` 이므로 응답이
    #    **행위자에 따라 달라지지 않는다**. 그래서 키에 actor 를 넣지 않아도 된다.
    #    (대조: /admin/analytics 는 require_admin_or_team_leader 라 같은 파라미터가
    #     ADMIN 에겐 전사·TEAM_LEADER 에겐 팀 범위를 뜻한다 — 그 캐시는 해석된 팀 집합을
    #     키에 넣는다.)
    cache_key = _cache_key("summary", period=period, client=client)

    async def compute():
        # 성공/에러 구분은 집계 FILTER — 한 번의 하이브리드 읽기에 성공 집계와 에러 수를
        # 같이 얻는다. 정책 차단(429·예산 차단·인증 실패)은 usage_logs 에 행이 없어 자연 배제.
        scope = ur.RollupScope.of(rng, client=client)
        t = await reads.totals(session, scope)
        meta = await ur.fetch_meta(session, scope)
        active_users = t.active_users
        cost_per_user = (t.total_cost / active_users) if active_users > 0 else Decimal(0)
        return {
            "period": period,
            "total_requests": t.total_requests,
            "error_requests": t.error_requests,
            "cancelled_requests": t.cancelled_requests,
            "billable_requests": t.billable_requests,
            "total_tokens": t.total_tokens,
            # 모델 토큰 비용 — 청구 대상 전 상태(wave 3). 청구 총액·분해는 charged.
            "total_cost_usd": round(float(t.total_cost), 4),
            "charged": t.charged(),
            "active_users": active_users,
            "cost_per_user_usd": round(float(cost_per_user), 4),
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


@router.get("/model-share")
async def model_share(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    team_id: str = Query(default="all", description="UUID 또는 'all'"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    period, rng = _month(period)

    team_filter: str = "all"
    teams = None
    if team_id and team_id != "all":
        try:
            team_uuid = uuid.UUID(team_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_team_id")
        teams = [team_uuid]
        team_filter = str(team_uuid)

    # 응답을 바꾸는 파라미터 **전부**를 키에 넣는다 — 하나라도 빠지면 다른 질의의
    # 결과가 반환된다(team_id 를 빼면 A팀 화면에 B팀 점유율이 뜨는 식).
    cache_key = _cache_key("model-share", period=period, team_id=team_filter, client=client)

    async def compute():
        # 비용 집계 표준: 청구 대상(상태 무관, wave 3) + 리포팅 tz 월 경계.
        scope = ur.RollupScope.of(rng, teams=teams, client=client, billable_only=True)
        rows = await reads.model_share(session, scope)
        meta = await ur.fetch_meta(session, scope)
        total = sum(float(r.cost_usd or 0) for r in rows)
        models = []
        for r in rows:
            cost = float(r.cost_usd or 0)
            share = (cost / total * 100) if total > 0 else 0.0
            models.append({
                "model_alias": r.model_alias,
                "display_name": r.display_name,
                "cost_usd": round(cost, 6),
                "share_pct": round(share, 2),
            })
        return {
            "period": period,
            "team_id": team_filter,
            # 6자리 — 소액 호출($0.00001 규모)이 4자리 반올림에서 0 이 되면
            # 프론트의 `total_cost_usd === 0` 공백 판정에 "사용 기록 없음"으로 뜬다.
            "total_cost_usd": round(total, 6),
            "models": models,
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


@router.get("/client-share")
async def client_share(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """앱별(client) 비용 점유율 — claude-code / cowork / codex / other(legacy NULL 포함).

    롤업 client 는 이미 COALESCE(client,'other') 로 접혀 있다(raw 분기도 같은 식).
    비용 집계 표준(청구 대상 전 상태 + 리포팅 tz 월 경계) 동일 적용. admin-ui ClientShareResponse 형태.
    """
    period, rng = _month(period)
    cache_key = _cache_key("client-share", period=period)

    async def compute():
        # 모델 비용·호출 수는 청구 대상(상태 무관, wave 3). 웹 검색도 전 status(실행·과금됨).
        scope = ur.RollupScope.of(rng)
        rows = [
            r for r in await reads.client_share(session, scope)
            # 청구가 없는 앱(0 토큰 실패 마커만)은 숨긴다 — 검색을 썼으면 비용이 있으므로 남긴다.
            if (r.call_count or 0) > 0 or (r.ws_queries or 0) > 0
        ]
        settings = await ws_settings(session)
        meta = await ur.fetch_meta(session, scope)

        total = sum(float(r.cost_usd or 0) for r in rows)
        clients = []
        for r in rows:
            cost = float(r.cost_usd or 0)
            share = (cost / total * 100) if total > 0 else 0.0
            ws = ws_from_totals(r.ws_queries, r.ws_cost, r.ws_untracked, settings)
            clients.append({
                "client": r.client,
                # 6자리 — 소액 호출이 4자리 반올림에서 0 으로 사라진다(model-share 와 동일).
                # 모델 비용(청구 대상 전 상태) — 점유율(share_pct)도 모델 비용 기준이다.
                "cost_usd": round(cost, 6),
                "share_pct": round(share, 2),
                "call_count": int(r.call_count or 0),
                "web_search_count": ws.queries,
                # 웹 검색 비용(청구 총액의 일부, 자기 줄). None = 미추적.
                "web_search_cost_usd": ws.cost_usd,
                "web_search_untracked_count": ws.untracked_queries,
            })

        base = ws_from_totals(0, None, 0, settings)
        ws_total = WebSearchUsage(
            queries=sum(c["web_search_count"] for c in clients),
            cost_usd=(
                round(sum(c["web_search_cost_usd"] for c in clients
                          if c["web_search_cost_usd"] is not None), 6)
                if any(c["web_search_cost_usd"] is not None for c in clients) else None
            ),
            untracked_queries=sum(c["web_search_untracked_count"] for c in clients),
            tracking_since=base.tracking_since if rows else None,
            deployed=web_search_deployed(),
            **({"price_per_1k_queries": base.price_per_1k_queries} if rows else {}),
        )
        return {
            "period": period,
            "total_cost_usd": round(total, 6),
            "clients": clients,
            "web_search": ws_total.model_dump(mode="json"),
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


@router.get("/top-users")
async def top_users(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """실제 비용 기준 상위 사용자(§60.8) — 청구 대상(전 상태)+리포팅 tz 월 집계를 cost 내림차순.

    ⚠️ 기존 대시보드 'Top 사용자 by 비용' 위젯은 budgets/summary(예산설정된 사용자만)를
    써서 예산 없는 헤비유저를 누락했다(라벨='비용'인데 실제론 예산설정자 중 사용액).
    이 엔드포인트는 챗(text2SQL)과 동일하게 사용량 전체에서 진짜 top spender 를 낸다.
    PII 금지: sso_subject 미노출(display_name·email 만).
    """
    period, rng = _month(period)
    cache_key = _cache_key("top-users", period=period, client=client, limit=limit)

    async def compute():
        scope = ur.RollupScope.of(rng, client=client, billable_only=True)
        rows = await reads.top_users(session, scope, limit)
        meta = await ur.fetch_meta(session, scope)
        return {
            "period": period,
            "users": [
                {
                    # 표시명은 UI 가 team_name + department_name 을 canonical 규칙
                    # (부서_팀)으로 조합한다 — 동명 팀이 부서별로 존재할 수 있다.
                    "user_id": str(r.user_id),
                    "name": r.name,
                    "email": r.email,
                    "team_id": str(r.team_id) if r.team_id else None,
                    "team_name": r.team_name,
                    "department_name": r.department_name,
                    "cost_usd": round(float(r.cost_usd or 0), 4),
                    "call_count": int(r.call_count or 0),
                }
                for r in rows
            ],
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


@router.get("/top-teams")
async def top_teams(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """실제 비용 기준 상위 팀(§60.9) — 청구 대상(전 상태)+리포팅 tz 월 집계를 cost 내림차순.

    **팀 귀속은 요청 시점 team_id**(롤업 입도에 그대로 있다 — users.team_id 경유 금지:
    팀 이동 사용자의 과거 비용 오귀속 방지).
    """
    period, rng = _month(period)
    cache_key = _cache_key("top-teams", period=period, client=client, limit=limit)

    async def compute():
        scope = ur.RollupScope.of(rng, client=client, billable_only=True)
        rows = await reads.top_teams(session, scope, limit)
        meta = await ur.fetch_meta(session, scope)
        return {
            "period": period,
            "teams": [
                {
                    # 같은 이름의 팀이 부서별로 중복 존재할 수 있어(예: NDS/SSIR 산하
                    # 동명 Developers) 표시명은 UI 가 department_name 과 조합해 만든다.
                    "team_id": str(r.team_id),
                    "name": r.name,
                    "department_name": r.department_name,
                    "cost_usd": round(float(r.cost_usd or 0), 4),
                    "call_count": int(r.call_count or 0),
                }
                for r in rows
            ],
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


async def _budget_parts(session: AsyncSession, period: str) -> tuple[Decimal, Decimal]:
    """(budget_used, total_limit) — 예산 사용률 분자·분모. 작은 테이블만 읽는다."""
    # ── 예산 한도(분모): TEAM + 팀 없는 USER 의 활성 config 합계.
    #    LEFT JOIN 후 team_id IS NULL 조건으로 "팀 없는 USER" 를 고른다. USER config 의
    #    scope_id 가 auth.users 에 없는 고아 행이면 조인이 NULL 이 되어 포함되는데,
    #    그건 팀 소속을 확인할 수 없는 예산이므로 합산 대상으로 두는 편이 안전하다
    #    (누락시 사용률이 과대평가된다).
    limit_row = (
        await session.execute(
            select(func.coalesce(func.sum(BudgetConfig.max_budget_usd), 0).label("total_limit"))
            .select_from(BudgetConfig)
            .outerjoin(
                User,
                and_(
                    BudgetConfig.scope == BudgetScope.USER,
                    User.id == BudgetConfig.scope_id,
                ),
            )
            .where(
                BudgetConfig.is_active.is_(True),
                # client IS NULL = 총한도 행만. per-app config(claude-code 등)는
                # 그 부분집합이라 더하면 팀없는 USER 의 한도가 이중 합산된다.
                BudgetConfig.client.is_(None),
                or_(
                    BudgetConfig.scope == BudgetScope.TEAM,
                    and_(
                        BudgetConfig.scope == BudgetScope.USER,
                        User.team_id.is_(None),
                    ),
                ),
            )
        )
    ).one()
    total_limit = Decimal(str(limit_row.total_limit or 0))

    # ── 예산 사용액(분자): **budget_usages** — 집행과 같은 계수.
    #    usage_logs SUCCESS 합계는 실패 호출 비용과 시드 이관분을 빼먹어,
    #    사용자가 "used $13" 을 보는 동안 enforcement 는 $963 에 하드블록하는
    #    불일치가 생겼다. 분모(한도)와 같은 스코프(TEAM + 팀없는 USER, 총합 행만)로
    #    합산한다. client 필터는 적용하지 않는다(예산은 client 축과 무관).
    #    단, 워커는 config 유무와 무관하게 budget_usages 를 UPSERT 하므로 분모와
    #    스코프를 맞추려면 "활성 총한도 config 가 있는 스코프" 로 한정해야 한다
    #    — 안 하면 한도 없는 스코프의 사용액이 분자에만 들어가 사용률이 100% 를
    #    넘는다 (R25-S1).
    usage_scope_has_active_config = exists(
        select(BudgetConfig.id).where(
            and_(
                BudgetConfig.scope == BudgetUsage.scope,
                BudgetConfig.scope_id == BudgetUsage.scope_id,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
                # R25-r2 S1-잔여: D-cap(기본 예산 하향)만 저장된 config 는
                # max_budget_usd=NULL 이라 분모 합산에서 0으로 빠지는데 분자에는
                # 사용액이 들어가 사용률이 900%+ 로 부풀었다. 실제 한도가 있는
                # config 만 "한도 있음" 으로 본다.
                BudgetConfig.max_budget_usd.isnot(None),
            )
        ).correlate(BudgetUsage)
    )
    budget_used_row = (
        await session.execute(
            select(func.coalesce(func.sum(BudgetUsage.used_usd), 0).label("used"))
            .select_from(BudgetUsage)
            .outerjoin(
                User,
                and_(
                    BudgetUsage.scope == BudgetScope.USER,
                    User.id == BudgetUsage.scope_id,
                ),
            )
            .where(
                BudgetUsage.period == period,
                BudgetUsage.client.is_(None),
                or_(
                    BudgetUsage.scope == BudgetScope.TEAM,
                    and_(
                        BudgetUsage.scope == BudgetScope.USER,
                        User.team_id.is_(None),
                    ),
                ),
                usage_scope_has_active_config,
            )
        )
    ).one()
    return Decimal(str(budget_used_row.used or 0)), total_limit


@router.get("/kpi")
async def dashboard_kpi(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """대시보드 상단 KPI 카드 일괄 — 화면 1개당 API 1개.

    ## 왜 필요한가

    예전 화면은 카드를 채우려고 4개 API 를 동시에 불렀고, 그중
    `GET /admin/budgets/summary` 가 병목이었다(config 하나당 Redis GET + usage SUM 순차).
    KPI 카드에 필요한 건 **합계 두 개**(사용액·한도)뿐이라, 그걸 SQL 집계 한 번으로 낸다.

    ## 예산 사용률을 이렇게 계산하는 이유

    분모(한도)는 **TEAM 예산 + 팀이 없는 USER 예산**만 더한다. 팀에 속한 USER 예산을
    함께 더하면 같은 지출 한도를 팀 축과 개인 축에서 **이중계상**한다(팀 예산이 이미 그
    멤버들을 포함한다). 분자(사용액)는 `budget.budget_usages`(집행과 같은 계수)에서 읽고
    스코프를 분모와 맞춘다(R25-S1). 사용량 카드(요청·비용·토큰·활성 사용자·웹 검색)는
    하이브리드 롤업 읽기다(services/usage_rollup).

    ## 실패 시 의미

    카드별로 `null` 을 돌려준다. 프론트는 null 을 '—' 로 렌더해야 한다 — 0 으로 접으면
    "활성 키 0개" 처럼 **거짓 사실**을 표시하게 된다(page.tsx 의 기존 관례).
    """
    period, rng = _month(period)
    cache_key = _cache_key("kpi", period=period, client=client)

    async def compute():
        # ── 1) 비용/요청/사용자/토큰/웹 검색: /summary 와 같은 하이브리드 합계.
        scope = ur.RollupScope.of(rng, client=client)
        t = await reads.totals(session, scope)
        meta = await ur.fetch_meta(session, scope)
        settings = await ws_settings(session)
        # ── 2)·3) 예산 한도·사용액
        budget_used, total_limit = await _budget_parts(session, period)
        utilization = (
            float(budget_used / total_limit * 100) if total_limit > 0 else None
        )
        # ── 4) 활성 키 / 활성 모델
        active_keys = (
            await session.execute(
                select(func.count()).select_from(VirtualKey).where(
                    VirtualKey.status == KeyStatus.ACTIVE
                )
            )
        ).scalar()
        active_models = (
            await session.execute(
                select(func.count()).select_from(ModelAlias).where(
                    ModelAlias.status == ModelStatus.ACTIVE
                )
            )
        ).scalar()
        active_users = t.active_users
        return {
            "period": period,
            "total_requests": t.total_requests,
            "error_requests": t.error_requests,
            "cancelled_requests": t.cancelled_requests,
            "billable_requests": t.billable_requests,
            "total_tokens": t.total_tokens,
            # 모델 토큰 비용 — 청구 대상 전 상태(wave 3). Total (charged) 와 분해는 charged.
            "total_cost_usd": round(float(t.total_cost), 4),
            "charged": t.charged(),
            "active_users": active_users,
            "cost_per_user_usd": (
                round(float(t.total_cost / active_users), 4) if active_users else 0.0),
            "budget_used_usd": round(float(budget_used), 4),
            "budget_limit_usd": round(float(total_limit), 4),
            # 한도가 0 이면 비율이 정의되지 않는다 — 0% 로 접으면 "예산을 안 썼다" 는
            # 거짓 사실이 된다. null 로 두고 프론트가 '—' 로 렌더한다.
            "budget_utilization_pct": round(utilization, 2) if utilization is not None else None,
            "active_keys": active_keys or 0,
            "active_models": active_models or 0,
            # 웹 검색 — total_cost_usd(모델) 에는 없고 charged.total_usd 와 budget_*(wave 3 이후
            # 기록분)에 들어간다.
            "web_search": ws_from_totals(t.ws_queries, t.ws_cost, t.ws_untracked,
                                         settings).model_dump(mode="json"),
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


@router.get("/web-search")
async def dashboard_web_search(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM (리포팅 TZ). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """대시보드 'WEB SEARCH · AGENTCORE' 섹션 — 앱별 검색 수, 일별 검색 수, 비용, 일평균,
    요청당 검색 수. **모니터링 전용**(예산·한도와 무관).

    - 검색 수·비용은 status 무관(실패 요청의 검색도 과금 — services/web_search_cost).
    - ``queries_per_request`` = 검색 수 / (성공 + 취소 요청 수)(대시보드 요청 카드와 같은 모집단),
      소수 8자리(작은 값도 0 이 되지 않게).
    - ``avg_queries_per_day`` = 검색 수 / 경과일(리포팅 TZ; 당월은 오늘까지, 지난 달은 그 달 일수).
    - ``daily`` 는 검색이 있었던 날만 — 빈 날·미래 날 채우기는 UI(비용 추이와 같은 규칙).
    """
    period, rng = _month(period)
    cache_key = _cache_key("web-search", period=period, client=client)

    async def compute():
        scope = ur.RollupScope.of(rng, client=client)
        day_scope = ur.RollupScope.of(rng, client=client, by_day=True)
        t = await reads.totals(session, scope)
        settings = await ws_settings(session)
        ws = ws_from_totals(t.ws_queries, t.ws_cost, t.ws_untracked, settings)
        by_client_rows = await reads.ws_by_client(session, scope)
        daily_rows = await reads.ws_daily(session, day_scope)
        meta = ur.merge_meta(await ur.fetch_meta(session, scope),
                             await ur.fetch_meta(session, day_scope))

        price, _is_default, _row = await get_price(session)
        days = elapsed_days(period, reporting_today())
        # 응답 필드명은 하위 호환을 위해 successful_requests 그대로 두지만, 분모는
        # 대시보드 요청 카드와 같은 성공+취소다(cancelled 도 프로바이더 처리·과금 행).
        counted_requests = int(t.total_requests) + int(t.cancelled_requests)
        return {
            "period": period,
            "deployed": ws.deployed,
            # 현재 단가(부제 "<N> queries × $<price> per 1,000" 용). 행별 비용은 기록 시점 단가.
            "price_per_1k_queries": str(price),
            "queries": ws.queries,
            "cost_usd": ws.cost_usd,
            "untracked_queries": ws.untracked_queries,
            "successful_requests": counted_requests,
            "elapsed_days": days,
            "avg_queries_per_day": round(ws.queries / days, 2) if days > 0 else None,
            # 소수 2자리로 자르면 3/1,818 이 0.0 이 된다 — 표시 자릿수는 UI(fmtSmallRatio)가 정한다.
            "queries_per_request": (round(ws.queries / counted_requests, 8)
                                   if counted_requests > 0 else None),
            "by_client": [
                {
                    "client": r.client,
                    "queries": int(r.queries or 0),
                    "cost_usd": round(float(r.cost), 6) if r.cost is not None else None,
                }
                for r in by_client_rows
            ],
            "daily": [{"date": str(r.day), "queries": int(r.q or 0)} for r in daily_rows],
            **meta.as_dict(),
        }

    return await _cached(request, cache_key, rng, compute)


#: 월 목록 — usage_logs 의 가장 이른/늦은 요청 시각. 둘 다 requested_at 인덱스의 끝점 조회라
#: 테이블 크기와 무관하다(예전 DISTINCT to_char(...) 전체 스캔은 30M 행에서 18.6s / cold 22.7s).
_PERIOD_BOUNDS_SQL = text(
    "SELECT (SELECT min(requested_at) FROM usage.usage_logs) AS lo, "
    "(SELECT max(requested_at) FROM usage.usage_logs) AS hi"
)


@router.get("/periods")
async def dashboard_periods(
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """사용량 데이터가 있는 월(YYYY-MM) 목록 — 최신순.

    admin-ui 의 기간 선택기가 이걸로 옵션을 채우고, 기본값을
    "데이터 있는 가장 최근 월"(periods[0])로 잡아 현재 달력월이 비어도 빈 화면을 피한다.
    status 필터 안 함 — 에러만 있는 월도 노출.

    min(requested_at)~max(requested_at) 사이의 리포팅 tz 월 전부다. 그 사이에 사용량이 한 건도
    없는 달이 끼면(운영 중단) 그 달도 목록에 나온다 — 고르면 빈 화면일 뿐 숫자는 틀리지 않는다.
    """
    cache_key = _cache_key("periods")
    if (cached := await _cache_get(request, cache_key)) is not None:
        return cached

    from zoneinfo import ZoneInfo

    row = (await session.execute(_PERIOD_BOUNDS_SQL)).one()
    periods: list[str] = []
    if row.lo is not None and row.hi is not None:
        tz = ZoneInfo(reporting_timezone())
        lo, hi = row.lo.astimezone(tz), row.hi.astimezone(tz)
        y, m = hi.year, hi.month
        while (y, m) >= (lo.year, lo.month):
            periods.append(f"{y}-{m:02d}")
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    payload = {"periods": periods}
    # 월 목록은 느리게 변하는 차원이라 10분 캐시(R25-N10). 끝점 조회라 캐시가 없어도 ms 다.
    await _cache_set(request, cache_key, payload, ttl=600)
    return payload
