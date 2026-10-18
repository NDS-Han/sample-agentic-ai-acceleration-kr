# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: R25 라운드 2(kiro/opus) 발견 — 앱 정책 서브쿼리 상관 누락,
예산 분모의 D-cap 전용 config, drain 이 shield 된 persist 를 안 기다리는 결함.

- **M1** — ``_build_app_policy`` 의 ``team_org_id`` 스칼라 서브쿼리가
  ``exists()`` 안에 중첩되면 SQLAlchemy 자동 상관이 바깥 EXISTS select 까지만
  닿아, 렌더된 SQL 이 ``FROM teams JOIN departments, users`` 로 ``users`` 를
  다시 FROM 에 넣었다. 같은 팀 사용자가 2명 이상이면 스칼라 서브쿼리가 다중 행을
  돌려 ``CardinalityViolationError``(500) — 운영 데이터에서는 항상 재현.
  ``.correlate(User)`` 로 고쳤으므로 컴파일된 SQL 에 콤마-조인된 ``auth.users``
  가 없어야 한다.

- **S1-잔여** — 대시보드 예산 사용률 분모: D-cap(기본 하향)만 저장된 팀 config 는
  ``max_budget_usd=NULL`` 이라 SUM 에서 빠지는데 분자 사용액은 들어가 사용률이
  950% 같은 값으로 부풀었다. EXISTS 가 ``max_budget_usd.isnot(None)`` 을
  요구하도록 고쳤다 — 소스에 해당 필터가 있어야 한다.

- **N-b / R3-S2** — ``drain_chat_bg_tasks`` 가 진입 스냅샷만 cancel 하고 shield 된
  persist(_spawn 으로 ``_bg_tasks`` 에 등록됨)를 기다리지 않아, 사용자 Stop 직후
  pod 종료 시 부분 답변이 유실될 수 있었다. drain 은 이제 ``_bg_tasks`` 가 빌
  때까지 기다린다. R3-S2 추가: 스냅샷에 이미 있던 persist 와 사용자 취소를
  처리 중인 producer(``cancelling() > 0``)를 다시 cancel 하면 안 된다 — 둘 다
  assistant 행 유실로 실측됐다. persist 는 ``_persist_tasks`` 로 따로 추적해
  cancel 대상에서 제외한다.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.routers import apps as apps_mod
from app.routers import chat_agent as m


# ──────────────────────────────────────────────────────────────────────────────
# M1 — _build_app_policy 스칼라 서브쿼리 상관(correlate) 회귀
# ──────────────────────────────────────────────────────────────────────────────


class _EmptyResult:
    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def all(self):
        return []


class _CapturingSession:
    """execute 호출의 statement 만 기록하고 빈 결과를 돌려주는 대역."""

    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, stmt, params=None):  # noqa: ARG002
        self.statements.append(stmt)
        return _EmptyResult()


async def test_app_policy_subquery_is_correlated_to_users():
    """렌더된 SQL 의 team→dept→org 스칼라 서브쿼리 FROM 에 auth.users 가
    다시 끼어들면(비상관) 다중 행 반환 → CardinalityViolationError."""
    session = _CapturingSession()
    await apps_mod._build_app_policy(session, "claude-code")

    selects = [s for s in session.statements if isinstance(s, Select)]
    assert selects, "_build_app_policy 가 ORM select 를 실행하지 않음"
    sql = str(
        selects[-1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    # 결함 시그니처: 서브쿼리가 `FROM auth.teams JOIN auth.departments ...,
    # auth.users` 로 users 를 콤마-조인. `auth.users` 단독 등장(WHERE 상관)은
    # 정상이므로, JOIN 이후 영역에서 콤마-조인 여부만 본다.
    join_idx = sql.find("JOIN auth.departments")
    assert join_idx >= 0, "team→dept 조인이 없음 — 쿼리 구조가 바뀜"
    assert ", auth.users" not in sql[join_idx:], (
        "team_org_id 서브쿼리가 비상관 — .correlate(User) 누락 회귀"
    )


# ──────────────────────────────────────────────────────────────────────────────
# S1-잔여 — 예산 분모가 한도 있는 config 만 요구
# ──────────────────────────────────────────────────────────────────────────────


def test_dashboard_budget_denominator_excludes_dcap_only_configs():
    src = open(
        __import__("pathlib").Path(apps_mod.__file__).parent.parent
        / "routers"
        / "dashboard.py"
    ).read()
    assert "max_budget_usd.isnot(None)" in src, (
        "usage_scope_has_active_config 가 한도 있는 config 만 확인하지 않음 — "
        "D-cap 전용 팀 사용액이 분자에만 들어가 사용률이 부풀려짐"
    )


# ──────────────────────────────────────────────────────────────────────────────
# N-b — drain 이 취소 핸들러가 띄운 shield persist 까지 기다림
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _clean_bg_tasks():
    yield
    # 테스트가 남긴 태스크가 다른 테스트에 새지 않게 정리한다.
    for t in list(m._bg_tasks):
        t.cancel()
    m._bg_tasks.clear()
    m._persist_tasks.clear()


async def test_drain_waits_for_persist_spawned_during_cancellation(
    _clean_bg_tasks,
):
    """producer 가 cancel 되면서 핸들러 안에서 _spawn_persist 한 persist 도 drain 이
    기다려야 한다 — 안 기다리면 engine dispose/루프 종료로 부분 답변이 유실."""
    persisted = False

    async def persist():
        nonlocal persisted
        await asyncio.sleep(0.05)
        persisted = True

    async def producer():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # chat producer 의 CancelledError 경로와 같은 패턴: shield + _spawn_persist.
            await asyncio.shield(m._spawn_persist(persist()))
            raise

    m._spawn(producer())
    await asyncio.sleep(0)  # producer 진입 보장
    await asyncio.wait_for(m.drain_chat_bg_tasks(timeout=5.0), timeout=10.0)
    assert persisted, "drain 이 shield 된 persist 를 기다리지 않고 반환"


async def test_drain_does_not_cancel_a_persist_already_in_flight(
    _clean_bg_tasks,
):
    """R3-S2 — drain 진입 시점에 이미 ``_bg_tasks`` 에 있던 persist 를 다시
    cancel 하면 shield 가 무의미해져 완결 답변이 유실된다(실측). drain 은
    persist 를 cancel 하지 않고 완주를 기다려야 한다."""
    persisted = False

    async def persist():
        nonlocal persisted
        await asyncio.sleep(0.05)
        persisted = True

    # drain 진입 "전에" 이미 persist 가 돌고 있는 상황(정상 경로 persist).
    m._spawn_persist(persist())
    await asyncio.sleep(0)
    await asyncio.wait_for(m.drain_chat_bg_tasks(timeout=5.0), timeout=10.0)
    assert persisted, "drain 이 진행 중 persist 를 cancel 해 답변이 유실됨"


async def test_drain_does_not_re_cancel_a_producer_being_cancelled(
    _clean_bg_tasks,
):
    """R3-S2 — 사용자 Stop 으로 이미 취소 핸들러 안에 있는 producer 를 drain 이
    다시 cancel 하면 핸들러의 shield/to_thread await 에서 두 번째
    CancelledError 가 올라와(except Exception 에 안 잡힘) persist 없이 빠진다."""
    persisted = False
    entered_handler = asyncio.Event()

    async def persist():
        nonlocal persisted
        await asyncio.sleep(0.05)
        persisted = True

    async def producer():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            entered_handler.set()
            # 사용자 취소 경로: 여기서 두 번째 cancel 이 오면 await 이 깨져
            # 아래 persist spawn 에 도달하지 못한다.
            await asyncio.sleep(0.02)  # stop_runtime_session 같은 비동기 정리
            await asyncio.shield(m._spawn_persist(persist()))
            raise

    t = m._spawn(producer())
    await asyncio.sleep(0)
    t.cancel()  # 사용자 Stop — cancelling() == 1 상태로 핸들러 진입
    await entered_handler.wait()
    await asyncio.wait_for(m.drain_chat_bg_tasks(timeout=5.0), timeout=10.0)
    assert persisted, "drain 이 취소 처리 중인 producer 를 재취소해 persist 유실"


async def test_concurrent_post_409_does_not_eject_the_owner_relay(
    _clean_bg_tasks, monkeypatch,
):
    """두 번째 post_message 가 로컬 클레임 409 를 받을 때, 첫 요청이 등록한
    ``_active_relays[sid]`` 를 뜯어내면 안 된다 — 뜯기면 재구독 404 + 다음
    post 가 통과해 이중 실행이 된다(클레임 해제는 소유자 검증 필수)."""
    import uuid as _uuid
    from types import SimpleNamespace

    from fastapi import HTTPException

    sid = str(_uuid.uuid4())
    owner_relay = m._StreamRelay()
    m._active_relays[sid] = owner_relay

    class _Row(SimpleNamespace):
        pass

    class _Sess:
        async def execute(self, _stmt, *a, **k):
            return SimpleNamespace(
                first=lambda: _Row(
                    user_id="u1",
                    expires_at=__import__("datetime").datetime(
                        2999, 1, 1, tzinfo=__import__("datetime").timezone.utc
                    ),
                )
            )

        async def commit(self):
            pass

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    monkeypatch.setattr(m, "AGENTCORE_RUNTIME_ARN", "arn:aws:test")
    try:
        with pytest.raises(HTTPException) as ei:
            await m.post_message(
                req,
                _uuid.UUID(sid),
                m.MessageCreate(content="hi"),
                admin=SimpleNamespace(user_id="u1"),
                session=_Sess(),
            )
        assert ei.value.status_code == 409
        assert m._active_relays[sid] is owner_relay
    finally:
        m._active_relays.pop(sid, None)


async def test_drain_still_bounded_when_task_ignores_cancellation(
    _clean_bg_tasks,
):
    """취소를 삼키는 태스크가 있어도 drain 은 timeout 안에 반환한다."""
    async def stubborn():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(60)

    m._spawn(stubborn())
    await asyncio.sleep(0)
    await asyncio.wait_for(m.drain_chat_bg_tasks(timeout=0.2), timeout=5.0)
