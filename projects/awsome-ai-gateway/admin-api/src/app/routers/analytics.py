# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.db import get_analytics_session
from app.core.usage_filters import current_kst_period

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/analytics", tags=["Analytics"])

def _scope_part(scope_ids) -> str:
    """캐시 키의 범위 조각 — **해석된** 팀 집합. None = 전사(ADMIN 의 scope='all')."""
    return "global" if scope_ids is None else "teams=" + ",".join(sorted(str(t) for t in scope_ids))


def _analytics_cache_key(
    *,
    period: str,
    group_by: str,
    client: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
    scope_ids=None,
) -> str:
    """/admin/analytics 응답 캐시 키.

    ⚠️ 격리 불변식: 키에 들어가는 범위는 요청 문자열(scope='all')이 아니라 **해석된 팀 집합**
    (_resolve_scope 결과)이다. 같은 `scope='all'` 이 ADMIN 에겐 전사(→ 'global'),
    TEAM_LEADER 에겐 본인이 리드하는 팀들(→ 'teams=a,b')로 갈리므로, 키가 다르고
    TEAM_LEADER 가 ADMIN 의 전사 응답을 받는 경로가 없다. 응답은 (기간·group_by·client·팀
    집합)만의 함수라 행위자 자체를 키에 넣을 필요는 없다 — 같은 팀을 리드하는 두 리더는 같은
    응답을 공유해도 된다(같은 데이터를 볼 권한).
    custom 날짜 구간도 키에 싣는다 — 빼면 월 질의와 구간 질의가 TTL 안에 서로의 응답을 돌려준다.
    """
    normalized_client = "all" if client in (None, "", "all") else client
    range_part = f"{start_date or ''}~{end_date or ''}"
    return (f"analytics:v2:{_scope_part(scope_ids)}:{period}:{range_part}:{group_by}:"
            f"{normalized_client}")


async def _cached(request: Request, key: str, rng, compute, *, keep_local: bool = False):
    from app.core import read_cache
    from app.services import usage_rollup as ur

    return await read_cache.cached(
        getattr(request.app.state, "redis", None), key, compute,
        ttl=read_cache.ttl_for(rng.end, ur.reporting_today()), keep_local=keep_local,
    )


@router.get("")
async def get_analytics(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    # ⚠️ Literal 로 좁혀 잘못된 값이 422 로 실패하게 한다. 예전 description 은
    #    'department' 를 광고했지만 analytics_service 에는 그 분기가 없어서, 요청하면
    #    조용히 by_model 응답이 돌아왔다(쓰레기 값과 바이트 단위로 동일 = 무증상 오답).
    #    이 Literal 집합은 admin-ui/src/types/enums.ts 의 GroupByType 과 일치한다.
    group_by: Literal["model", "team", "user"] = Query("model"),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_analytics_session),
):
    from app.services import usage_rollup as ur
    from app.services.analytics_service import AnalyticsService, _resolve_scope

    svc: AnalyticsService = request.app.state.analytics_service

    # 범위 해석(권한 검사 포함 — TEAM_LEADER 의 남의 팀 요청은 여기서 403)이 캐시보다 먼저다.
    # 캐시 키는 해석된 팀 집합을 담는다(_analytics_cache_key 주석의 격리 불변식).
    rng = ur.resolve_range(period, start_date, end_date)
    _, scope_ids = await _resolve_scope(user, scope, session)
    cache_key = _analytics_cache_key(
        period=period, group_by=group_by, client=client,
        start_date=start_date, end_date=end_date, scope_ids=scope_ids,
    )

    async def compute():
        return await svc.get_analytics(
            session,
            period=period,
            group_by=group_by,
            scope=scope,
            client=client,
            actor=user,
            start_date=start_date,
            end_date=end_date,
            scope_ids=scope_ids,
        )

    return await _cached(request, cache_key, rng, compute)


@router.get("/models")
async def get_model_cost_analytics(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM format"),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_analytics_session),
):
    """모델별 상세 비용 — /analytics 페이지의 상세 섹션.

    overview(/admin/analytics)와 같은 필터셋(월 또는 custom 구간·client·scope·
    TEAM_LEADER 팀 격리)을 받는다 — 예전엔 period 만 받아서 별도 페이지였을 때는
    괜찮았지만, 같은 화면에 붙은 이상 필터가 다르면 카드와 표의 숫자가 어긋난다.
    집계·격리 로직은 AnalyticsService.get_model_cost_detail 이 단일 소스다.
    """
    from app.services import usage_rollup as ur
    from app.services.analytics_service import AnalyticsService, _resolve_scope

    if not period:
        period = current_kst_period()  # KST 월(§59) — pod TZ(UTC) 아님

    svc: AnalyticsService = request.app.state.analytics_service
    rng = ur.resolve_range(period, start_date, end_date)
    _, scope_ids = await _resolve_scope(user, scope, session)  # 권한 검사가 캐시보다 먼저
    key = _analytics_cache_key(period=period, group_by="models-detail", client=client,
                               start_date=start_date, end_date=end_date, scope_ids=scope_ids)

    async def compute():
        return await svc.get_model_cost_detail(
            session,
            period=period,
            actor=user,
            scope=scope,
            client=client,
            start_date=start_date,
            end_date=end_date,
        )

    return await _cached(request, key, rng, compute)


@router.get("/export")
async def export_analytics(
    request: Request,
    format: Literal["csv", "json"] = Query("csv"),
    period: str = Query(description="YYYY-MM format"),
    group_by: Literal["model", "team", "user"] = Query("model"),
    scope: str = Query("all"),
    client: str | None = Query(default=None),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    view: Literal["user_model"] | None = Query(
        default=None, description="user_model = 사용자 × 모델 행(CSV 전용)"
    ),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_analytics_session),
):
    """CSV/JSON 내보내기 — **응답 헤더를 먼저** 보내고 본문을 스트리밍한다.

    ⚠️ 예전엔 전체 결과를 메모리에서 만든 뒤 한 번에 응답했다 — 큰 범위(6k 사용자 × 모델,
    366일)는 헤더가 나가기까지 수십 초가 걸려 admin-ui 프록시의 헤더 타임아웃(15 s,
    lib/utils/upstream.ts)에 504 로 끊겼다. 이제 검증(기간·스코프 403)만 먼저 끝내고
    StreamingResponse 로 헤더를 바로 보낸다. 쿼리는 본문 생성기 안에서 자기 분석 세션으로
    돌고(요청 세션은 스트리밍 전에 반납), 사용자×모델 행은 서버 측 커서로 묶음 단위로 낸다.
    본문 도중 실패하면 연결이 끊겨 다운로드가 실패로 보인다(잘린 파일을 성공으로 주지 않는다).
    """
    from app.core.db import AnalyticsSessionLocal
    from app.services import usage_rollup as ur
    from app.services.analytics_service import _resolve_scope

    if view == "user_model":
        from app.services import user_model_usage as umu

        if format != "csv":
            from app.core.exceptions import ValidationError

            raise ValidationError("view=user_model supports format=csv only")
        rng = umu.resolve_range(period, start_date, end_date)
        _, scope_ids = await _resolve_scope(user, scope, session)
        await session.close()  # 검증용 연결 반납 — 본문은 자기 세션으로 돈다

        async def user_model_body():
            async with AnalyticsSessionLocal() as s:
                try:
                    async for chunk in umu.export_csv_chunks(s, rng, scope_ids, client):
                        yield chunk.encode()
                finally:
                    await s.rollback()

        stamp = rng.label.replace("~", "_")
        return StreamingResponse(
            user_model_body(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="analytics_user_model_{stamp}.csv"',
                "Cache-Control": "no-store",
            },
        )

    # 기간 검증(400/422)과 스코프 해석(403)은 헤더 전에 — 스트리밍 뒤엔 상태 코드를 못 바꾼다.
    ur.resolve_range(period, start_date, end_date)
    _, scope_ids = await _resolve_scope(user, scope, session)
    await session.close()
    svc: AnalyticsService = request.app.state.analytics_service

    async def body():
        # 집계 응답만 세션에서 읽고 세션은 바로 반납 — 본문 직렬화·전송 동안 분석 풀
        # 연결과 열린 트랜잭션을 붙잡지 않는다(느린 클라이언트 대비).
        async with AnalyticsSessionLocal() as s:
            try:
                response = await svc.get_analytics(
                    s,
                    period=period,
                    group_by=group_by,
                    scope=scope,
                    client=client,
                    actor=user,
                    start_date=start_date,
                    end_date=end_date,
                    scope_ids=scope_ids,
                )
            finally:
                await s.rollback()
        if format == "csv":
            for chunk in svc._iter_csv(response, group_by):
                yield chunk.encode()
        else:
            text = response.model_dump_json(indent=2)
            for i in range(0, len(text), 65536):
                yield text[i : i + 65536].encode()

    filename = (
        f"analytics_{start_date}_{end_date}.{format}"
        if start_date and end_date
        else f"analytics_{period}.{format}"
    )
    return StreamingResponse(
        body(),
        media_type="text/csv" if format == "csv" else "application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/usage/by-user")
async def get_usage_by_user(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_analytics_session),
):
    """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
    from app.services import usage_rollup as ur
    from app.services.analytics_service import AnalyticsService, _validate_period_date

    _validate_period_date(period, date)
    svc: AnalyticsService = request.app.state.analytics_service
    rng = ur.month_range(period)

    async def compute():
        return await svc.get_usage_by_user(session, period=period, date=date)

    return await _cached(request, f"analytics:v2:usage-by-user:{period}:{date}", rng, compute)


# ── 사용자 × 모델 토큰 사용량 (By User 모드) ───────────────────────────────────

def _user_models_cache_key(*, scope_ids, start: str, end: str, client: str | None) -> str:
    """키 = 범위 + 앱 필터 + **해석된** 팀 집합(_analytics_cache_key 와 같은 격리 불변식)."""
    normalized_client = "all" if client in (None, "", "all") else client
    return f"analytics:user-models:v2:{_scope_part(scope_ids)}:{start}~{end}:{normalized_client}"


@router.get("/user-models")
async def get_user_models(
    request: Request,
    period: str | None = Query(default=None, description="YYYY-MM (custom 구간이 없을 때)"),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 시작(end_date 필요)"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD — custom 구간 끝(포함)"),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str | None = Query(default=None, description="claude-code|cowork|codex|other|all"),
    sort: str = Query("total", max_length=134,
                      description="total|input|output|cache|cost|name|other|model:<alias>"),
    dir: Literal["asc", "desc"] = Query("desc"),
    page: int = Query(1, ge=1, le=100_000),
    # 상한 500 — /users 팀 상세가 팀 전원(≤500)을 한 요청으로 받는다(예전 50 × 순차 페이지).
    page_size: int = Query(10, ge=1, le=500),
    q: str | None = Query(default=None, max_length=100, description="이름·이메일·팀 부분 일치"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_analytics_session),
):
    """사용자 × 모델 표 — 상위 5 모델(총 토큰) 열 + Other + 행 합계.

    숫자는 Total Tokens KPI 와 같은 정의(SUCCESS·리포팅 tz 일자·앱 필터·팀 스코프)다.
    기간 검증은 /admin/analytics 와 같다(366일 초과 422, 한쪽만 400).

    스코프 **전체** 결과(6,000 사용자 ≈ 3 MB)를 캐시하고 정렬·페이지·검색은 거기서 파생한다.
    파생(정렬·검색·페이지)과 JSON (역)직렬화는 이벤트 루프 밖(스레드)에서 하고, 파싱된 결과는
    프로세스 로컬 LRU 에 TTL 동안 둔다 — 페이지를 넘길 때마다 3 MB 를 다시 파싱하지 않는다.
    """
    from app.services import user_model_usage as umu
    from app.services.analytics_service import _resolve_scope

    if not period and not (start_date or end_date):
        period = current_kst_period()
    rng = umu.resolve_range(period, start_date, end_date)
    if not umu.SORT_RE.fullmatch(sort):
        from app.core.exceptions import ValidationError

        raise ValidationError(
            "sort must be total|input|output|cache|cost|name|other|model:<alias>"
        )
    _, scope_ids = await _resolve_scope(user, scope, session)
    cache_key = _user_models_cache_key(
        scope_ids=scope_ids, start=rng.start.isoformat(), end=rng.end.isoformat(), client=client
    )

    async def compute():
        return await umu.fetch_scope_payload(session, rng, scope_ids, client)

    payload = await _cached(request, cache_key, rng, compute, keep_local=True)
    return await asyncio.to_thread(
        umu.page_from_payload, payload, sort=sort, direction=dir, page=page,
        page_size=page_size, q=q,
    )


@router.get("/user-models/{user_id}")
async def get_user_model_detail(
    user_id: uuid.UUID,
    period: str | None = Query(default=None, description="YYYY-MM (custom 구간이 없을 때)"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    scope: str = Query("all", description="all | team:{uuid}"),
    client: str | None = Query(default=None),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_analytics_session),
):
    """한 사용자의 전 모델 상세(요청 수·4 토큰 버킷·합계·비용·평균 지연). 캐시하지 않는다."""
    from app.services import user_model_usage as umu
    from app.services.analytics_service import _resolve_scope

    if not period and not (start_date or end_date):
        period = current_kst_period()
    rng = umu.resolve_range(period, start_date, end_date)
    _, scope_ids = await _resolve_scope(user, scope, session)
    return await umu.get_user_detail(session, user_id, rng, scope_ids, client)

