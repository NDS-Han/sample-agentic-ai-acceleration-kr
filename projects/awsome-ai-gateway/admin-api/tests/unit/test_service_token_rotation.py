# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""service token self-rotate 의 수명 상한 회귀.

수정 전: ``rotate`` → ``issue(expiry_days=None)`` → 매번 새 90 일. 유출된 토큰이
rotate 를 반복하면 expires_at 이 무기한 연장됐다(로테이션이 수명 연장으로
오용됨).

수정 후 계약:
- 새 토큰의 expires_at 은 구 토큰의 expires_at 을 넘지 못한다.
- 이미 만료/폐기된 토큰은 rotate 를 거부한다(죽은 자격 부활 방지).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ValidationError
from app.services.service_token_service import ServiceTokenService


def _old_token(*, days_left: float, revoked: bool = False, expired: bool = False):
    now = datetime.now(timezone.utc)
    tok = MagicMock()
    tok.id = uuid.uuid4()
    tok.name = "ci-bot"
    tok.created_by = uuid.uuid4()
    tok.expires_at = now + timedelta(seconds=-1) if expired else now + timedelta(days=days_left)
    tok.revoked_at = (now - timedelta(hours=1)) if revoked else None
    return tok


def _svc():
    return ServiceTokenService()


@pytest.mark.asyncio
async def test_rotation_caps_new_expiry_at_old_expiry():
    """잔여 30 일 토큰의 rotate 는 90 일이 아니라 ≤30 일짜리 토큰을 낸다."""
    session = AsyncMock()
    old = _old_token(days_left=30)
    svc = _svc()

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=old)
    repo.set_revoked_at = AsyncMock()
    repo.create = AsyncMock()

    with patch(
        "app.services.service_token_service.ServiceTokenRepository",
        return_value=repo,
    ):
        resp = await svc.rotate(
            session, token_id=old.id, created_by=uuid.uuid4()
        )

    assert resp.expires_at <= old.expires_at
    # 그리고 실제로 단축됐다(상한이 적용된 증거 — 90 일이면 이 단정은 실패)
    assert resp.expires_at < datetime.now(timezone.utc) + timedelta(days=31)


@pytest.mark.asyncio
async def test_rotation_of_fresh_token_keeps_full_window():
    """잔여 수명이 90 일보다 긴 토큰은 기본 90 일 상한이 적용된다."""
    session = AsyncMock()
    old = _old_token(days_left=200)
    svc = _svc()

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=old)
    repo.set_revoked_at = AsyncMock()
    repo.create = AsyncMock()

    with patch(
        "app.services.service_token_service.ServiceTokenRepository",
        return_value=repo,
    ):
        resp = await svc.rotate(
            session, token_id=old.id, created_by=uuid.uuid4()
        )

    now = datetime.now(timezone.utc)
    assert resp.expires_at <= now + timedelta(days=91)
    assert resp.expires_at > now + timedelta(days=89)


@pytest.mark.asyncio
async def test_expired_token_cannot_rotate():
    """만료 토큰의 rotate 거부 — 부활 경로 차단."""
    session = AsyncMock()
    old = _old_token(days_left=0, expired=True)
    svc = _svc()

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=old)

    with patch(
        "app.services.service_token_service.ServiceTokenRepository",
        return_value=repo,
    ):
        with pytest.raises(ValidationError):
            await svc.rotate(session, token_id=old.id, created_by=uuid.uuid4())


@pytest.mark.asyncio
async def test_revoked_token_cannot_rotate():
    """폐기 완료된 토큰의 rotate 거부."""
    session = AsyncMock()
    old = _old_token(days_left=30, revoked=True)
    svc = _svc()

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=old)

    with patch(
        "app.services.service_token_service.ServiceTokenRepository",
        return_value=repo,
    ):
        with pytest.raises(ValidationError):
            await svc.rotate(session, token_id=old.id, created_by=uuid.uuid4())


@pytest.mark.asyncio
async def test_rerotate_during_grace_does_not_extend_old_token_life():
    """grace 기간 중 재로테이션이 구 토큰의 폐기 시각을 뒤로 밀지 않는다.

    authenticate 는 ``revoked_at > now`` 를 아직 유효로 본다 — 매 rotate 마다
    ``revoked_at = now+24h`` 로 덮으면, grace 안에 rotate 를 반복하는 것만으로
    유출 토큰이 영구히 산다. 종료 시각은 min(신규 grace, 기존 revoked_at) 여야 한다.
    """
    session = AsyncMock()
    now = datetime.now(timezone.utc)
    old = _old_token(days_left=30)
    old.revoked_at = now + timedelta(hours=20)  # 기존 grace 4h 남은 상태
    svc = _svc()

    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=old)
    repo.set_revoked_at = AsyncMock()
    repo.create = AsyncMock(side_effect=lambda t: t)

    with patch(
        "app.services.service_token_service.ServiceTokenRepository",
        return_value=repo,
    ):
        await svc.rotate(session, token_id=old.id, created_by=uuid.uuid4())

    set_args = repo.set_revoked_at.call_args[0]
    # 폐기 시각이 기존 grace(20h 후)를 넘기면 안 된다 — 연장이면 무한 연장 루프.
    assert set_args[1] <= old.revoked_at
