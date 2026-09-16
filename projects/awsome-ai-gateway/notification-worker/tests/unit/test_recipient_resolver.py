# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""RecipientResolver 단위 테스트 — DB를 AsyncMock으로 대체."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.models.auth import Team, User
from worker.schemas.recipients import RecipientRole
from worker.services.recipient_resolver import RecipientResolver


# spec_set (not spec) is deliberate: it makes an assignment to an attribute that does not
# exist on the real model raise AttributeError. These fixtures previously set `name` and
# `roles`, neither of which exists on auth.users (the columns are `display_name` and `role`),
# and plain `spec` allows that — so the resolver received an auto-created child MagicMock,
# Recipient() rejected it, RecipientResolver swallowed the exception, and the tests asserted
# on an empty list. spec_set turns that silent drift into an immediate failure.
def _make_user(
    user_id: str = "u1",
    email: str = "user@example.com",
    display_name: str = "Alice",
    team_id: str = "t1",
    role: str = "DEVELOPER",
    is_active: bool = True,
) -> User:
    u = MagicMock(spec_set=User)
    u.id = user_id
    u.email = email
    u.display_name = display_name
    u.team_id = team_id
    u.role = role
    u.is_active = is_active
    return u


def _make_team(team_id: str = "t1", leader_user_id: str | None = "u2") -> Team:
    t = MagicMock(spec_set=Team)
    t.id = team_id
    t.leader_user_id = leader_user_id
    return t


def _make_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


async def test_resolve_affected_user() -> None:
    user = _make_user("u1", "alice@example.com", "Alice")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session.execute = AsyncMock(return_value=result)

    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=[RecipientRole.AFFECTED_USER],
        payload={"user_id": "u1"},
    )

    assert len(recipients) == 1
    assert recipients[0].email == "alice@example.com"
    assert recipients[0].role == RecipientRole.AFFECTED_USER


async def test_resolve_affected_user_not_found_returns_empty() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=[RecipientRole.AFFECTED_USER],
        payload={"user_id": "nonexistent"},
    )
    assert recipients == []


async def test_resolve_affected_user_no_user_id_returns_empty() -> None:
    session = AsyncMock()
    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=[RecipientRole.AFFECTED_USER],
        payload={},  # user_id 없음
    )
    assert recipients == []


async def test_resolve_admin_returns_all_admins() -> None:
    admin1 = _make_user("u1", "admin1@example.com", "Admin1", role="ADMIN")
    admin2 = _make_user("u2", "admin2@example.com", "Admin2", role="ADMIN")

    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [admin1, admin2]
    session.execute = AsyncMock(return_value=result)

    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=[RecipientRole.ADMIN],
        payload={},
    )

    assert len(recipients) == 2
    emails = {r.email for r in recipients}
    assert "admin1@example.com" in emails
    assert "admin2@example.com" in emails


async def test_resolve_deduplicates_emails() -> None:
    """동일 이메일이 여러 역할로 해소되면 중복 제거."""
    user = _make_user("u1", "alice@example.com", "Alice")

    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    result.scalars.return_value.all.return_value = [user]
    session.execute = AsyncMock(return_value=result)

    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=[RecipientRole.AFFECTED_USER, RecipientRole.ADMIN],
        payload={"user_id": "u1"},
    )

    emails = [r.email for r in recipients]
    assert emails.count("alice@example.com") == 1


async def test_resolve_unknown_role_returns_empty() -> None:
    session = AsyncMock()
    resolver = RecipientResolver(_make_factory(session))
    recipients = await resolver.resolve(
        roles=["unknown_role"],
        payload={},
    )
    assert recipients == []
