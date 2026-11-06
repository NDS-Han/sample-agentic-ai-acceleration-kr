# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""body_logs 라우터 — 게이트/검증/감사 계약 단위 테스트 (S3·DB mock)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.auth import CurrentUser
from app.models.auth import UserRole
from app.routers.body_logs import get_body_log
from app.services.body_log_store import (
    BodyLogFetchResult,
    BodyLogLookupError,
    BodyLogNotConfigured,
)

REQ = "023c673b-0686-4794-b8ac-8d1317118e7a"
NOW = datetime(2026, 11, 5, 8, 0, 0, tzinfo=timezone.utc)


def _request() -> MagicMock:
    req = MagicMock()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=True
    )
    req.client.host = "10.0.0.1"
    req.state.request_id = "t-1"
    req.headers = {}
    return req


def _admin() -> MagicMock:
    admin = MagicMock()
    admin.user_id = uuid.uuid4()
    admin.role.value = "ADMIN"
    return admin


def _session(row=None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.first.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


def _usage_row(requested_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        user_email="dev@example.com",
        team_id=None,
        provider="BEDROCK",
        client="other",
        requested_at=requested_at or NOW - timedelta(minutes=10),
        latency_ms=1200,
        model_alias="claude-sonnet",
        cost_usd=None,
        is_streaming=True,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        reasoning_tokens=0,
        ttft_ms=200,
    )


@pytest.fixture
def settings():
    with patch("app.routers.body_logs.get_settings") as gs:
        gs.return_value.BODY_LOG_S3_BUCKET = "bucket"
        yield gs


@pytest.fixture
def audit():
    with patch("app.routers.body_logs.audit_logger") as a:
        a.log = AsyncMock()
        yield a


@pytest.fixture
def store_cls():
    with patch("app.routers.body_logs.BodyLogStore") as cls:
        cls.return_value.fetch = AsyncMock()
        yield cls


@pytest.mark.asyncio
async def test_toggle_off_403_no_audit(settings, audit):
    req = _request()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=False
    )
    with pytest.raises(HTTPException) as ei:
        await get_body_log(req, REQ, _admin(), _session(_usage_row()))
    assert ei.value.status_code == 403
    audit.log.assert_not_called()


@pytest.mark.asyncio
async def test_bad_request_id_400_no_audit(settings, audit):
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), "x/../etc", _admin(), _session(_usage_row()))
    assert ei.value.status_code == 400
    audit.log.assert_not_called()


@pytest.mark.asyncio
async def test_no_usage_row_404_audit_queue_only(settings, audit, store_cls):
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(None))
    assert ei.value.status_code == 404
    store_cls.assert_not_called()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert kwargs["action"] == "VIEW_BODY_LOG"
    assert kwargs["changes"]["found"] is False
    assert kwargs["result"] == "FAILURE"


@pytest.mark.asyncio
async def test_lookup_error_502_audit(settings, audit, store_cls):
    store_cls.return_value.fetch.side_effect = BodyLogLookupError("boom")
    with pytest.raises(HTTPException) as ei:
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    assert ei.value.status_code == 502
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert kwargs["changes"]["found"] is None
    assert "boom" in kwargs["changes"]["error"]


@pytest.mark.asyncio
async def test_not_configured_503_audit(settings, audit, store_cls):
    store_cls.return_value.fetch.side_effect = BodyLogNotConfigured("no bucket")
    with pytest.raises(HTTPException) as ei:
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    assert ei.value.status_code == 503
    assert audit.log.call_args.kwargs["queue_only"] is True


@pytest.mark.asyncio
async def test_unexpected_error_audited_then_raised(settings, audit, store_cls):
    """안전망 — 서비스가 못 잡은 예외도 열람 시도는 감사되고 예외는 올라간다."""
    store_cls.return_value.fetch.side_effect = RuntimeError("weird")
    with pytest.raises(RuntimeError):
        await get_body_log(
            _request(), REQ, _admin(), _session(_usage_row())
        )
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is True
    assert "weird" in kwargs["changes"]["error"]


@pytest.mark.asyncio
async def test_success_200_audit_session_bound(settings, audit, store_cls):
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        record={"request_id": REQ, "request_body": {"messages": []}},
        source="firehose",
        key="k",
    )
    resp = await get_body_log(
        _request(), REQ, _admin(), _session(_usage_row())
    )
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    kwargs = audit.log.call_args.kwargs
    assert kwargs["queue_only"] is False
    assert kwargs["changes"]["found"] is True
    assert kwargs["result"] == "SUCCESS"
    assert kwargs["changes"]["subject_user_id"]


@pytest.mark.asyncio
async def test_not_found_recent_pending_message(settings, audit, store_cls):
    """수집 창(<3분) 안 404 는 '수집 대기' 메시지다."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult()
    recent = _usage_row(requested_at=NOW - timedelta(seconds=30))
    with patch("app.routers.body_logs.datetime") as dt:
        dt.now.return_value = NOW
        dt.side_effect = lambda *a, **k: datetime(*a, **k)
        with pytest.raises(HTTPException) as ei:
            await get_body_log(
                _request(), REQ, _admin(), _session(recent)
            )
    assert ei.value.status_code == 404
    assert "수집 대기" in ei.value.detail


@pytest.mark.asyncio
async def test_not_found_old_not_collected_message(settings, audit, store_cls):
    store_cls.return_value.fetch.return_value = BodyLogFetchResult()
    old = _usage_row(requested_at=NOW - timedelta(hours=2))
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(old))
    assert ei.value.status_code == 404
    assert "수집되지 않았습니다" in ei.value.detail


@pytest.mark.asyncio
async def test_not_found_incomplete_message(settings, audit, store_cls):
    """스캔 상한/파손이면 '미수집'이 아니라 '못 찾았을 수 있음' 메시지."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        incomplete=True
    )
    old = _usage_row(requested_at=NOW - timedelta(hours=2))
    with pytest.raises(HTTPException) as ei:
        await get_body_log(_request(), REQ, _admin(), _session(old))
    assert ei.value.status_code == 404
    assert "전부 확인하지 못했습니다" in ei.value.detail


@pytest.mark.asyncio
async def test_oversized_counts_as_found(settings, audit, store_cls):
    """oversized fallback 객체는 레코드 없이도 '존재함' — 200 + found=True."""
    store_cls.return_value.fetch.return_value = BodyLogFetchResult(
        oversized=True, source="fallback", key="k"
    )
    resp = await get_body_log(
        _request(), REQ, _admin(), _session(_usage_row())
    )
    assert resp.status_code == 200
    assert audit.log.call_args.kwargs["changes"]["found"] is True


# --- svc 토큰 차단 회귀 (Opus 리뷰 지적 — 원문 본문은 사람 admin 전용) ---


def _svc_admin() -> CurrentUser:
    """svc- 토큰의 합성 ADMIN 신원 — require_admin 은 통과한다."""
    return CurrentUser(
        user_id=uuid.uuid4(),
        email="svc@machine",
        role=UserRole.ADMIN,
        team_id=None,
        is_service_token=True,
        service_token_id=uuid.uuid4(),
    )


def _human_admin() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid4(),
        email="dev@example.com",
        role=UserRole.ADMIN,
        team_id=None,
    )


def _test_app() -> "tuple[TestClient, MagicMock, MagicMock]":
    """실제 라우터 + 실제 예외 핸들러로 마운트한 최소 앱 — 의존성만 override."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.auth import get_current_user
    from app.core.db import get_db_session
    from app.main import register_exception_handlers
    from app.routers.body_logs import router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    session = MagicMock()
    session.execute = AsyncMock()
    app.dependency_overrides[get_db_session] = lambda: session
    app.state.system_settings_service = MagicMock(
        get_body_logging_enabled=AsyncMock(return_value=True)
    )
    return TestClient(app, raise_server_exceptions=False), session, app


def test_router_level_guard_blocks_service_tokens():
    """라우터 dependencies 에 가드 자체가 등록돼 있어야 svc 토큰이 핸들러까지
    못 온다 — 이름 문자열이 아니라 함수 identity 로 확인한다."""
    from app.core.auth import forbid_service_token_reader
    from app.routers.body_logs import router

    deps = {d.dependency for d in router.dependencies}
    assert forbid_service_token_reader in deps


def test_svc_token_http_403_store_never_called():
    """HTTP 레벨 검증 — svc 토큰은 403, 설정 조회/DB/BodyLogStore 전부 미도달.

    FastAPI 의존성 그래프를 실제로 타야 "가드가 핸들러보다 먼저"를 증명한다 —
    가드 함수만 호출하는 테스트는 가드가 라우터에서 빠져도 통과한다.
    """
    from app.core.auth import get_current_user
    from app.routers.body_logs import router  # noqa: F401 — 라우터 로드

    client, session, app = _test_app()
    app.dependency_overrides[get_current_user] = _svc_admin
    with patch("app.routers.body_logs.BodyLogStore") as store:
        resp = client.get(f"/admin/body-logs/{REQ}")
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"
    assert not session.execute.called
    assert not app.state.system_settings_service.get_body_logging_enabled.called
    assert not store.called


def test_human_admin_passes_guard_http():
    """사람 admin 은 가드를 통과해 핸들러 로직까지 도달한다(401/403 아님)."""
    from app.core.auth import get_current_user

    client, session, app = _test_app()
    app.dependency_overrides[get_current_user] = _human_admin
    # AsyncMock 의 자식 mock 도 async 가 되므로 first 는 동기 MagicMock 으로 고정
    session.execute.return_value.first = MagicMock(return_value=None)
    with patch("app.routers.body_logs.get_settings") as gs:
        gs.return_value.BODY_LOG_S3_BUCKET = "bucket"
        resp = client.get(f"/admin/body-logs/{REQ}")
    # usage row 없음 → 404 — 즉 가드를 지나 핸들러까지 도달했다는 증거
    assert resp.status_code == 404
    assert app.state.system_settings_service.get_body_logging_enabled.called
    assert session.execute.called


# ── sessions 목록 + batch 다중 조회 ───────────────────────────────────────────

REQ2 = "4781da38-8565-4b37-a725-60475392f9f6"
REQ3 = "ecf65b98-faff-4dfd-abdb-8e730b64afc9"

from app.routers.body_logs import (  # noqa: E402
    _BatchRequest,
    get_body_logs_batch,
    list_body_log_sessions,
)


def _sess_row(
    rid: str,
    minutes_ago: float,
    user=None,
    client="claude-code",
    model="m",
    status="SUCCESS",
    session_id=None,
) -> SimpleNamespace:
    """status 는 prod 와 같은 str-enum — 순수 문자열로 넣으면 직렬화 회귀를 못 잡는다."""
    from app.models.usage import UsageStatus

    st = UsageStatus[status] if isinstance(status, str) else status
    uid = uuid.uuid5(uuid.NAMESPACE_DNS, user or "u")
    return SimpleNamespace(
        request_id=rid,
        requested_at=NOW - timedelta(minutes=minutes_ago),
        user_id=uid,
        user_email=f"{user or 'u'}@e",
        team_id=None,
        provider="BEDROCK",
        client=client,
        latency_ms=100,
        model_alias=model,
        status=st,
        cost_usd=None,
        is_streaming=True,
        session_id=session_id,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        reasoning_tokens=0,
        ttft_ms=50,
    )


def _agg_row(
    session_id: str,
    minutes_ago: float,
    user="u",
    client="claude-code",
    call_count=2,
    error_count=0,
    models=("m",),
) -> SimpleNamespace:
    """session_id GROUP BY 쿼리의 집계 행 — 컬럼명은 라우터의 label 과 동일."""
    return SimpleNamespace(
        session_id=session_id,
        started_at=NOW - timedelta(minutes=minutes_ago + 1),
        ended_at=NOW - timedelta(minutes=minutes_ago),
        call_count=call_count,
        error_count=error_count,
        user_id=uuid.uuid5(uuid.NAMESPACE_DNS, user),
        user_email=f"{user}@e",
        client=client,
        models=list(models),
        total_cost=None,
    )


def _session_rows(rows, exact=None, call_rows=None, uid_rows=None) -> MagicMock:
    """세 쿼리를 SQL 형태로 구분해 반환 — 집계(GROUP BY) / 호출(IN) / NULL 스캔."""
    session = MagicMock()
    r_exact = MagicMock()
    r_exact.all.return_value = exact or []
    r_calls = MagicMock()
    r_calls.all.return_value = call_rows or []
    r_null = MagicMock()
    r_null.all.return_value = rows
    r_uids = MagicMock()
    r_uids.scalars.return_value = uid_rows or []

    async def _exec(stmt, *a, **k):
        sql = str(stmt).upper()
        # user 부분일치 → user_id 해석 쿼리 (ILIKE)를 먼저 구분한다.
        if "ILIKE" in sql:
            return r_uids
        if "GROUP BY" in sql:
            return r_exact
        if "IN (" in sql:
            return r_calls
        return r_null

    session.execute = AsyncMock(side_effect=_exec)
    return session


@pytest.mark.asyncio
async def test_sessions_groups_by_gap():
    """같은 (user, client) 연속 호출이 gap 을 넘으면 두 세션으로 나뉜다."""
    rows = [
        _sess_row("a1", 5, user="x"),
        _sess_row("a2", 4, user="x"),
        _sess_row("a3", 3, user="x"),
        _sess_row("b1", 200, user="x"),  # 30분+ 전 — 다른 세션
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 2
    s_new, s_old = payload["sessions"]
    assert s_new["call_count"] == 3
    assert [c["request_id"] for c in s_new["calls"]] == ["a1", "a2", "a3"]  # 시간순(asc)
    assert s_old["call_count"] == 1


@pytest.mark.asyncio
async def test_sessions_user_boundary():
    """다른 사용자의 동시간대 호출은 다른 세션."""
    rows = [
        _sess_row("a1", 5, user="x"),
        _sess_row("b1", 4, user="y"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 2
    assert {s["user_email"] for s in payload["sessions"]} == {"x@e", "y@e"}


@pytest.mark.asyncio
async def test_sessions_explicit_session_id_groups_exactly():
    """session_id 가 있으면 같은 유저의 동시 대화도 세션별로 정확히 갈라진다."""
    exact = [
        _agg_row("sess-A", 3, user="x", call_count=2),
        _agg_row("sess-B", 4, user="x", call_count=1, error_count=1),
    ]
    calls = [
        _sess_row("c1", 4, user="x", session_id="sess-A"),
        _sess_row("c2", 3, user="x", session_id="sess-A"),
        _sess_row("d1", 4, user="x", status="ERROR", session_id="sess-B"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows([], exact=exact, call_rows=calls),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 2
    sa, sb = payload["sessions"]
    assert sa["session_id"] == "sess-A"
    assert [c["request_id"] for c in sa["calls"]] == ["c1", "c2"]
    assert sb["session_id"] == "sess-B"
    assert sb["error_count"] == 1
    assert [c["request_id"] for c in sb["calls"]] == ["d1"]


@pytest.mark.asyncio
async def test_sessions_heuristic_before_cursor_no_split():
    """휴리스틱 before 커서가 세션을 쪼개면 안 된다 (MAJOR-2 회귀).

    커서를 걸치는 세션(커서 이전+이후 행)은 1페이지에 이미 나온 세션이라
    통째로 버려야 하고, 커서보다 완전히 이전에 끝난 세션만 2페이지에 나온다.
    라우터는 행 필터를 before+gap 으로 넓히고 그룹 단위로 ended_at < before
    만 남긴다 — 쪼개진 조각이 재등장하면 안 된다.
    """
    before = NOW - timedelta(minutes=10)
    rows = [
        # 세션 A — 커서를 걸침(행 간격 10분 연속, 커서 이전·이후 행 모두 포함).
        _sess_row("a1", 25, user="x"),
        _sess_row("a2", 15, user="x"),
        _sess_row("a3", 5, user="x"),  # 커서(-10분)보다 최신
        # 세션 B — 커서보다 완전히 이전 + A 와 gap(30분) 초과로 분리 → 2페이지 대상.
        _sess_row("b1", 80, user="x"),
        _sess_row("b2", 70, user="x"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=before, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 1
    s = payload["sessions"][0]
    # 세션 A 는 통째로 버려진다 — a1/a2 조각 세션이 재등장하면 안 된다.
    assert [c["request_id"] for c in s["calls"]] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_sessions_mixed_exact_and_heuristic():
    """session_id 세션과 NULL 폴백 세션이 ended_at desc 로 병합된다."""
    exact = [_agg_row("sess-A", 10, user="x", call_count=1)]
    calls = [_sess_row("c1", 10, user="x", session_id="sess-A")]
    null_rows = [_sess_row("h1", 2, user="y")]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(),
        session=_session_rows(null_rows, exact=exact, call_rows=calls),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 2
    # NULL 휴리스틱 세션(2분 전)이 명시적 세션(10분 전)보다 최신 — 정렬 병합 확인.
    assert payload["sessions"][0]["session_id"] is None
    assert payload["sessions"][1]["session_id"] == "sess-A"


@pytest.mark.asyncio
async def test_sessions_collision_splits_calls_by_owner():
    """같은 session_id 를 쓰는 다른 유저 — 세션은 갈라지고 호출도 섞이지 않는다."""
    exact = [
        _agg_row("dup", 3, user="x", call_count=1),
        _agg_row("dup", 4, user="y", call_count=1),
    ]
    calls = [
        _sess_row("cx", 3, user="x", session_id="dup"),
        _sess_row("cy", 4, user="y", session_id="dup"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows([], exact=exact, call_rows=calls),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert len(payload["sessions"]) == 2
    assert payload["sessions"][0]["key"] != payload["sessions"][1]["key"]
    by_user = {s["user_email"]: s for s in payload["sessions"]}
    assert [c["request_id"] for c in by_user["x@e"]["calls"]] == ["cx"]
    assert [c["request_id"] for c in by_user["y@e"]["calls"]] == ["cy"]


def test_session_agg_stmt_no_uuid_aggregate():
    """session_id 집계 SQL 에 max(uuid) 계열이 없어야 한다 — Postgres 는 uuid 에
    순서 연산이 없어 UndefinedFunctionError 로 500 이 난다(라이브 회귀)."""
    from app.routers.body_logs import _session_agg_stmt

    sql = str(_session_agg_stmt(None, 20)).lower()
    assert "group by" in sql
    assert "usage_logs.user_id" in sql  # group 키로는 쓴다
    # 집계 위치에 uuid 가 오면 안 된다 — max(…user_id…)/min(…user_id…) 패턴 검출.
    import re as _re

    assert not _re.search(r"(max|min)\s*\([^)]*user_id", sql)


@pytest.mark.asyncio
async def test_sessions_toggle_off_403():
    req = _request()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=False
    )
    with pytest.raises(HTTPException) as ei:
        await list_body_log_sessions(
            req, days=1, gap_minutes=30, limit=20,
            before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows([]),
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_batch_mixed_found_missing(settings, audit, store_cls):
    """found + firehose-missing + usage-row-missing 혼합 응답과 단일 감사 행."""
    from app.services.body_log_store import BodyLogBatchResult

    rec = {"request_id": REQ}
    batch = BodyLogBatchResult(
        records={REQ: rec},
        sources={REQ: "firehose"},
        keys={REQ: "k"},
        missing=[REQ2],
    )
    store_cls.return_value.fetch_many = AsyncMock(return_value=batch)

    # REQ, REQ2 는 usage row 있음 / GONE 은 없음
    session = MagicMock()
    rows = [_sess_row(REQ, 5), _sess_row(REQ2, 6)]
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)

    resp = await get_body_logs_batch(
        _request(),
        _BatchRequest(request_ids=[REQ, REQ2, REQ3, "bad/../id", REQ]),
        _admin(),
        session,
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert payload["records"][REQ]["record"] == rec
    assert payload["records"][REQ]["found"] is True
    # REQ2(firehose 스캔 미스) + REQ3(usage row 없음) — 둘 다 missing 에 합산
    assert set(payload["missing"]) == {REQ2, REQ3}
    # 감사 한 번, found 목록에 REQ
    assert audit.log.call_count == 1
    changes = audit.log.call_args.kwargs["changes"]
    assert changes["found"] == [REQ]
    # 무효 id + 중복 제거돼 4개만 요청된 것으로 기록
    assert sorted(changes["request_ids"]) == sorted([REQ, REQ2, REQ3])
    # 스토어는 usage row 있는 id 만 타겟
    sent = store_cls.return_value.fetch_many.call_args.args[0]
    assert {t.request_id for t in sent} == {REQ, REQ2}


@pytest.mark.asyncio
async def test_batch_empty_ids_no_store_call(settings, audit, store_cls):
    """id 가 전부 무효/없으면 스토어를 열지 않고 감사만 남긴다."""
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    resp = await get_body_logs_batch(
        _request(),
        _BatchRequest(request_ids=["../x", ""]),
        _admin(),
        session,
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert payload["records"] == {}
    store_cls.return_value.fetch_many.assert_not_called()


@pytest.mark.asyncio
async def test_batch_toggle_off_403(settings, audit):
    req = _request()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=False
    )
    with pytest.raises(HTTPException) as ei:
        await get_body_logs_batch(
            req, _BatchRequest(request_ids=[REQ]), _admin(), MagicMock()
        )
    assert ei.value.status_code == 403
    audit.log.assert_not_called()


@pytest.mark.asyncio
async def test_sessions_route_and_batch_route_registered():
    """경로 파라미터 라우트보다 literal 라우트가 먼저 등록됐는지 — 순서 회귀."""
    from app.routers.body_logs import router

    paths = [r.path for r in router.routes]
    assert paths.index("/admin/body-logs/sessions") < paths.index(
        "/admin/body-logs/{request_id}"
    )


@pytest.mark.asyncio
async def test_sessions_error_count_only_non_success():
    """str-enum 행에서 SUCCESS 는 에러가 아니다 — error_count 와 calls[].status
    둘 다 wire 값(SUCCESS/ERROR)으로 나가야 한다."""
    rows = [
        _sess_row("ok1", 5, user="x", status="SUCCESS"),
        _sess_row("ok2", 4, user="x", status="SUCCESS"),
        _sess_row("bad1", 3, user="x", status="ERROR"),
        _sess_row("tmo1", 2, user="x", status="TIMEOUT"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows),
    )
    import json as _json

    s = _json.loads(resp.body)["sessions"][0]
    assert s["call_count"] == 4
    assert s["error_count"] == 2
    assert [c["status"] for c in s["calls"]] == [
        "SUCCESS",
        "SUCCESS",
        "ERROR",
        "TIMEOUT",
    ]


@pytest.mark.asyncio
async def test_sessions_sid_direct_skips_heuristic():
    """sid 직행 — 명시 세션만 반환하고 NULL 스캔 경로는 결과에 안 섞는다.

    딥링크 용도라 목록 스캔을 생략한다 — scanned_rows 가 0 이고 같은 유저의
    다른 휴리스틱 세션이 있어도 나오지 않아야 한다.
    """
    exact = [_agg_row("sess-abc", 5, user="x", call_count=2)]
    calls = [
        _sess_row("a1", 6, user="x", session_id="sess-abc"),
        _sess_row("a2", 5, user="x", session_id="sess-abc"),
    ]
    # NULL 행도 흘려보낸다 — sid 모드에서는 읽히면 안 된다.
    null_rows = [_sess_row("z1", 1, user="x", session_id=None)]
    resp = await list_body_log_sessions(
        _request(), days=30, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid="sess-abc",
        admin=_admin(),
        session=_session_rows(null_rows, exact=exact, call_rows=calls),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert payload["scanned_rows"] == 0
    assert payload["has_more"] is False
    assert len(payload["sessions"]) == 1
    s = payload["sessions"][0]
    assert s["session_id"] == "sess-abc"
    assert [c["request_id"] for c in s["calls"]] == ["a1", "a2"]


@pytest.mark.asyncio
async def test_sessions_sid_charset_validated():
    """sid 는 클라이언트 제어 — 세션키 문자셋 밖이면 400."""
    req = _request()
    req.app.state.system_settings_service.get_body_logging_enabled = AsyncMock(
        return_value=True
    )
    with pytest.raises(HTTPException) as ei:
        await list_body_log_sessions(
            req, days=1, gap_minutes=30, limit=20,
            before=None, user=None, client=None, has_error=False,
            sid="bad session!",
            admin=_admin(), session=MagicMock(),
        )
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_sessions_user_filter_no_match_returns_empty():
    """user 부분일치가 user_id 를 못 찾으면 스캔 없이 빈 목록."""
    rows = [_sess_row("a1", 5, user="x")]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user="nobody@x", client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows, uid_rows=[]),
    )
    import json as _json

    assert _json.loads(resp.body)["sessions"] == []


@pytest.mark.asyncio
async def test_sessions_has_more_and_next_before():
    """limit+1 조회로 has_more 판별 — next_before 는 마지막 반환 세션의 ended_at."""
    exact = [
        _agg_row("s1", 5, user="x"),
        _agg_row("s2", 10, user="x"),  # limit 초과분 — 잘리고 has_more 표시
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=1,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(),
        session=_session_rows([], exact=exact, call_rows=[]),
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert payload["has_more"] is True
    assert len(payload["sessions"]) == 1
    assert payload["next_before"] == payload["sessions"][0]["ended_at"]
    assert payload["sessions"][0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_sessions_has_error_filters_heuristic_groups():
    """has_error 는 휴리스틱 그룹을 사후 필터 — 에러 없는 세션은 빠진다."""
    rows = [
        _sess_row("ok1", 3, user="x", status="SUCCESS"),
        _sess_row("ok2", 2, user="x", status="SUCCESS"),
        # 다른 유저의 에러 세션 (같은 시각대)
        _sess_row("bad1", 3, user="y", status="ERROR"),
    ]
    resp = await list_body_log_sessions(
        _request(), days=1, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=True, sid=None,
        admin=_admin(), session=_session_rows(rows),
    )
    import json as _json

    sessions = _json.loads(resp.body)["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["error_count"] == 1
    assert sessions[0]["user_email"] == "y@e"


@pytest.mark.asyncio
async def test_single_response_includes_cost_token_meta(settings, audit, store_cls):
    """단건 응답에 usage 행의 비용/토큰/지연 메타가 실린다."""
    store_cls.return_value.fetch = AsyncMock(
        return_value=BodyLogFetchResult(
            record={"request_id": REQ}, source="firehose", key="k"
        )
    )
    row = _usage_row()
    row.cost_usd = Decimal("0.012345")
    resp = await get_body_log(
        _request(), REQ, _admin(), _session(row)
    )
    import json as _json

    payload = _json.loads(resp.body)
    assert payload["cost_usd"] == 0.012345
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 50
    assert payload["ttft_ms"] == 200
    assert payload["client"] == "other"
    assert payload["latency_ms"] == 1200


@pytest.mark.asyncio
async def test_sessions_truncated_scan_defers_boundary_group(monkeypatch):
    """스캔 상한 경계 그룹은 지연 — 커서가 커버리지 아래로 점프해 세션을 잃지 않는다.

    페이지1: 스캔 상한(4행)에 걸려 경계 그룹 S2와 그 아래 명시 세션은 지연된다.
    페이지2: 더 깊은 스캔으로 S2가 완전한 행 수로 복원되고 지연됐던 세션도 나온다.
    """
    import json as _json

    import app.routers.body_logs as bl

    # 페이지 1 — 스캔 상한 4행에 딱 걸리는 데이터 (o3 는 상한 아래라 못 읽음)
    rows_p1 = [
        _sess_row("n1", 5, user="x"),   # S1 (07:55)
        _sess_row("n2", 10, user="x"),  # S1
        _sess_row("o1", 300, user="x"), # S2 (03:00) — 경계 그룹
        _sess_row("o2", 310, user="x"), # S2 — 스캔 하한 행 (02:50)
    ]
    exact = [_agg_row("sess-old", 500, user="y", call_count=1)]

    monkeypatch.setattr(bl, "_SESSION_SCAN_LIMIT", 4)
    resp = await list_body_log_sessions(
        _request(), days=0, gap_minutes=30, limit=20,
        before=None, user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows_p1, exact=exact),
    )
    p1 = _json.loads(resp.body)
    # 경계 그룹 S2(02:50 지연)와 그보다 이전 sess-old 는 이번 페이지에서 빠진다
    assert len(p1["sessions"]) == 1
    s1 = p1["sessions"][0]
    assert s1["call_count"] == 2
    assert p1["has_more"] is True
    assert p1["next_before"] == s1["ended_at"]

    # 페이지 2 — 스캔이 더 깊어 o3 까지 읽음(상한 해제). before_key 로 S1 을
    # 타이 배제해 같은 ended_at 이어도 중복 반환되지 않아야 한다.
    rows_p2 = rows_p1 + [_sess_row("o3", 315, user="x")]  # o2 와 5분 — 같은 세션
    monkeypatch.setattr(bl, "_SESSION_SCAN_LIMIT", 10)
    resp2 = await list_body_log_sessions(
        _request(), days=0, gap_minutes=30, limit=20,
        before=datetime.fromisoformat(p1["next_before"]),
        before_key=[s1["key"]],
        user=None, client=None, has_error=False, sid=None,
        admin=_admin(), session=_session_rows(rows_p2, exact=exact),
    )
    p2 = _json.loads(resp2.body)
    keys = [s["key"] for s in p2["sessions"]]
    # S1 재등장 없음 (타이 배제) — S2 는 하한 아래 행까지 합쳐 3콜로 복원
    assert s1["key"] not in keys
    s2 = next(s for s in p2["sessions"] if s["session_id"] is None)
    assert s2["call_count"] == 3
    assert [c["request_id"] for c in s2["calls"]] == ["o3", "o2", "o1"]
    # 지연됐던 명시 세션도 이 페이지에 나온다 — 두 페이지 합집합이 전체를 덮음
    assert any(s["session_id"] == "sess-old" for s in p2["sessions"])
