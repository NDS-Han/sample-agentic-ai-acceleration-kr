# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Gateway body-log 뷰어 — S3 에 적재된 요청/응답 본문을 request_id 로 조회한다.

두 겹의 게이트: ``body_logging_enabled`` 런타임 토글이 꺼져 있으면 403(수집
스위치와 열람을 연동하는 운영 결정 — 과거 수집분도 끄면 안 보인다), 통과해도
열람 행위 자체가 ``VIEW_BODY_LOG`` 감사 행으로 남는다. 응답은 프롬프트 원문을
담으므로 ``Cache-Control: no-store`` 를 강제한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import (
    CurrentUser,
    forbid_service_token_reader,
    require_admin,
)
from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.auth import User
from app.models.usage import UsageLog, UsageStatus
from app.services.body_log_store import (
    BodyLogBatchResult,
    BodyLogLookupError,
    BodyLogNotConfigured,
    BodyLogStore,
    BodyLogTarget,
)

logger = structlog.get_logger()

# svc 토큰 차단 — 원문 본문(프롬프트/응답)은 사람 admin 만 읽는다.
# 유출된 장수명 토큰이 사용자 대화를 긁어가는 경로를 닫는다(audit 라우터와 동일).
router = APIRouter(
    prefix="/admin/body-logs",
    tags=["Body Logs"],
    dependencies=[Depends(forbid_service_token_reader)],
)

#: request_id 는 S3 키 세그먼트로 들어간다 — ``/`` 와 ``..`` 를 허용하면
#: prefix 탈출로 다른 파티션을 읽을 수 있으므로 문자셋을 좁힌다.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,128}$")

#: session_id 는 클라이언트 제어 문자열 — ``sid`` 필터로 들어올 때 같은
#: 화이트리스트를 적용한다(gateway 추출기와 동일 문자셋).
_SESSION_ID_RE = re.compile(r"^[0-9A-Za-z._:-]{1,128}$")

#: 수집 지연 안내 임계 — Firehose 버퍼(60s)+전파 여유로, 이 안의 404는
#: "아직 안 온 것"이지 "없는 것"이 아니다.
_COLLECTING_WINDOW_S = 180

#: 세션 그룹핑 — 같은 (user, client) 의 연속 호출이 이 간격을 넘으면 새 세션.
#: 사용자가 읽고 생각하는 공백까지 끊지 않도록 넉넉하게 잡는다(휴리스틱 —
#: 대화 식별자를 본문에서만 알 수 있으므로 시간 인접성으로 묶는다).
_SESSION_GAP_MINUTES = 30
#: 세션 목록이 한 번에 훑는 usage 행 상한 — 인덱스 스캔이라 저렴하지만
#: 그룹핑은 메모리에서 하므로 상한을 둔다. 넘으면 truncated 로 표시한다.
_SESSION_SCAN_LIMIT = 2000
# 세션당 반환 호출 상한 — 긴 에이전트 세션(수백~수천 호출)도 응답을 바운딩한다.
_SESSION_CALLS_MAX = 500
#: 배치 조회 상한 — 응답 크기·감사 행 크기·S3 스캔 시간을 묶는 상한.
_BATCH_MAX_IDS = 50

_NO_STORE = {"Cache-Control": "no-store"}


class _BatchRequest(BaseModel):
    request_ids: list[str] = Field(default_factory=list, max_length=_BATCH_MAX_IDS)


async def _require_body_logging(request: Request, session: AsyncSession) -> None:
    """런타임 토글 게이트 — OFF 면 열람·목록 모두 403.

    403 초기 거절은 감사하지 않는다 — "열람 시도"가 성립하기 전의 게이트
    거절로, invocation-log 엔드포인트와 같은 계약이다.
    """
    svc = request.app.state.system_settings_service
    if not await svc.get_body_logging_enabled(session):
        raise HTTPException(
            403, "본문 로깅이 비활성 상태입니다 — Monitoring 토글을 먼저 켜세요"
        )


def _status_str(status) -> str | None:
    """UsageStatus 같은 str-enum 은 ``str()`` 이 ``"UsageStatus.SUCCESS"`` 를
    낸다 — wire 값은 ``.value`` 로 정규화한다(순수 문자열 행도 통과)."""
    if status is None:
        return None
    return str(getattr(status, "value", status))


def _usage_lookup_stmt():
    return select(
        UsageLog.request_id,
        UsageLog.user_id,
        User.email.label("user_email"),
        UsageLog.team_id,
        UsageLog.provider,
        UsageLog.client,
        UsageLog.requested_at,
        UsageLog.latency_ms,
        UsageLog.model_alias,
        UsageLog.status,
        UsageLog.cost_usd,
        UsageLog.is_streaming,
        UsageLog.input_tokens,
        UsageLog.output_tokens,
        UsageLog.cache_read_tokens,
        UsageLog.reasoning_tokens,
        UsageLog.ttft_ms,
    ).outerjoin(User, User.id == UsageLog.user_id)


def _session_agg_stmt(
    cutoff: datetime | None,
    limit: int,
    *,
    before: datetime | None = None,
    user_ids: list | None = None,
    client: str | None = None,
    has_error: bool = False,
    sid: str | None = None,
):
    """session_id 명시 세션 집계 — ⚠️ user_id 는 uuid 라 max()/min() 집계 불가.

    Postgres 는 uuid 에 순서 연산이 없어 ``max(uuid)`` 가 UndefinedFunctionError
    다 — user_id/client/email 은 집계 대신 GROUP BY 키에 넣는다(세션은 단일
    소유자라 결과 행은 같고, 만에 하나 키가 갈라지면 방어적으로 두 세션으로
    보이는 게 합쳐지는 것보다 낫다). 집계가 필요한 건 시각/건수/모델/비용뿐.

    ``before``(페이지네이션 커서)·``has_error`` 는 그룹 수준 조건이라 HAVING —
    WHERE 에 넣으면 세션 경계가 아니라 개별 행이 잘린다.
    """
    stmt = (
        select(
            UsageLog.session_id,
            func.min(UsageLog.requested_at).label("started_at"),
            func.max(UsageLog.requested_at).label("ended_at"),
            func.count().label("call_count"),
            func.count()
            .filter(UsageLog.status != UsageStatus.SUCCESS)
            .label("error_count"),
            func.sum(UsageLog.cost_usd).label("total_cost"),
            UsageLog.user_id,
            User.email.label("user_email"),
            UsageLog.client,
            func.array_agg(UsageLog.model_alias.distinct()).label("models"),
        )
        .select_from(UsageLog)
        .outerjoin(User, User.id == UsageLog.user_id)
        .where(UsageLog.session_id.is_not(None))
        .group_by(
            UsageLog.session_id,
            UsageLog.user_id,
            User.email,
            UsageLog.client,
        )
        .order_by(func.max(UsageLog.requested_at).desc())
        .limit(limit)
    )
    if cutoff is not None:
        stmt = stmt.where(UsageLog.requested_at >= cutoff)
    if sid is not None:
        stmt = stmt.where(UsageLog.session_id == sid)
    if user_ids is not None:
        stmt = stmt.where(UsageLog.user_id.in_(user_ids))
    if client is not None:
        stmt = stmt.where(UsageLog.client == client)
    if before is not None:
        stmt = stmt.having(func.max(UsageLog.requested_at) < before)
    if has_error:
        stmt = stmt.having(
            func.count().filter(UsageLog.status != UsageStatus.SUCCESS) > 0
        )
    return stmt


# ── 세션 목록 (메타데이터만 — 본문 아님, 감사 불요) ────────────────────────────
# `/{request_id}` 보다 먼저 선언해야 경로 파라미터에 삼켜지지 않는다.
@router.get("/sessions")
async def list_body_log_sessions(
    request: Request,
    days: int = Query(default=1, ge=0, le=365),
    gap_minutes: int = Query(default=_SESSION_GAP_MINUTES, ge=1, le=1440),
    limit: int = Query(default=20, ge=1, le=50),
    before: datetime | None = Query(default=None),
    user: str | None = Query(default=None, max_length=200),
    client: str | None = Query(default=None, max_length=128),
    has_error: bool = Query(default=False),
    sid: str | None = Query(default=None),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """세션 목록 — 명시적 session_id 그룹핑 + NULL 행은 시간 휴리스틱 폴백.

    게이트웨이가 원본 본문 metadata 의 session_<id> 를 usage_logs.session_id 에
    기록한다(migration 0054). 있는 행은 그 키로 정확히 묶고 — 같은 유저의 동시
    대화도 갈라진다 — NULL 행(구 레코드·비 Anthropic 클라이언트)만 기존
    (user, client, gap) 휴리스틱으로 묶는다. 본문은 반환하지 않으므로 감사하지
    않는다(monitoring events 와 같은 민감도 계급).

    필터는 그룹핑 전에 WHERE 로 건다 — 1000명 규모에서 "어제 A씨 세션"을 찾는
    경로가 이 엔드포인트뿐이다. ``before`` 는 커서(직전 페이지 마지막 세션의
    ended_at), ``sid`` 는 딥링크용 직행 조회(휴리스틱 스캔 생략).
    """
    await _require_body_logging(request, session)

    if sid is not None and not _SESSION_ID_RE.fullmatch(sid):
        raise HTTPException(400, "sid 형식이 올바르지 않습니다")
    if before is not None and before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)

    # user 부분일치 → user_id 목록으로 해석 (email 또는 uuid 텍스트).
    # 와일드카드 이스케이프 후 ilike — NULL user_id(agent 토큰)는 자연 제외.
    user_ids = None
    if user:
        like = (
            "%"
            + user.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        user_ids = list(
            (
                await session.execute(
                    select(User.id)
                    .where(
                        (User.email.ilike(like, escape="\\"))
                        | (cast(User.id, String).ilike(like, escape="\\"))
                    )
                    .limit(100)
                )
            ).scalars()
        )

    cutoff = None
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # ── 1) 명시적 세션 — session_id GROUP BY + 호출은 IN 쿼리 1회 ─────────
    # limit+1 로 가져와 has_more 를 판별한다.
    exact_rows = list(
        (
            await session.execute(
                _session_agg_stmt(
                    cutoff,
                    limit + 1,
                    before=before,
                    user_ids=user_ids,
                    client=client,
                    has_error=has_error,
                    sid=sid,
                )
            )
        ).all()
    )
    exact_overflow = len(exact_rows) > limit
    exact_rows = exact_rows[:limit]

    calls_by_sid: dict[tuple, list] = {}
    calls_truncated = False
    sids = [r.session_id for r in exact_rows]
    if sids:
        cstmt = (
            select(
                UsageLog.session_id,
                UsageLog.user_id,
                UsageLog.client,
                UsageLog.request_id,
                UsageLog.requested_at,
                UsageLog.model_alias,
                UsageLog.status,
                UsageLog.latency_ms,
                UsageLog.cost_usd,
                UsageLog.input_tokens,
                UsageLog.output_tokens,
            )
            .where(UsageLog.session_id.in_(sids))
            .order_by(UsageLog.requested_at.asc())
        )
        if cutoff is not None:
            cstmt = cstmt.where(UsageLog.requested_at >= cutoff)
        # session_id 는 클라이언트 제어 문자열 — 다른 user/client 가 같은 값을
        # 쓰면 충돌하므로 호출 매칭도 (session_id, user_id, client) 복합키로.
        for c in (await session.execute(cstmt)).all():
            skey = (c.session_id, str(c.user_id) if c.user_id else None, c.client)
            bucket = calls_by_sid.setdefault(skey, [])
            if len(bucket) >= _SESSION_CALLS_MAX:
                calls_truncated = True
                continue
            bucket.append(
                {
                    "request_id": c.request_id,
                    "requested_at": c.requested_at.isoformat(),
                    "model_alias": c.model_alias,
                    "status": _status_str(c.status),
                    "latency_ms": c.latency_ms,
                    "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                }
            )

    sessions = [
        {
            # session_id 만으로는 다른 유저/클라이언트와 충돌할 수 있어 복합키.
            "key": f"sid|{r.session_id}|{r.user_id or 'none'}|{r.client or 'other'}",
            "session_id": r.session_id,
            "user_id": str(r.user_id) if r.user_id else None,
            "user_email": r.user_email,
            "client": r.client,
            "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat(),
            "call_count": r.call_count,
            "error_count": r.error_count,
            "total_cost_usd": float(r.total_cost) if r.total_cost is not None else None,
            "models": sorted(m for m in (r.models or []) if m),
            "calls": calls_by_sid.get(
                (
                    r.session_id,
                    str(r.user_id) if r.user_id else None,
                    r.client,
                ),
                [],
            ),
        }
        for r in exact_rows
    ]

    # sid 직행은 딥링크 조회다 — 휴리스틱 목록 스캔을 생략하고 바로 반환한다.
    if sid is not None:
        return JSONResponse(
            {
                "sessions": sessions,
                "scanned_rows": 0,
                "explicit_sessions": len(exact_rows),
                "truncated": False,
                "calls_truncated": calls_truncated,
                "has_more": False,
                "next_before": None,
            },
            headers=_NO_STORE,
        )

    # ── 2) NULL session_id 행 — 구 레코드·미추출 클라이언트 폴백 (기존 휴리스틱) ──
    stmt = (
        _usage_lookup_stmt()
        .where(UsageLog.session_id.is_(None))
        .order_by(UsageLog.requested_at.desc())
        .limit(_SESSION_SCAN_LIMIT)
    )
    if cutoff is not None:
        stmt = stmt.where(UsageLog.requested_at >= cutoff)
    if before is not None:
        stmt = stmt.where(UsageLog.requested_at < before)
    if user_ids is not None:
        stmt = stmt.where(UsageLog.user_id.in_(user_ids))
    if client is not None:
        stmt = stmt.where(UsageLog.client == client)
    rows = (await session.execute(stmt)).all()
    truncated = len(rows) >= _SESSION_SCAN_LIMIT

    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault((r.user_id, r.client), []).append(r)

    groups: list[list] = []
    for items in by_key.values():
        items.sort(key=lambda r: r.requested_at)
        cur: list = []
        for r in items:
            if cur and (
                r.requested_at - cur[-1].requested_at
            ) > timedelta(minutes=gap_minutes):
                groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            groups.append(cur)

    groups.sort(key=lambda g: g[-1].requested_at, reverse=True)
    for g in groups:
        first, last = g[0], g[-1]
        errors = sum(
            1
            for r in g
            if r.status is not None
            and (_status_str(r.status) or "").upper() != "SUCCESS"
        )
        if has_error and errors == 0:
            continue
        sessions.append(
            {
                "key": f"{first.user_id or 'none'}|{first.client or 'other'}|{last.requested_at.isoformat()}",
                "session_id": None,
                "user_id": str(first.user_id) if first.user_id else None,
                "user_email": first.user_email,
                "client": first.client,
                "started_at": first.requested_at.isoformat(),
                "ended_at": last.requested_at.isoformat(),
                "call_count": len(g),
                "error_count": errors,
                "total_cost_usd": sum(float(r.cost_usd or 0) for r in g),
                "models": sorted({r.model_alias for r in g if r.model_alias}),
                "calls": [
                    {
                        "request_id": r.request_id,
                        "requested_at": r.requested_at.isoformat(),
                        "model_alias": r.model_alias,
                        "status": _status_str(r.status),
                        "latency_ms": r.latency_ms,
                        "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                    }
                    for r in g
                ],
            }
        )

    sessions.sort(key=lambda x: x["ended_at"], reverse=True)
    returned = sessions[:limit]
    has_more = exact_overflow or truncated or len(sessions) > limit
    return JSONResponse(
        {
            "sessions": returned,
            # 단위가 섞이지 않게 둘로 나눈다 — 스캔한 NULL 행 수와 집계된
            # 명시 세션 수는 다른 것이다.
            "scanned_rows": len(rows),
            "explicit_sessions": len(exact_rows),
            "truncated": truncated,
            "calls_truncated": calls_truncated,
            "has_more": has_more,
            "next_before": returned[-1]["ended_at"] if has_more and returned else None,
        },
        headers=_NO_STORE,
    )


# ── 세션 trace 배치 조회 ──────────────────────────────────────────────────────
@router.post("/batch")
async def get_body_logs_batch(
    request: Request,
    body: _BatchRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """여러 request_id 의 본문을 한 번의 S3 스캔으로 가져온다.

    단건 엔드포인트와 같은 감사 계약 — 호출 한 번 = VIEW_BODY_LOG 한 행에
    요청/발견/미발견 id 목록을 남긴다.
    """
    await _require_body_logging(request, session)

    settings = get_settings()
    if not settings.BODY_LOG_S3_BUCKET:
        raise HTTPException(503, "BODY_LOG_S3_BUCKET 미설정 — body_logging 인프라 없음")

    # 문자셋 검증 — 개별 id 와 같은 규칙. 무효 id 는 조회 대상에서만 제외한다.
    valid_ids = [
        rid
        for rid in dict.fromkeys(body.request_ids)
        if _REQUEST_ID_RE.fullmatch(rid) and ".." not in rid
    ]

    targets: list[BodyLogTarget] = []
    meta: dict[str, dict] = {}
    no_row: list[str] = []
    if valid_ids:
        rows = (
            await session.execute(
                _usage_lookup_stmt().where(UsageLog.request_id.in_(valid_ids))
            )
        ).all()
        by_id = {r.request_id: r for r in rows}
        for rid in valid_ids:
            r = by_id.get(rid)
            if r is None:
                no_row.append(rid)
                continue
            targets.append(
                BodyLogTarget(
                    request_id=rid,
                    provider=r.provider,
                    client=r.client,
                    requested_at=r.requested_at,
                    latency_ms=r.latency_ms,
                )
            )
            meta[rid] = {
                "model_alias": r.model_alias,
                "user_email": r.user_email,
                "requested_at": r.requested_at.isoformat(),
                "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
                "latency_ms": r.latency_ms,
                "ttft_ms": r.ttft_ms,
                "client": r.client,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cache_read_tokens": r.cache_read_tokens,
                "reasoning_tokens": r.reasoning_tokens,
            }

    result = BodyLogBatchResult()
    try:
        if targets:
            store = BodyLogStore(settings.BODY_LOG_S3_BUCKET)
            result = await store.fetch_many(targets)
    except BodyLogNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except BodyLogLookupError as exc:
        await audit_logger.log(
            session,
            queue_only=True,
            actor_user_id=admin.user_id,
            actor_role=admin.role.value,
            service_token_id=getattr(admin, "service_token_id", None),
            action="VIEW_BODY_LOG",
            resource_type="GatewayBodyLog",
            resource_id=f"batch:{len(valid_ids)}",
            changes={"request_ids": valid_ids[:60], "error": str(exc)[:200]},
            result="FAILURE",
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=getattr(request.state, "request_id", "")
            or request.headers.get("x-request-id", ""),
        )
        raise HTTPException(502, f"본문 저장소 조회 실패: {exc}") from exc

    found_ids = sorted(result.records) + sorted(result.oversized)
    missing = sorted(set(result.missing) | set(no_row))
    await audit_logger.log(
        session,
        queue_only=not found_ids,
        actor_user_id=admin.user_id,
        actor_role=admin.role.value,
        service_token_id=getattr(admin, "service_token_id", None),
        action="VIEW_BODY_LOG",
        resource_type="GatewayBodyLog",
        resource_id=f"batch:{len(valid_ids)}",
        changes={
            "request_ids": valid_ids[:60],
            "found": found_ids[:60],
            "missing": missing[:60],
        },
        result="SUCCESS" if found_ids else "FAILURE",
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=getattr(request.state, "request_id", "")
        or request.headers.get("x-request-id", ""),
    )

    return JSONResponse(
        {
            "records": {
                rid: {
                    "found": True,
                    "source": result.sources.get(rid),
                    "key": result.keys.get(rid),
                    "duplicates": result.duplicates.get(rid, 0),
                    "record": result.records[rid],
                    **(meta.get(rid) or {}),
                }
                for rid in sorted(result.records)
            },
            "oversized": sorted(result.oversized),
            "missing": missing,
            "incomplete": result.incomplete,
        },
        headers=_NO_STORE,
    )


@router.get("/{request_id}")
async def get_body_log(
    request: Request,
    request_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    await _require_body_logging(request, session)

    settings = get_settings()
    if not settings.BODY_LOG_S3_BUCKET:
        raise HTTPException(503, "BODY_LOG_S3_BUCKET 미설정 — body_logging 인프라 없음")

    if not _REQUEST_ID_RE.fullmatch(request_id) or ".." in request_id:
        raise HTTPException(400, "request_id 형식이 올바르지 않습니다")

    # user_email 은 usage_logs 에 없고 auth.users 조인이 필요하다 — user_id 가
    # NULL(agent/service 토큰)일 수 있어 outer join.
    row = (
        await session.execute(
            _usage_lookup_stmt().where(UsageLog.request_id == request_id)
        )
    ).first()

    async def _audit(found: bool | None, error: str | None = None) -> None:
        # 성공(found=True)은 세션 바인딩, 실패/오류는 queue_only —
        # ``audit_reconcile._audit_body_view`` 와 같은 계약: 열람 시도는
        # 무조건 기록되고, 본문 주인(user_id/team_id)도 남긴다.
        await audit_logger.log(
            session,
            queue_only=found is not True,
            actor_user_id=admin.user_id,
            actor_role=admin.role.value,
            service_token_id=getattr(admin, "service_token_id", None),
            action="VIEW_BODY_LOG",
            resource_type="GatewayBodyLog",
            resource_id=request_id[:256],
            changes={
                "subject_user_id": str(row.user_id) if row else None,
                "subject_team_id": str(row.team_id) if row else None,
                "found": found,
                **({"error": error[:200]} if error else {}),
            },
            result="SUCCESS" if found else "FAILURE",
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=getattr(request.state, "request_id", "")
            or request.headers.get("x-request-id", ""),
        )

    if row is None:
        await _audit(found=False)
        # usage 행은 cost-recorder-worker 가 비동기로 쓴다 — 방금 보낸 요청은
        # 아직 행이 없는 게 정상이고, 그때는 파티션 정보를 유도할 수 없어 S3 를
        # 열지도 못한다. 오래된 요청인데 행이 없으면 비용 기록 실패다.
        raise HTTPException(
            404,
            "해당 request_id 의 사용 기록이 아직 없습니다 — 방금 보낸 요청이면 "
            "비용 기록이 도착하지 않은 것일 수 있으니 수 분 후 다시 시도하세요. "
            "오래된 요청이면 비용 기록 실패로 이 화면에서는 찾을 수 없습니다",
        )

    try:
        store = BodyLogStore(settings.BODY_LOG_S3_BUCKET)
        result = await store.fetch(
            request_id,
            provider=row.provider,
            client=row.client,
            requested_at=row.requested_at,
            latency_ms=row.latency_ms,
        )
    except BodyLogNotConfigured as exc:
        await _audit(found=None, error=str(exc))
        raise HTTPException(503, str(exc)) from exc
    except BodyLogLookupError as exc:
        await _audit(found=None, error=str(exc))
        raise HTTPException(502, f"본문 저장소 조회 실패: {exc}") from exc
    except Exception as exc:
        # 안전망 — 서비스가 못 잡은 예외(BotoCore 계열 등)도 "열람 시도는
        # 무조건 감사" 계약 아래로 들어와야 한다.
        await _audit(found=None, error=f"{type(exc).__name__}: {exc}"[:200])
        raise

    # oversized 도 레코드는 존재하는 것으로 확인된 상태 — 미발견(False)과 구분.
    await _audit(found=result.record is not None or result.oversized)

    if result.record is None and not result.oversized:
        age_s = (
            datetime.now(timezone.utc) - row.requested_at.astimezone(timezone.utc)
        ).total_seconds()
        if result.incomplete:
            # 스캔 상한은 결정적 — 같은 조건으로 재시도해도 결과가 같다.
            # "없다"가 아니라 "못 찾았다"이고, 대안 경로를 안내한다.
            hint = "저장소 스캔이 상한에 걸려 전부 확인하지 못했습니다 — 본문이 있을 수 있습니다. 재시도해도 같으면 S3 콘솔에서 직접 확인하세요."
        else:
            hint = (
                "수집 대기 중일 수 있습니다 — Firehose 버퍼링으로 수 분 후 도착합니다. 잠시 후 다시 시도하세요."
                if age_s < _COLLECTING_WINDOW_S
                else "이 요청의 본문은 수집되지 않았습니다 — 요청 시점에 본문 로깅이 꺼져 있었거나, 보존 기간이 지났거나, 적재에 실패했을 수 있습니다."
            )
        raise HTTPException(404, hint)

    return JSONResponse(
        {
            "found": True,
            "request_id": request_id,
            "source": result.source,
            "key": result.key,
            "duplicates": result.duplicates,
            "oversized": result.oversized,
            "incomplete": result.incomplete,
            "model_alias": row.model_alias,
            "user_email": row.user_email,
            "requested_at": row.requested_at.isoformat(),
            "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
            "latency_ms": row.latency_ms,
            "ttft_ms": row.ttft_ms,
            "client": row.client,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "reasoning_tokens": row.reasoning_tokens,
            "record": result.record,
        },
        headers={"Cache-Control": "no-store"},
    )
