# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Bedrock invocation log 감사 표면 (ADMIN 전용, 읽기 전용).

GPT-5.6 을 표준 runtime plane 으로 호출하면 Bedrock 이 요청/응답 본문까지 CloudWatch 에
기록한다(Mantle plane 은 기록하지 않는다). 로그가 쌓여 있어도 **우리 과금 행과 맞춰볼 수단이
없으면 감사에 못 쓴다** — 이 라우터가 그 수단이다.

- `GET /admin/audit/invocation-log/config`     설정 상태(UI 가 감사 탭을 켤지 판단)
- `GET /admin/audit/invocation-log/reconcile`  구간 대조 리포트
- `GET /admin/audit/invocation-log/{id}`       단일 레코드(본문은 기본 마스킹 + 설정 게이트)

⚠️ 경로 선언 순서가 계약이다: `{bedrock_request_id}` 를 먼저 선언하면 FastAPI 가
`/config`·`/reconcile` 를 path param 으로 삼켜 두 엔드포인트가 사라진다. 리터럴 경로를 먼저.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.usage import UsageLog
from app.services.invocation_log_service import (
    InvocationLogNotConfigured,
    InvocationLogQueryError,
    InvocationLogService,
    UsageRowRef,
    padded_window,
    reconcile,
)

router = APIRouter(prefix="/admin/audit", tags=["Audit"])

# 한 번의 대조에서 읽는 usage 행 상한. 넘으면 잘렸다고 **명시** 한다(조용한 축소 금지).
_ROW_LIMIT_DEFAULT = 5_000
_ROW_LIMIT_MAX = 50_000
# 구간 상한 — Logs Insights 쿼리 비용과 응답 시간을 사람이 예측할 수 있게 묶어둔다.
_MAX_WINDOW_HOURS = 24 * 7


def _build_invocation_log_service() -> InvocationLogService:
    """boto3 CloudWatch Logs 클라이언트로 서비스 생성(lazy import — pricing 패턴과 동일).

    Region 은 게이트웨이 홈리전이 아니라 **모델이 실행된 Region** 이다. invocation log 는
    호출이 일어난 Region 에 쌓이므로(GPT-5.6 runtime plane = us-east-2), 홈리전으로 조회하면
    로그가 하나도 없어 전부 `missing` 으로 보인다.
    """
    import boto3  # noqa: PLC0415 — 요청 시점에만 필요(AWS 미설정 환경에서 import 비용 회피)

    settings = get_settings()
    if not settings.BEDROCK_INVOCATION_LOG_GROUP:
        raise InvocationLogNotConfigured(
            "BEDROCK_INVOCATION_LOG_GROUP 이 비어 있습니다. "
            "deployment/scripts/provision_bedrock_invocation_logging.py deploy "
            "(또는 terraform module bedrock-invocation-logging) 로 켠 뒤 helm values 의 "
            "adminApi.env.BEDROCK_INVOCATION_LOG_GROUP / _REGION 을 설정하세요"
        )
    client = boto3.client("logs", region_name=settings.BEDROCK_INVOCATION_LOG_REGION)
    return InvocationLogService(
        client,
        log_group=settings.BEDROCK_INVOCATION_LOG_GROUP,
        region=settings.BEDROCK_INVOCATION_LOG_REGION,
        query_timeout_s=settings.BEDROCK_INVOCATION_LOG_QUERY_TIMEOUT_S,
        max_records=settings.BEDROCK_INVOCATION_LOG_MAX_RECORDS,
    )


def _resolve_window(
    hours: int,
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    """`start`/`end` 가 있으면 그것을, 없으면 `hours` 만큼의 최근 구간을 쓴다."""
    now = datetime.now(timezone.utc)
    if start is not None and end is not None:
        core_start, core_end = start, end
    elif start is not None or end is not None:
        raise HTTPException(400, "start 와 end 는 함께 지정해야 합니다")
    else:
        core_start, core_end = now - timedelta(hours=hours), now
    if core_start.tzinfo is None:
        core_start = core_start.replace(tzinfo=timezone.utc)
    if core_end.tzinfo is None:
        core_end = core_end.replace(tzinfo=timezone.utc)
    if core_end <= core_start:
        raise HTTPException(400, "end 는 start 보다 뒤여야 합니다")
    if core_end - core_start > timedelta(hours=_MAX_WINDOW_HOURS):
        raise HTTPException(400, f"구간이 너무 깁니다 (최대 {_MAX_WINDOW_HOURS}시간)")
    return core_start, core_end


@router.get("/invocation-log/config")
async def invocation_log_config(
    admin: CurrentUser = Depends(require_admin),
):
    """감사 기능의 설정 상태. UI 는 이 값으로 감사 탭을 켜고, 미설정 시 안내를 띄운다."""
    settings = get_settings()
    return {
        "configured": bool(settings.BEDROCK_INVOCATION_LOG_GROUP),
        "log_group": settings.BEDROCK_INVOCATION_LOG_GROUP,
        "region": settings.BEDROCK_INVOCATION_LOG_REGION,
        "bodies_enabled": settings.BEDROCK_INVOCATION_LOG_BODIES_ENABLED,
        "query_timeout_s": settings.BEDROCK_INVOCATION_LOG_QUERY_TIMEOUT_S,
        "max_records": settings.BEDROCK_INVOCATION_LOG_MAX_RECORDS,
        # plane 별 캡처 여부 — UI 가 "Mantle 은 원래 없음" 을 설명할 수 있게 서버가 알려준다.
        "capture_by_plane": {
            "BEDROCK_RUNTIME_OPENAI": True,
            "BEDROCK_MANTLE_OPENAI": False,
            "BEDROCK_MANTLE": False,
            "BEDROCK": True,
            "OPENMODEL": False,
        },
    }


@router.get("/invocation-log/reconcile")
async def invocation_log_reconcile(
    hours: int = Query(1, ge=1, le=_MAX_WINDOW_HOURS),
    start: datetime | None = Query(None, description="ISO8601. end 와 함께 지정"),
    end: datetime | None = Query(None, description="ISO8601. start 와 함께 지정"),
    model_alias: str | None = Query(None, description="특정 alias 만 대조"),
    client: str | None = Query(None, description="특정 client(codex 등)만 대조"),
    pad_minutes: int = Query(10, ge=0, le=120, description="로그 조회 구간 패딩(분)"),
    row_limit: int = Query(_ROW_LIMIT_DEFAULT, ge=1, le=_ROW_LIMIT_MAX),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """구간의 `usage_logs` ↔ Bedrock invocation log 대조 리포트.

    `missing` = 과금은 했는데 감사 근거가 없는 행(진짜 알람).
    `skipped_null` = `bedrock_request_id` 가 NULL 인 행의 **이유별 집계** — Mantle plane 은
    로그를 아예 남기지 않으므로 이건 결함이 아니다(자세한 근거는 서비스 docstring).
    """
    core_start, core_end = _resolve_window(hours, start, end)

    stmt = (
        select(
            UsageLog.request_id,
            UsageLog.bedrock_request_id,
            UsageLog.model_alias,
            UsageLog.provider,
            UsageLog.requested_at,
            UsageLog.web_search_count,
            UsageLog.is_streaming,
        )
        .where(UsageLog.requested_at >= core_start, UsageLog.requested_at <= core_end)
        .order_by(UsageLog.requested_at.asc())
        .limit(row_limit + 1)  # +1 로 "잘렸는지" 를 감지한다
    )
    if model_alias:
        stmt = stmt.where(UsageLog.model_alias == model_alias)
    if client:
        stmt = stmt.where(UsageLog.client == client)

    db_rows = (await session.execute(stmt)).all()
    rows_truncated = len(db_rows) > row_limit
    if rows_truncated:
        db_rows = db_rows[:row_limit]

    refs = [
        UsageRowRef(
            request_id=r.request_id,
            bedrock_request_id=r.bedrock_request_id,
            model_alias=r.model_alias,
            provider=r.provider,
            requested_at=r.requested_at,
            web_search_count=r.web_search_count or 0,
            is_streaming=bool(r.is_streaming),
        )
        for r in db_rows
    ]

    try:
        svc = _build_invocation_log_service()
        log_start, log_end = padded_window(core_start, core_end, pad_minutes=pad_minutes)
        window = await svc.fetch_request_ids(start=log_start, end=log_end)
    except InvocationLogNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvocationLogQueryError as exc:
        raise HTTPException(502, str(exc)) from exc

    report = reconcile(
        refs,
        window,
        window_start=log_start,
        window_end=log_end,
        log_group=svc.log_group,
        region=svc.region,
        core_start=core_start,
        core_end=core_end,
    )
    payload = report.as_dict()
    payload["core_start"] = core_start.isoformat()
    payload["core_end"] = core_end.isoformat()
    payload["pad_minutes"] = pad_minutes
    payload["filters"] = {"model_alias": model_alias, "client": client}
    payload["usage_rows_truncated"] = rows_truncated
    if rows_truncated:
        payload["notes"].append(
            f"usage 행이 row_limit={row_limit} 에서 잘렸습니다 — 구간을 좁히거나 "
            "row_limit 을 올리세요. 이 리포트는 전수가 아닙니다."
        )
    return payload


@router.get("/invocation-log/{bedrock_request_id}")
async def invocation_log_record(
    request: Request,
    bedrock_request_id: str,
    hours: int = Query(24, ge=1, le=_MAX_WINDOW_HOURS, description="레코드를 찾을 최근 구간"),
    include_bodies: bool = Query(False, description="요청/응답 본문 포함(설정으로 게이트됨)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """단일 invocation 레코드 조회.

    본문(프롬프트 원문)은 두 겹으로 막혀 있다: `include_bodies=true` 를 명시해야 하고,
    `BEDROCK_INVOCATION_LOG_BODIES_ENABLED` 가 켜져 있어야 한다. 켜고 조회하면 **그 조회
    자체가** `audit.audit_logs` 에 남는다 — 감사자를 감사하는 기록이 없으면 본문 열람 권한은
    승인받을 수 없다.
    """
    settings = get_settings()
    if include_bodies and not settings.BEDROCK_INVOCATION_LOG_BODIES_ENABLED:
        raise HTTPException(
            403,
            "본문 조회가 비활성 상태입니다 (BEDROCK_INVOCATION_LOG_BODIES_ENABLED=false)",
        )

    now = datetime.now(timezone.utc)
    log_start, log_end = padded_window(now - timedelta(hours=hours), now, pad_minutes=10)
    try:
        svc = _build_invocation_log_service()
        record = await svc.fetch_record(
            bedrock_request_id,
            start=log_start,
            end=log_end,
            include_bodies=include_bodies,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except InvocationLogNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvocationLogQueryError as exc:
        raise HTTPException(502, str(exc)) from exc

    if include_bodies:
        # 조회 성공/실패 무관하게 남긴다 — "없더라" 도 열람 시도의 사실이다.
        await audit_logger.log(
            session,
            actor_user_id=admin.user_id,
            actor_role=admin.role.value,
            action="VIEW_INVOCATION_LOG_BODY",
            resource_type="BedrockInvocationLog",
            resource_id=bedrock_request_id[:256],
            changes={
                "log_group": svc.log_group,
                "region": svc.region,
                "found": record is not None,
                "window_hours": hours,
            },
            result="SUCCESS" if record is not None else "FAILURE",
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )

    if record is None:
        # 404 의 의미를 좁혀 준다: Mantle plane 호출이면 레코드는 애초에 존재하지 않는다.
        raise HTTPException(
            404,
            f"'{bedrock_request_id}' 레코드를 최근 {hours}시간 구간에서 찾지 못했습니다. "
            "Mantle plane 호출이면 invocation log 가 생성되지 않습니다(정상). "
            "runtime plane 호출이면 구간을 넓히거나 Region 설정을 확인하세요",
        )
    return record
