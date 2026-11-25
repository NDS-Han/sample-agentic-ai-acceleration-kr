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
from app.core.db import get_analytics_session, get_db_session
from app.models.auth import User
from app.models.usage import UsageLog, UsageStatus
from app.services.web_search_cost import web_search_deployed
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
#: 명시 세션 목록의 시간 창 걸음 — 창 전체 GROUP BY(7일 ≈ 수백만 행) 대신 최신 시각부터
#: 작은 구간을 차례로 집계하고, 필요한 세션 수가 모이면 멈춘다. 첫 구간 15분, 이후 밀도에
#: 맞춰 최대 4배씩(최대 7일) 넓히고, 빈 구간이면 그 아래 가장 최근 행으로 바로 뛴다.
#: 구간들이 읽은 행 합이 상한을 넘으면 그 구간 하한을 커서로 돌려준다(has_more) — 한 요청이
#: 365일 창 전체를 훑는 일이 없다.
_SESSION_SLICE_START = timedelta(minutes=15)
_SESSION_SLICE_MAX = timedelta(days=7)
_SESSION_SLICE_ROW_BUDGET = 300_000
#: 다음 구간의 목표 행 수 — 앞 구간의 밀도(행/시간)로 걸음 폭을 정한다.
_SESSION_SLICE_TARGET_ROWS = 50_000
#: has_error 필터는 세션 전체의 오류 수로 판정하므로 후보를 묶음으로 집계해 거른다.
_SESSION_AGG_BATCH = 200
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


def _web_search_fields(row) -> dict:
    """요청 1건의 웹 검색 — 모델 비용(cost_usd)과 별개, 예산 제외.
    web_search_cost_usd=None 은 "미추적"(비용 추적 이전 행)이지 $0 이 아니다."""
    cost = row.web_search_cost_usd
    return {
        "web_search_count": int(row.web_search_count or 0),
        "web_search_cost_usd": float(cost) if cost is not None else None,
        # UI 표시 규칙(배포됐거나 기록 비용 > 0 일 때만 웹 검색 비용 표시)의 입력.
        "web_search_deployed": web_search_deployed(),
    }


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
        UsageLog.cache_creation_tokens,
        UsageLog.reasoning_tokens,
        UsageLog.ttft_ms,
        UsageLog.web_search_count,
        UsageLog.web_search_cost_usd,
    ).outerjoin(User, User.id == UsageLog.user_id)


def _session_agg_stmt(
    cutoff: datetime | None,
    limit: int | None,
    *,
    before: datetime | None = None,
    user_ids: list | None = None,
    client: str | None = None,
    has_error: bool = False,
    sid: str | None = None,
    null_client: bool = False,
    sids: list[str] | None = None,
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
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if cutoff is not None:
        stmt = stmt.where(UsageLog.requested_at >= cutoff)
    if sid is not None:
        stmt = stmt.where(UsageLog.session_id == sid)
    if sids is not None:
        stmt = stmt.where(UsageLog.session_id.in_(sids))
    if user_ids is not None:
        stmt = stmt.where(UsageLog.user_id.in_(user_ids))
    if null_client:
        stmt = stmt.where(UsageLog.client.is_(None))
    elif client is not None:
        stmt = stmt.where(UsageLog.client == client)
    if before is not None:
        # <= 로 타이(같은 ended_at)를 포함해 가져오고, 이미 반환된 key 는
        # 호출부에서 배제한다 — < 만이면 동시 종료 세션이 영영 빠진다.
        stmt = stmt.having(func.max(UsageLog.requested_at) <= before)
    if has_error:
        stmt = stmt.having(
            func.count().filter(UsageLog.status != UsageStatus.SUCCESS) > 0
        )
    return stmt


def _sess_key(session_id, user_id, client) -> tuple:
    return (session_id, str(user_id) if user_id else None, client)


def _wire_key(session_id, user_id, client) -> str:
    return f"sid|{session_id}|{user_id or 'none'}|{client or 'other'}"


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _row_filters(stmt, *, user_ids, client, null_client):
    if user_ids is not None:
        stmt = stmt.where(UsageLog.user_id.in_(user_ids))
    if null_client:
        stmt = stmt.where(UsageLog.client.is_(None))
    elif client is not None:
        stmt = stmt.where(UsageLog.client == client)
    return stmt


async def _explicit_sessions(
    session: AsyncSession,
    *,
    cutoff: datetime | None,
    before: datetime | None,
    exclude_keys: set[str],
    need: int,
    user_ids: list | None,
    client: str | None,
    null_client: bool,
    has_error: bool,
) -> tuple[list, datetime | None]:
    """명시 세션 목록 — ``_session_agg_stmt``(창 전체 GROUP BY … ORDER BY max DESC LIMIT)와
    같은 결과를 시간 창을 최신부터 걸어 내려가며 만든다.

    구간 ``(lo, hi]`` 마다 (session_id, user_id, client) 별 max(requested_at) 를 모은다. 처음
    나타난 구간의 max 가 그 세션의 ended_at 이다(더 최신 구간은 이미 다 읽었다). 그래서 구간
    하나를 끝내면 ``ended_at > lo`` 인 세션은 전부 알려져 있고, 필요한 수(need)가 모이면 그
    순서가 최종이다. 통계(started_at·건수·오류·비용·모델)는 고른 세션만 session 인덱스로
    ``requested_at >= cutoff`` 전체를 집계한다(원래 쿼리와 같은 범위).

    ``before`` 커서: 원래 쿼리는 HAVING max <= before — 커서 뒤에 행이 있는 세션(앞 페이지에
    나온 것)은 통째로 빠진다. 구간은 before 아래만 읽으므로, 후보 중 before 뒤에 행이 있는
    것을 session 인덱스로 확인해 버린다.

    반환 ``(rows, floor)``: rows 는 ended_at 내림차순 집계 행(최대 need 개). floor 가 None 이
    아니면 행 예산을 다 써서 ``ended_at > floor`` 까지만 확정됐다는 뜻이다(호출부가 has_more
    + 그 시각을 커서로 쓴다).
    """
    if cutoff is not None:
        floor = cutoff
    else:
        floor = (
            await session.execute(
                select(func.min(UsageLog.requested_at).label("earliest")).where(
                    UsageLog.session_id.is_not(None)
                )
            )
        ).scalar()
        if floor is None:
            return [], None
        floor = _utc(floor)

    top = before if before is not None else datetime.now(timezone.utc)
    hi = before  # None = 상한 없음(시계가 앞선 행까지 원래 쿼리처럼 포함)
    step = _SESSION_SLICE_START
    seen: set[tuple] = set()
    accepted: list = []
    scanned = 0
    while True:
        lo = top - step
        at_floor = lo <= floor
        if at_floor:
            lo = floor
        cstmt = (
            select(
                UsageLog.session_id,
                UsageLog.user_id,
                UsageLog.client,
                func.max(UsageLog.requested_at).label("ended_at"),
                func.count().label("slice_rows"),
            )
            .where(UsageLog.session_id.is_not(None))
            .group_by(UsageLog.session_id, UsageLog.user_id, UsageLog.client)
        )
        # 하한이 cutoff 이면 원래 쿼리처럼 포함(>=), 중간 구간은 (lo, hi] 반개구간.
        cstmt = cstmt.where(
            UsageLog.requested_at >= lo if at_floor else UsageLog.requested_at > lo
        )
        if hi is not None:
            cstmt = cstmt.where(UsageLog.requested_at <= hi)
        cstmt = _row_filters(cstmt, user_ids=user_ids, client=client, null_client=null_client)
        rows = (await session.execute(cstmt)).all()
        scanned += sum(int(r.slice_rows or 0) for r in rows)

        cands = []
        for r in rows:
            k = _sess_key(r.session_id, r.user_id, r.client)
            if k in seen:
                continue
            seen.add(k)
            if (
                before is not None
                and _utc(r.ended_at) == before
                and _wire_key(r.session_id, r.user_id, r.client) in exclude_keys
            ):
                continue  # 앞 페이지에서 반환된 타이 세션
            cands.append(r)

        if before is not None and cands:
            later = (
                await session.execute(
                    select(UsageLog.session_id, UsageLog.user_id, UsageLog.client)
                    .where(
                        UsageLog.session_id.in_(sorted({c.session_id for c in cands})),
                        UsageLog.requested_at > before,
                    )
                    .distinct()
                )
            ).all()
            gone = {_sess_key(x.session_id, x.user_id, x.client) for x in later}
            if gone:
                cands = [
                    c for c in cands
                    if _sess_key(c.session_id, c.user_id, c.client) not in gone
                ]

        cands.sort(key=lambda r: _utc(r.ended_at), reverse=True)
        i = 0
        while i < len(cands) and len(accepted) < need:
            size = _SESSION_AGG_BATCH if has_error else need - len(accepted)
            batch = cands[i : i + size]
            i += len(batch)
            astmt = _session_agg_stmt(
                cutoff,
                None,
                user_ids=user_ids,
                client=client,
                null_client=null_client,
                sids=sorted({c.session_id for c in batch}),
            )
            by_key = {
                _sess_key(a.session_id, a.user_id, a.client): a
                for a in (await session.execute(astmt)).all()
            }
            for c in batch:
                a = by_key.get(_sess_key(c.session_id, c.user_id, c.client))
                if a is None or (has_error and not a.error_count):
                    continue
                accepted.append(a)
                if len(accepted) >= need:
                    break
        if len(accepted) >= need or at_floor:
            return accepted, None
        if scanned >= _SESSION_SLICE_ROW_BUDGET:
            return accepted, lo
        hi = lo
        slice_rows = sum(int(r.slice_rows or 0) for r in rows)
        if slice_rows == 0:
            # 빈 구간 — 걸음을 키우며 빈 시간을 건너는 대신 그 아래 가장 최근 행으로 바로 뛴다
            # (requested_at 인덱스 역방향 한 번). 데이터가 몰린 시각에서 첫 걸음부터 다시.
            newest_stmt = _row_filters(
                select(func.max(UsageLog.requested_at).label("newest")).where(
                    UsageLog.session_id.is_not(None),
                    UsageLog.requested_at <= lo,
                    UsageLog.requested_at >= floor,
                ),
                user_ids=user_ids, client=client, null_client=null_client,
            )
            newest = (await session.execute(newest_stmt)).scalar()
            if newest is None:
                return accepted, None
            top = _utc(newest)
            step = _SESSION_SLICE_START
        else:
            # 밀도에 맞춰 넓힌다 — 다음 구간의 예상 행 수가 _SESSION_SLICE_TARGET_ROWS 를 넘지 않게.
            top = lo
            grow = min(4.0, _SESSION_SLICE_TARGET_ROWS / slice_rows)
            step = min(max(step * grow, _SESSION_SLICE_START), _SESSION_SLICE_MAX)


# ── 세션 목록 (메타데이터만 — 본문 아님, 감사 불요) ────────────────────────────
# `/{request_id}` 보다 먼저 선언해야 경로 파라미터에 삼켜지지 않는다.
@router.get("/sessions")
async def list_body_log_sessions(
    request: Request,
    days: int = Query(default=1, ge=0, le=365),
    gap_minutes: int = Query(default=_SESSION_GAP_MINUTES, ge=1, le=1440),
    limit: int = Query(default=20, ge=1, le=50),
    before: datetime | None = Query(default=None),
    before_key: list[str] = Query(default=[]),
    user: str | None = Query(default=None, max_length=200),
    client: str | None = Query(default=None, max_length=128),
    has_error: bool = Query(default=False),
    sid: str | None = Query(default=None),
    admin: CurrentUser = Depends(require_admin),
    # 읽기 전용 목록 — 분석 풀(격리 + 문장 상한)에서 돈다. 인증·쓰기 경로의 기본 풀을
    # 긴 세션 스캔이 붙잡지 않게 한다.
    session: AsyncSession = Depends(get_analytics_session),
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
    if before is not None:
        # UTC 로 정규화 — 커서 비교(ended_at ISO 문자열 포함)가 오프셋에 흔들리지 않게.
        before = _utc(before).astimezone(timezone.utc)
    # before 와 같은 ended_at 을 가진 세션 중 이미 반환한 것 — 커서는 초 단위라
    # 동시 종료 세션이 있으면 `< before` 만으로는 다음 페이지에서 빠진다.
    exclude_keys = set(before_key)

    # user 부분일치 → user_id 목록으로 해석 (email 또는 uuid 텍스트).
    # 와일드카드 이스케이프 후 ilike — NULL user_id(agent 토큰)는 자연 제외.
    user_ids = None
    user_filter_truncated = False
    if user:
        like = (
            "%"
            + user.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        # limit+1 로 가져와 잘림을 감지한다 — 짧은 부분문자열로 100명 넘게
        # 매칭돼도 조용히 잘리면 관리자가 모르니 응답에 표시한다.
        user_rows = list(
            (
                await session.execute(
                    select(User.id)
                    .where(
                        (User.email.ilike(like, escape="\\"))
                        | (cast(User.id, String).ilike(like, escape="\\"))
                    )
                    .limit(101)
                )
            ).scalars()
        )
        user_filter_truncated = len(user_rows) > 100
        user_ids = user_rows[:100]

    cutoff = None
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # ── 1) 명시적 세션 — 시간 창 걸음 집계(_explicit_sessions) + 호출은 IN 쿼리 1회 ──
    # limit+1 로 가져와 has_more 를 판별한다. sid 직행은 session 인덱스 한 번이라 그대로 집계.
    exact_floor: datetime | None = None
    if sid is not None:
        exact_rows = list(
            (
                await session.execute(
                    _session_agg_stmt(
                        cutoff,
                        limit + 1,
                        before=before,
                        user_ids=user_ids,
                        client=None if client == "other" else client,
                        has_error=has_error,
                        sid=sid,
                        null_client=client == "other",
                    )
                )
            ).all()
        )
    elif user_ids is not None and not user_ids:
        exact_rows = []  # user 필터가 아무도 못 찾음 — 스캔할 것이 없다
    else:
        exact_rows, exact_floor = await _explicit_sessions(
            session,
            cutoff=cutoff,
            before=before,
            exclude_keys=exclude_keys,
            need=limit + 1,
            user_ids=user_ids,
            client=None if client == "other" else client,
            null_client=client == "other",
            has_error=has_error,
        )
    exact_overflow = len(exact_rows) > limit
    exact_rows = exact_rows[:limit]

    calls_by_sid: dict[tuple, list] = {}
    calls_truncated = False
    sids = [r.session_id for r in exact_rows]
    if sids:
        # session_id 는 클라이언트 제어 문자열 — 같은 sid 로 수천 행을 쌓은
        # 세션이 있으면 파이썬 상한에 도달하기 전에 DB 가 전체를 스캔한다.
        # 파티션별 row_number 로 DB 측 상한을 두고, 잘림 여부는 count(*) over 로
        # 판별한다(500번째까지만 읽으면 초과 행 도착 여부를 알 수 없기 때문).
        partition = (UsageLog.session_id, UsageLog.user_id, UsageLog.client)
        inner = (
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
                UsageLog.cache_read_tokens,
                UsageLog.cache_creation_tokens,
                func.row_number()
                .over(
                    partition_by=partition,
                    order_by=(UsageLog.requested_at.asc(), UsageLog.request_id.asc()),
                )
                .label("rn"),
                func.count().over(partition_by=partition).label("cnt"),
            )
            .where(UsageLog.session_id.in_(sids))
        )
        # cutoff 는 윈도우 함수 계산 *전에* 걸러야 한다 — 밖에서 거르면
        # rn/cnt 가 전체 행 기준이 되어 창 안쪽 호출이 밀려난다.
        if cutoff is not None:
            inner = inner.where(UsageLog.requested_at >= cutoff)
        inner = inner.subquery()
        cstmt = (
            select(inner)
            .where(inner.c.rn <= _SESSION_CALLS_MAX)
            .order_by(inner.c.requested_at.asc())
        )
        # 다른 user/client 가 같은 값을 쓰면 충돌하므로 호출 매칭도
        # (session_id, user_id, client) 복합키로.
        for c in (await session.execute(cstmt)).all():
            skey = (c.session_id, str(c.user_id) if c.user_id else None, c.client)
            if c.cnt > _SESSION_CALLS_MAX:
                calls_truncated = True
            calls_by_sid.setdefault(skey, []).append(
                {
                    "request_id": c.request_id,
                    "requested_at": c.requested_at.isoformat(),
                    "model_alias": c.model_alias,
                    "status": _status_str(c.status),
                    "latency_ms": c.latency_ms,
                    "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "cache_read_tokens": c.cache_read_tokens,
                    "cache_write_tokens": c.cache_creation_tokens,
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
                "user_filter_truncated": user_filter_truncated,
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
        .limit(_SESSION_SCAN_LIMIT + 1)
    )
    if cutoff is not None:
        stmt = stmt.where(UsageLog.requested_at >= cutoff)
    if before is not None:
        # 커서보다 gap 만큼 앞의 행까지 읽는다 — 커서를 걸친 세션은 커서 이전
        # 행을 포함해 다시 그룹핑돼 조각/중복으로 재등장하므로(1페이지에 이미
        # 나온 세션) 통째로 잡아 버린다. 그룹 단위로 ended_at < before 만 남김.
        stmt = stmt.where(
            UsageLog.requested_at < before + timedelta(minutes=gap_minutes)
        )
    if user_ids is not None:
        stmt = stmt.where(UsageLog.user_id.in_(user_ids))
    if client == "other":
        # 세션 키에서 NULL client 를 'other' 로 표기 — 필터도 같은 관례로.
        stmt = stmt.where(UsageLog.client.is_(None))
    elif client is not None:
        stmt = stmt.where(UsageLog.client == client)
    rows = (await session.execute(stmt)).all()
    # 정확히 상한 개수인 경우까지 잘림으로 보면 다음 페이지도 같은 판정이라
    # 진행이 멈춘다 — limit+1 로 읽어 실제 초과 여부만 잘림으로 본다.
    truncated = len(rows) > _SESSION_SCAN_LIMIT
    rows = rows[:_SESSION_SCAN_LIMIT]
    # 스캔 하한 — 상한에 잘렸으면 가장 오래된 행 아래에 행이 더 있다. 그 경계에
    # 걸친 그룹은 반쪽 세션이므로 이번 페이지에서 버리고 커서를 그 앞에 둔다.
    scan_floor = rows[-1].requested_at if truncated and rows else None

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
    deferred_until: datetime | None = None
    deferred_sessions: list[dict] = []
    for g in groups:
        first, last = g[0], g[-1]
        gkey = f"{first.user_id or 'none'}|{first.client or 'other'}|{last.requested_at.isoformat()}"
        if before is not None and (
            last.requested_at > before
            or (last.requested_at == before and gkey in exclude_keys)
        ):
            # 커서 이후이거나 이미 반환된 타이 세션 — 버린다.
            continue
        errors = sum(
            1
            for r in g
            if r.status is not None
            and (_status_str(r.status) or "").upper() != "SUCCESS"
        )
        if has_error and errors == 0:
            continue
        sess = {
            "key": gkey,
            "session_id": None,
            "user_id": str(first.user_id) if first.user_id else None,
            "user_email": first.user_email,
            "client": first.client,
            "started_at": first.requested_at.isoformat(),
            "ended_at": last.requested_at.isoformat(),
            "call_count": len(g),
            "error_count": errors,
            # 전부 NULL 이면 None — exact 경로(func.sum → NULL)와 일치시킨다.
            "total_cost_usd": (
                sum(float(r.cost_usd) for r in g if r.cost_usd is not None)
                if any(r.cost_usd is not None for r in g)
                else None
            ),
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
                    "cache_read_tokens": r.cache_read_tokens,
                    "cache_write_tokens": r.cache_creation_tokens,
                }
                for r in g
            ],
        }
        if scan_floor is not None and (
            first.requested_at - scan_floor
        ) <= timedelta(minutes=gap_minutes):
            # 스캔 하한에 걸친 그룹 — 아래에 행이 더 있을 수 있어 반쪽 세션.
            # 다음 페이지(더 아래까지 스캔)에서 온전히 복원되도록 지연시키되,
            # 남는 세션이 하나도 없으면 진행을 위해 그대로 낸다(무한 빈 페이지
            # 방지 — kiro r2). 지연 보관은 따로 둔다.
            if deferred_until is None or last.requested_at > deferred_until:
                deferred_until = last.requested_at
            deferred_sessions.append(sess)
            continue
        sessions.append(sess)

    sessions.sort(key=lambda x: x["ended_at"], reverse=True)
    if exact_floor is not None:
        # 명시 세션 걸음이 행 예산에 멈췄다 — ended_at > floor 까지만 확정이다. 그 아래로
        # 끝나는 세션(휴리스틱 포함)은 이번 페이지에 내지 않는다. 내면 커서가 floor 아래로
        # 내려가 (커서, floor] 의 명시 세션이 어느 페이지에도 안 나온다.
        floor_iso = exact_floor.astimezone(timezone.utc).isoformat()
        sessions = [s for s in sessions if s["ended_at"] > floor_iso]
    deferred_iso = deferred_until.isoformat() if deferred_until is not None else None
    if deferred_iso is not None:
        # 스캔 하한에 걸친 그룹보다 이전에 끝나는 세션도 함께 미룬다 — 그대로
        # 반환하면 커서(returned[-1].ended_at)가 지연 구간 아래로 내려가 커버
        # 못 한 구간의 세션이 어느 페이지에도 빠진다.
        covered = [s for s in sessions if s["ended_at"] > deferred_iso]
        if covered:
            sessions = covered
        else:
            # 전부 미루면 빈 페이지가 같은 커서로 무한 반복된다 — 진행을 위해
            # 지연분까지 낸다(truncated 플래그로 반쪽임은 표시된다).
            sessions = sessions + deferred_sessions
            sessions.sort(key=lambda x: x["ended_at"], reverse=True)
    returned = sessions[:limit]
    has_more = (
        exact_overflow
        or truncated
        or len(sessions) > limit
        or deferred_iso is not None
        or exact_floor is not None
    )
    next_before = returned[-1]["ended_at"] if has_more and returned else None
    if has_more and not returned and exact_floor is not None:
        # 빈 페이지라도 진행한다 — 다음 요청은 floor 아래부터 걷는다.
        next_before = exact_floor.astimezone(timezone.utc).isoformat()
    return JSONResponse(
        {
            "sessions": returned,
            # 단위가 섞이지 않게 둘로 나눈다 — 스캔한 NULL 행 수와 집계된
            # 명시 세션 수는 다른 것이다.
            "scanned_rows": len(rows),
            "explicit_sessions": len(exact_rows),
            "truncated": truncated,
            "calls_truncated": calls_truncated,
            "user_filter_truncated": user_filter_truncated,
            "has_more": has_more,
            "next_before": next_before,
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
                "cache_write_tokens": r.cache_creation_tokens,
                "reasoning_tokens": r.reasoning_tokens,
                **_web_search_fields(r),
            }

    result = BodyLogBatchResult()
    try:
        if targets:
            store = BodyLogStore(settings.BODY_LOG_S3_BUCKET)
            result = await store.fetch_many(targets)
    except BodyLogNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        # 단건 엔드포인트와 같은 안전망 — 예외도 "열람 시도"이므로 감사에 남긴다.
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
            "cache_write_tokens": row.cache_creation_tokens,
            "reasoning_tokens": row.reasoning_tokens,
            **_web_search_fields(row),
            "record": result.record,
        },
        headers={"Cache-Control": "no-store"},
    )
