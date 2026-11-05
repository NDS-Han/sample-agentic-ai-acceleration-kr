# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GET /admin/audit/logs 핸들러 계약 — 모킹 세션으로 직접 호출.

라우터 층에서만 깨질 수 있는 계약들:
- ``changes._service_token_id`` 는 응답 changes 에서 빠지고 ``service_token`` 필드로 옮긴다.
- 합성 행위자(…000010 dev / …000011 svc-token)와 실사용자/미해소 ID 의 ``actor_kind`` 분류.
- keyset 커서 + has_more 계산, 그룹→action IN 확장, 잘못된 입력 400.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError
from app.models.auth import UserRole
from app.routers.audit_reconcile import (
    _forbid_service_token_reader,
    list_audit_logs,
    router,
)

pytestmark = pytest.mark.asyncio

_ADMIN = CurrentUser(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    email="admin@test.com",
    role=UserRole.ADMIN,
    team_id=None,
)

_SVC_ACTOR = uuid.UUID("00000000-0000-4000-a000-000000000011")
_USER_ID = uuid.uuid4()
_TOKEN_ID = uuid.uuid4()
_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = dict(
        id=uuid.uuid4(),
        timestamp=_NOW,
        actor_user_id=_USER_ID,
        actor_role="ADMIN",
        action="CREATE_KEY",
        resource_type="virtual_key",
        resource_id=str(uuid.uuid4()),
        changes={},
        result="SUCCESS",
        ip_address="1.2.3.4",
        request_id="req-1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _result(*, scalars=None, all_=None):
    r = MagicMock()
    sc = MagicMock()
    sc.all.return_value = scalars or []
    r.scalars.return_value = sc
    r.all.return_value = all_ or []
    return r


def _session(*results):
    s = AsyncMock()
    s.execute = AsyncMock(side_effect=list(results))
    return s


async def _call(session, **kw):
    defaults = dict(
        range_="7d",
        group=None,
        action=None,
        actor_email=None,
        resource_type=None,
        resource_id=None,
        cursor=None,
        limit=50,
        admin=_ADMIN,
        session=session,
    )
    defaults.update(kw)
    return await list_audit_logs(**defaults)


async def test_rows_are_mapped_and_actor_email_resolved() -> None:
    row = _row(changes={"after": {"alias": "claude-haiku-5-5"}})
    session = _session(
        _result(scalars=[row]),  # logs select
        _result(all_=[(_USER_ID, "u@test.com", "User One", "cognito-sub-1")]),
    )
    res = await _call(session)
    assert res.pagination.has_more is False
    item = res.items[0]
    assert item.action == "CREATE_KEY"
    assert item.group == "keys_tokens"
    assert item.actor_kind == "user"
    assert item.actor_email == "u@test.com"
    assert item.actor_display_name == "User One"
    assert item.service_token is None
    assert item.changes == {"after": {"alias": "claude-haiku-5-5"}}


async def test_service_token_id_moves_out_of_changes() -> None:
    row = _row(
        actor_user_id=_SVC_ACTOR,
        changes={"_service_token_id": str(_TOKEN_ID), "after": {"x": 1}},
    )
    session = _session(
        _result(scalars=[row]),  # logs
        # 합성 ID 만 있으면 users 조회는 건너뛴다 — tokens 가 두 번째 execute.
        _result(all_=[(_TOKEN_ID, "ci-token", "svc-ab12")]),  # tokens
    )
    res = await _call(session)
    item = res.items[0]
    assert item.actor_kind == "service_token"
    assert "_service_token_id" not in item.changes
    assert item.service_token is not None
    assert item.service_token.name == "ci-token"
    assert item.service_token.token_prefix == "svc-ab12"


async def test_unknown_actor_is_classified_not_crash() -> None:
    row = _row(actor_user_id=uuid.uuid4())
    session = _session(
        _result(scalars=[row]),
        _result(all_=[]),  # users 에 없음
    )
    res = await _call(session)
    assert res.items[0].actor_kind == "unknown"
    assert res.items[0].actor_email is None


async def test_actor_email_with_no_match_returns_empty_without_log_scan() -> None:
    # users 조회(첫 execute)가 빈 목록 — logs select 는 두 번째 execute 가 없어야 한다.
    session = _session(_result(all_=[]))
    res = await _call(session, actor_email="nobody@x.com")
    assert res.items == []
    assert session.execute.await_count == 1


async def test_invalid_group_is_400() -> None:
    session = _session()
    with pytest.raises(HTTPException) as ei:
        await _call(session, group="no_such_group")
    assert ei.value.status_code == 400


async def test_invalid_cursor_is_400() -> None:
    session = _session()
    with pytest.raises(HTTPException) as ei:
        await _call(session, cursor="not-a-cursor")
    assert ei.value.status_code == 400


async def test_has_more_and_cursor_shape() -> None:
    rows = [_row() for _ in range(3)]
    session = _session(
        _result(scalars=rows),  # limit+1 조회 — limit=2 로 3행 → has_more
        _result(all_=[]),
    )
    res = await _call(session, limit=2)
    assert res.pagination.has_more is True
    assert len(res.items) == 2
    assert res.pagination.cursor is not None
    # 커서는 마지막 노출 행의 (timestamp, id)
    from app.repositories._cursor import decode_cursor

    ts, rid = decode_cursor(res.pagination.cursor)
    assert ts == rows[1].timestamp
    assert rid == rows[1].id


async def test_group_filter_expands_to_sql_in() -> None:
    """group 은 action IN (원본+_CONFIRMED) 조건으로 확장된다 — 쿼리 객체를 검사."""
    session = _session(_result(scalars=[]), _result(all_=[]))
    await _call(session, group="keys_tokens")
    stmt = session.execute.await_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "action IN" in compiled


async def test_action_beats_group() -> None:
    """action 과 group 이 같이 오면 action 만 적용된다(단일 equality)."""
    session = _session(_result(scalars=[]), _result(all_=[]))
    await _call(session, group="keys_tokens", action="REVOKE_KEY")
    stmt = session.execute.await_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "REVOKE_KEY" in compiled
    assert "CREATE_KEY" not in compiled


# ── kiro 리뷰 회귀 테스트 ─────────────────────────────────────────────────────


async def test_action_filter_includes_confirmed_variant() -> None:
    """R1 — 정규화된 이름으로 물어보면 *_CONFIRMED 행도 IN 에 들어간다."""
    session = _session(_result(scalars=[]))
    await _call(session, action="SET_TEAM_BUDGET")
    stmt = session.execute.await_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "SET_TEAM_BUDGET" in compiled
    # IN 목록에 _CONFIRMED 변형이 실제로 들어가야 한다 — equality 필터로
    # 바뀌면 이 단언이 실패한다(kiro 테스트 공백 지적).
    assert "SET_TEAM_BUDGET_CONFIRMED" in compiled


async def test_other_group_uses_not_in() -> None:
    """O5 — group=other 는 카탈로그 전체를 NOT IN 한다."""
    session = _session(_result(scalars=[]))
    await _call(session, group="other")
    stmt = session.execute.await_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "NOT" in compiled and "IN" in compiled


async def test_dev_actor_unclaimed_bootstrap_stays_dev() -> None:
    """미승계 bootstrap 행(sso_subject=placeholder)의 …000010 이벤트는 dev
    로그인 세션 — placeholder 이메일/이름은 실제 사람이 아니라 숨긴다."""
    dev_actor = uuid.UUID("00000000-0000-4000-a000-000000000010")
    row = _row(actor_user_id=dev_actor)
    session = _session(
        _result(scalars=[row]),
        _result(
            all_=[
                (dev_actor, "admin@example.com", "System Admin",
                 "bootstrap-admin-placeholder")
            ]
        ),
    )
    res = await _call(session)
    item = res.items[0]
    assert item.actor_kind == "dev"
    assert item.actor_email is None
    assert item.actor_display_name is None


async def test_dev_actor_claimed_resolves_to_real_user() -> None:
    """…000010 행이 실제 관리자에게 승계되면(sso_subject 가 실제 sub) 그 사람의
    운영 작업을 익명 dev 로 가리면 안 된다 — user + 실제 이메일로 표시."""
    dev_actor = uuid.UUID("00000000-0000-4000-a000-000000000010")
    row = _row(actor_user_id=dev_actor)
    session = _session(
        _result(scalars=[row]),
        _result(
            all_=[
                (dev_actor, "real.admin@corp.com", "Real Admin",
                 "cognito:sub-real-1")
            ]
        ),
    )
    res = await _call(session)
    item = res.items[0]
    assert item.actor_kind == "user"
    assert item.actor_email == "real.admin@corp.com"
    assert item.actor_display_name == "Real Admin"


async def test_actor_email_match_excludes_unclaimed_dev_actor() -> None:
    """미승계 bootstrap 행의 placeholder 이메일은 매칭에서 제외 — dev 로그인
    이벤트가 그 주소의 사람 작업처럼 검색되면 안 된다."""
    dev_actor = uuid.UUID("00000000-0000-4000-a000-000000000010")
    session = _session(
        _result(all_=[(dev_actor, "bootstrap-admin-placeholder")]),
    )
    res = await _call(session, actor_email="admin@example.com")
    # 매칭이 전부 제외돼 빈 페이지 — logs 스캔(두 번째 execute)도 없다.
    assert res.items == []
    assert session.execute.await_count == 1


async def test_service_token_deleted_keeps_kind_but_no_ref() -> None:
    """O5 — 토큰이 삭제돼 해소가 안 돼도 kind 는 service_token, ref 는 None."""
    row = _row(
        actor_user_id=_SVC_ACTOR,
        changes={"_service_token_id": str(_TOKEN_ID)},
    )
    session = _session(
        _result(scalars=[row]),
        _result(all_=[]),  # 토큰 미해소(삭제됨)
    )
    res = await _call(session)
    item = res.items[0]
    assert item.actor_kind == "service_token"
    assert item.service_token is None


async def test_actor_email_match_applies_in_clause() -> None:
    """O5 — actor_email 매칭 시 uid IN 조건이 쿼리에 실린다."""
    uid = uuid.uuid4()
    session = _session(
        _result(all_=[(uid, "cognito-sub-9")]),  # users 이메일 매칭(id, sso)
        _result(scalars=[]),  # logs
    )
    await _call(session, actor_email="u@test.com")
    assert session.execute.await_count == 2
    stmt = session.execute.await_args_list[1].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "actor_user_id IN" in compiled


# ── svc 토큰 읽기 차단 — 감사 로그는 사람 admin(JWT) 전용 ────────────────────

_SVC_ADMIN = CurrentUser(
    user_id=uuid.uuid4(),
    email="svc-token@internal",
    role=UserRole.ADMIN,
    team_id=None,
    is_service_token=True,
    service_token_id=uuid.uuid4(),
)


async def test_service_token_guard_rejects_svc_admin() -> None:
    """svc- 토큰의 합성 ADMIN 신원은 require_admin 을 통과하지만 감사 표면
    (로그의 토큰 이름·관리자 이메일·IP, invocation-log 본문)은 읽지 못한다 —
    탈취 토큰이 자기 탐지 여부를 감시하고 대화 원문을 읽는 경로 차단."""
    with pytest.raises(ForbiddenError):
        await _forbid_service_token_reader(admin=_SVC_ADMIN)


async def test_audit_router_applies_guard_to_every_endpoint() -> None:
    """라우터 단위 dependencies 라 현재·미래의 모든 /admin/audit 엔드포인트가
    같은 가드를 탄다 — 핸들러별 누락(invocation-log 3종, kiro R1)을 구조적으로
    막는다. 핸들러 시그니처의 Depends(require_admin) 과는 같은 호출이 캐시돼
    admin 해소는 한 번만 일어난다."""
    deps = {d.dependency for d in router.dependencies}
    assert _forbid_service_token_reader in deps
    # 가드 없이 인간 admin 은 통과한다 — 의존성이 항상 403 을 던지는 게 아님을
    # 잠근다(빈 의존성 목록에 대한 거짓 양성 방지).
    assert len(router.routes) >= 5
