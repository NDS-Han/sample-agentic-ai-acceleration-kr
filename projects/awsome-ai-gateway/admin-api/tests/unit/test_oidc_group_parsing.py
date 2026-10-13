# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDCService._parse_group — Cognito 그룹명 파싱 결정론 테스트.

규칙 (underscore 개수 기반):
  - "Claude_<team>"          → (None, "team")      Default Department 팀
  - "Claude_<dept>_<team>"   → ("dept", "team")
  - 그 외                    → None (reject)
"""
from __future__ import annotations

import pytest

from app.services.oidc_service import OIDCService


@pytest.mark.parametrize(
    "group_name,expected",
    [
        # 정상 케이스
        ("Claude_backend", (None, "backend")),
        ("Claude_ML", (None, "ML")),
        ("Claude_ai_platform", ("ai", "platform")),
        ("Claude_test-department_aws-test", ("test-department", "aws-test")),
        ("Claude_AI-Center_S/W-Culture-Office", ("AI-Center", "S/W-Culture-Office")),

        # prefix 불일치
        ("Engineers", None),
        ("", None),
        ("claude_backend", None),  # case-sensitive
        ("ClaudeAdmin", None),     # prefix 없음 (admin 부트스트랩은 별도 로직)

        # underscore 0개 — prefix 만 있고 tail 없음
        ("Claude_", None),

        # underscore 3개+ — 모호
        ("Claude_a_b_c", None),
        ("Claude_a_b_c_d", None),

        # 빈 세그먼트
        ("Claude__team", None),       # 부서 빈 문자열
        ("Claude_dept_", None),       # 팀 빈 문자열
        ("Claude__", None),
    ],
)
def test_parse_group(group_name: str, expected: tuple[str | None, str] | None) -> None:
    assert OIDCService._parse_group(group_name) == expected


# ── _upsert_user: 로그인 시 수동 지정 TEAM_LEADER 보존 ────────────────────────
# TEAM_LEADER 는 Cognito 그룹이 아니라 admin-ui("팀 리더 지정")에서만 부여된다 —
# derive_role 은 ADMIN/DEVELOPER 만 반환한다. 재로그인이 그 값을 DEVELOPER 로
# 되돌리지 않도록 보존하되, ADMIN 승격은 그대로 반영한다.

def _session():
    """AsyncMock 세션 — begin_nested 만 실제 async CM(MagicMock)으로 둔다."""
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=MagicMock())
    return session


def _session_without_leader_pointer():
    """``release_stale_leader_pointer`` 의 Team 조회가 '포인터 없음'을 보는 세션."""
    from unittest.mock import AsyncMock, MagicMock

    session = _session()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_upsert_user_preserves_manual_team_leader():
    """TEAM_LEADER 사용자가 재로그인(DEVELOPER 파생)해도 role 이 보존된다."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.models.auth import UserRole

    team_id = uuid.uuid4()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.email = "dev@test.com"
    existing.display_name = "Dev"
    existing.role = UserRole.TEAM_LEADER
    existing.team_id = team_id

    with patch("app.services.oidc_service.UserRepository") as MockRepo, \
         patch("app.services.oidc_service.get_settings") as MockSettings:
        MockRepo.return_value.get_by_sso_subject = AsyncMock(return_value=existing)
        MockRepo.return_value.get_by_email = AsyncMock(return_value=None)
        MockSettings.return_value.OIDC_PROVIDER_NAME = "cognito"

        user, created, team_changed = await OIDCService._upsert_user(
            MagicMock(),
            session=_session_without_leader_pointer(),
            sso_subject="sub-1",
            email="dev@test.com",
            display_name="Dev",
            team_id=team_id,
            role=UserRole.DEVELOPER,  # Cognito 그룹에서 파생 — 리더 그룹은 없다
        )

    assert user.role == UserRole.TEAM_LEADER
    assert created is False


@pytest.mark.asyncio
async def test_upsert_user_admin_promotion_beats_team_leader():
    """ADMIN_GROUPS 매칭으로 파생된 ADMIN 은 TEAM_LEADER 보존보다 우선한다."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.models.auth import UserRole

    team_id = uuid.uuid4()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.email = "dev@test.com"
    existing.display_name = "Dev"
    existing.role = UserRole.TEAM_LEADER
    existing.team_id = team_id

    with patch("app.services.oidc_service.UserRepository") as MockRepo, \
         patch("app.services.oidc_service.get_settings") as MockSettings:
        MockRepo.return_value.get_by_sso_subject = AsyncMock(return_value=existing)
        MockRepo.return_value.get_by_email = AsyncMock(return_value=None)
        MockSettings.return_value.OIDC_PROVIDER_NAME = "cognito"

        user, _, _ = await OIDCService._upsert_user(
            MagicMock(),
            session=_session_without_leader_pointer(),
            sso_subject="sub-1",
            email="dev@test.com",
            display_name="Dev",
            team_id=team_id,
            role=UserRole.ADMIN,
        )

    assert user.role == UserRole.ADMIN


# ── _upsert_user: 미검증 이메일 계정 연결 거부 (계정 탈취 차단) ────────────────
# 이메일은 검증 가능한 식별자가 아니다 — IdP 가 email_verified 를 확인하지 않은
# 주소로 기존 계정의 sso_subject 를 갈아끼우면 공격자가 피해자 주소를 주장하는
# 것만으로 계정을 탈취할 수 있다.

def _existing_account(email: str):
    import uuid
    from unittest.mock import MagicMock

    from app.models.auth import UserRole

    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = email
    u.sso_subject = "victim-old-sub"
    u.role = UserRole.DEVELOPER
    u.team_id = uuid.uuid4()
    u.is_active = True
    return u


@pytest.mark.asyncio
async def test_upsert_user_refuses_email_link_when_unverified():
    """sso_subject miss + email hit + 미검증 → 연결 거부(403 매핑 예외)."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    import pytest as _pytest

    from app.models.auth import UserRole
    from app.services.oidc_service import OIDCNotProvisionableError

    existing = _existing_account("victim@test.com")

    with patch("app.services.oidc_service.UserRepository") as MockRepo, \
         patch("app.services.oidc_service.get_settings") as MockSettings:
        MockRepo.return_value.get_by_sso_subject = AsyncMock(return_value=None)
        MockRepo.return_value.get_by_email = AsyncMock(return_value=existing)
        MockRepo.return_value.create_user = AsyncMock()
        MockSettings.return_value.OIDC_PROVIDER_NAME = "cognito"

        with _pytest.raises(OIDCNotProvisionableError):
            await OIDCService._upsert_user(
                MagicMock(),
                session=_session(),
                sso_subject="attacker-new-sub",
                email="victim@test.com",
                display_name="Attacker",
                team_id=uuid.uuid4(),
                role=UserRole.DEVELOPER,
                email_verified=False,
            )

    # sso_subject 가 갈아끼워지지 않았고, 새 행 생성도 시도하지 않았다
    # (시도하면 email UNIQUE 충돌로 500 이 된다 — 거부가 그보다 먼저여야 한다).
    assert existing.sso_subject == "victim-old-sub"
    MockRepo.return_value.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_user_links_email_when_verified():
    """검증된 이메일이면 재생성 계정 연결(재조정)이 정상 동작한다."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.models.auth import UserRole

    existing = _existing_account("victim@test.com")

    with patch("app.services.oidc_service.UserRepository") as MockRepo, \
         patch("app.services.oidc_service.get_settings") as MockSettings:
        MockRepo.return_value.get_by_sso_subject = AsyncMock(return_value=None)
        MockRepo.return_value.get_by_email = AsyncMock(return_value=existing)
        MockSettings.return_value.OIDC_PROVIDER_NAME = "cognito"

        user, created, _ = await OIDCService._upsert_user(
            MagicMock(),
            session=_session_without_leader_pointer(),
            sso_subject="new-sub-after-recreate",
            email="victim@test.com",
            display_name="Victim",
            team_id=uuid.uuid4(),
            role=UserRole.DEVELOPER,
            email_verified=True,
        )

    assert user.sso_subject == "new-sub-after-recreate"
    assert created is False


# ── _get_or_create_team: UNIQUE 경합에서 승자 행 재조회 ────────────────────────
# 두 exchange 가 같은 신규 팀을 동시에 만들면 uq_teams_dept_name 인덱스가 패자
# INSERT 를 승자 커밋까지 블록한 뒤 IntegrityError 를 낸다. savepoint 롤백 후
# 재조회하면 승자 행이 보여야 한다 — 재조회가 또 None 이면 그대로 raise.

def _nested_session():
    """begin_nested 만 실제 async context manager 인 세션 mock."""
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    session.begin_nested = MagicMock(return_value=cm)
    return session


@pytest.mark.asyncio
async def test_team_create_race_loser_recovers_winner_row():
    """create_team 이 IntegrityError 로 지면 재조회 결과(승자)를 반환한다."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from sqlalchemy.exc import IntegrityError

    svc = OIDCService.__new__(OIDCService)
    dept = MagicMock(); dept.id = uuid.uuid4()
    winner_id = uuid.uuid4()
    winner = MagicMock(); winner.id = winner_id

    repo = MagicMock()
    repo.get_department = AsyncMock(return_value=dept)
    repo.get_team_by_dept_and_name = AsyncMock(side_effect=[None, winner])
    repo.create_team = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("uq_teams_dept_name"))
    )

    team = await svc._get_or_create_team(
        repo, _nested_session(), dept_name=None, team_name="T1"
    )
    assert team.id == winner_id


@pytest.mark.asyncio
async def test_team_create_race_reraises_when_still_missing():
    """재조회도 None 이면(승자 롤백 등) IntegrityError 를 그대로 전파한다."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    svc = OIDCService.__new__(OIDCService)
    dept = MagicMock(); dept.id = uuid.uuid4()
    repo = MagicMock()
    # dept 를 직접 주입할 경로가 없으므로 dept_name=None 은 피하고
    # _get_or_create_department 를 우회하는 대신 dept_name=None 분기를 mock 한다.
    with _pytest.raises(IntegrityError):
        # dept_name=None 경로도 get_department 를 타므로 repo 에 dept 를 준다
        repo.get_department = AsyncMock(return_value=dept)
        repo.get_team_by_dept_and_name = AsyncMock(return_value=None)
        repo.create_team = AsyncMock(
            side_effect=IntegrityError("insert", {}, Exception("uq"))
        )
        await svc._get_or_create_team(
            repo, _nested_session(), dept_name=None, team_name="T1"
        )
