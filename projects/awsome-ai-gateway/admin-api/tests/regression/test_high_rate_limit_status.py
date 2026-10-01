# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""단건 rate-limit 조회/삭제 — /users 패널 "상위 정책 따라가기" 의 백엔드.

검증 대상:
  1. USER own 없음 → 팀 설정이 inherited 로 내려간다(get_rate_limit_tree 와 같은 규칙)
  2. USER own 있음 → inherited 는 비어야 한다(팀값이 같이 오면 UI 가 출처를 못 가림)
  3. TEAM 은 상위 상속이 없다 — inherited 항상 None
  4. DELETE 는 행 비활성화 + proxy 캐시 무효화(rl:config:* 패턴) + 감사 로그
  5. DELETE 는 설정이 없어도 멱등 성공 — 상속 상태에서 눌러도 에러가 아니다
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import NotFoundError
from app.models.model import RateLimitScope
from app.services.rate_limit_service import RateLimitService


def _cfg(scope_id=None, rpm=None, tpm=None, cpm=None, cph=None):
    c = MagicMock()
    c.scope_id = scope_id
    c.rpm_limit = rpm
    c.tpm_limit = tpm
    c.cpm_limit_usd = cpm
    c.cph_limit_usd = cph
    return c


def _user(team_id=None):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.team_id = team_id
    return u


def _svc():
    cache_mgr = MagicMock()
    cache_mgr._redis = MagicMock()
    cache_mgr._redis.delete = AsyncMock()
    cache_mgr.invalidate_pattern = AsyncMock()
    return RateLimitService(cache_mgr=cache_mgr), cache_mgr


def _repo_factory(configs=None, *, deactivated=1):
    """patch 대상 리포지토리 팩토리 — get_active(scope, scope_id) 라우팅.

    configs: {(scope, scope_id): RateLimitConfig|None}
    """
    configs = configs or {}
    repo = MagicMock()
    repo.deactivate_configs = AsyncMock(return_value=deactivated)

    async def _get_active(scope, scope_id):
        return configs.get((scope, scope_id))

    repo.get_active = AsyncMock(side_effect=_get_active)
    return repo


async def test_user_without_own_inherits_team():
    svc, _ = _svc()
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    team_cfg = _cfg(scope_id=team_id, rpm=100, tpm=50000)
    session = AsyncMock()
    session.get = AsyncMock(return_value=_user(team_id=team_id))
    repo = _repo_factory({(RateLimitScope.USER, user_id): None, (RateLimitScope.TEAM, team_id): team_cfg})
    with patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo):
        st = await svc.get_rate_limit_status(session, scope=RateLimitScope.USER, scope_id=user_id)
    assert st.own is None
    assert st.inherited is not None and st.inherited.rpm == 100
    assert st.inherited_scope == "TEAM"


async def test_user_with_own_has_no_inherited():
    svc, _ = _svc()
    user_id = uuid.uuid4()
    own = _cfg(scope_id=user_id, rpm=30)
    session = AsyncMock()
    session.get = AsyncMock(return_value=_user(team_id=uuid.uuid4()))
    repo = _repo_factory({(RateLimitScope.USER, user_id): own})
    with patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo):
        st = await svc.get_rate_limit_status(session, scope=RateLimitScope.USER, scope_id=user_id)
    assert st.own is not None and st.own.rpm == 30
    assert st.inherited is None and st.inherited_scope is None


async def test_team_never_has_inherited():
    svc, _ = _svc()
    team_id = uuid.uuid4()
    session = AsyncMock()
    repo = _repo_factory({(RateLimitScope.TEAM, team_id): _cfg(scope_id=team_id, rpm=50)})
    with patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo):
        st = await svc.get_rate_limit_status(session, scope=RateLimitScope.TEAM, scope_id=team_id)
    assert st.own is not None and st.inherited is None
    session.get.assert_not_called()  # TEAM 은 사용자 조회를 하지 않는다


async def test_user_not_found_raises():
    svc, _ = _svc()
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    repo = _repo_factory()
    with patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo):
        with pytest.raises(NotFoundError):
            await svc.get_rate_limit_status(
                session, scope=RateLimitScope.USER, scope_id=uuid.uuid4()
            )


async def test_delete_deactivates_and_invalidates_proxy_cache():
    svc, cache_mgr = _svc()
    user_id = uuid.uuid4()
    session = AsyncMock()
    actor = MagicMock()
    actor.user_id = uuid.uuid4()
    actor.role.value = "ADMIN"
    repo = _repo_factory(deactivated=1)
    with (
        patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo),
        patch("app.services.rate_limit_service.audit_logger") as audit,
    ):
        audit.log = AsyncMock()
        await svc.delete_rate_limit(
            session, scope=RateLimitScope.USER, scope_id=user_id, actor=actor
        )
    repo.deactivate_configs.assert_awaited_once_with(RateLimitScope.USER, user_id)
    # proxy 가 실제로 읽는 키 패턴이 무효화돼야 다음 요청부터 상속이 적용된다.
    cache_mgr.invalidate_pattern.assert_awaited_once()
    pattern = cache_mgr.invalidate_pattern.await_args.args[0]
    assert pattern == f"rl:config:USER:{user_id}:*"
    audit.log.assert_awaited_once()


async def test_delete_is_idempotent_when_nothing_set():
    svc, _ = _svc()
    session = AsyncMock()
    actor = MagicMock()
    actor.user_id = uuid.uuid4()
    actor.role.value = "ADMIN"
    repo = _repo_factory(deactivated=0)
    with (
        patch("app.services.rate_limit_service.RateLimitConfigRepository", return_value=repo),
        patch("app.services.rate_limit_service.audit_logger") as audit,
    ):
        audit.log = AsyncMock()
        await svc.delete_rate_limit(
            session, scope=RateLimitScope.TEAM, scope_id=uuid.uuid4(), actor=actor
        )
    # 예외 없이 끝나야 한다 — "상속으로 되돌리기"를 이미 상속 상태에서 눌러도 안전.
