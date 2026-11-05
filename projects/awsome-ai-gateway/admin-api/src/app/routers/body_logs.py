# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Gateway body-log 뷰어 — S3 에 적재된 요청/응답 본문을 request_id 로 조회한다.

두 겹의 게이트: ``body_logging_enabled`` 런타임 토글이 꺼져 있으면 403(수집
스위치와 열람을 연동하는 운영 결정 — 과거 수집분도 끄면 안 보인다), 통과해도
열람 행위 자체가 ``VIEW_BODY_LOG`` 감사 행으로 남는다. 응답은 프롬프트 원문을
담으므로 ``Cache-Control: no-store`` 를 강제한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.usage import UsageLog
from app.services.body_log_store import (
    BodyLogLookupError,
    BodyLogNotConfigured,
    BodyLogStore,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/body-logs", tags=["Body Logs"])

#: request_id 는 S3 키 세그먼트로 들어간다 — ``/`` 와 ``..`` 를 허용하면
#: prefix 탈출로 다른 파티션을 읽을 수 있으므로 문자셋을 좁힌다.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,128}$")

#: 수집 지연 안내 임계 — Firehose 버퍼(60s)+전파 여유로, 이 안의 404는
#: "아직 안 온 것"이지 "없는 것"이 아니다.
_COLLECTING_WINDOW_S = 180


@router.get("/{request_id}")
async def get_body_log(
    request: Request,
    request_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = request.app.state.system_settings_service
    if not await svc.get_body_logging_enabled(session):
        # 403/400 초기 거절은 감사하지 않는다 — "열람 시도"가 성립하기 전의 게이트
        # 거절로, invocation-log 엔드포인트와 같은 계약이다.
        raise HTTPException(
            403, "본문 로깅이 비활성 상태입니다 — Monitoring 토글을 먼저 켜세요"
        )

    settings = get_settings()
    if not settings.BODY_LOG_S3_BUCKET:
        raise HTTPException(503, "BODY_LOG_S3_BUCKET 미설정 — body_logging 인프라 없음")

    if not _REQUEST_ID_RE.fullmatch(request_id) or ".." in request_id:
        raise HTTPException(400, "request_id 형식이 올바르지 않습니다")

    row = (
        await session.execute(
            select(
                UsageLog.user_id,
                UsageLog.team_id,
                UsageLog.provider,
                UsageLog.client,
                UsageLog.requested_at,
                UsageLog.latency_ms,
                UsageLog.model_alias,
            ).where(UsageLog.request_id == request_id)
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
        # usage 행이 없으면 partition 정보를 유도할 수 없어 S3 를 열지도 못한다 —
        # 단 비용 기록 실패로 행 자체가 없는 경우 본문은 S3 에 있을 수 있다.
        raise HTTPException(
            404,
            "해당 request_id 의 사용 기록이 없습니다 — 비용 기록이 실패한 요청이면 "
            "본문은 저장돼 있어도 이 화면에서는 찾을 수 없습니다",
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
            # 스캔 상한/파손으로 못 다 본 구간이 있다 — "없다"가 아니라 "못 찾았다".
            hint = "저장소 스캔이 상한에 걸려 전부 확인하지 못했습니다 — 본문이 있을 수 있습니다. 잠시 후 다시 시도하세요."
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
            "requested_at": row.requested_at.isoformat(),
            "record": result.record,
        },
        headers={"Cache-Control": "no-store"},
    )
