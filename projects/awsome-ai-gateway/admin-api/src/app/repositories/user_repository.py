# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from sqlalchemy import String, all_, any_, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from app.models.auth import Department, Organization, Team, User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Organization ──

    async def get_default_org(self) -> Organization | None:
        stmt = select(Organization).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_first_org_id(self) -> uuid.UUID | None:
        """org id 만 단건 조회(멤버·부서 그래프 미로드). sync_all 의 팀 캐시가
        신규 부서 생성 시 사용할 org 를 1회 확보하기 위한 경량 경로 —
        ``list_all_orgs`` (부서→팀→멤버 전량 selectinload) 의 OOM 을 회피한다."""
        stmt = select(Organization.id).order_by(Organization.name).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Department ──

    async def create_department(self, dept: Department) -> Department:
        self._session.add(dept)
        await self._session.flush()
        return dept

    async def get_department(self, dept_id: uuid.UUID) -> Department | None:
        return await self._session.get(Department, dept_id)

    async def get_department_by_name(
        self, org_id: uuid.UUID, name: str
    ) -> Department | None:
        """동일 org 내에서 이름으로 부서 단건 조회.

        OIDC 그룹 매핑 hot path 에서 사용. ``list_all_orgs`` (모든 org + 부서 +
        팀 + 멤버 selectinload) 의 대체 경로로 도입.
        """
        stmt = (
            select(Department)
            .where(Department.org_id == org_id, Department.name == name)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Team ──

    async def create_team(self, team: Team) -> Team:
        self._session.add(team)
        await self._session.flush()
        return team

    async def get_team(self, team_id: uuid.UUID) -> Team | None:
        return await self._session.get(Team, team_id)

    async def get_team_by_dept_and_name(
        self, dept_id: uuid.UUID, name: str
    ) -> Team | None:
        """(dept_id, name) 으로 팀 단건 조회.

        OIDC 그룹 매핑 hot path 에서 사용. ``list_all_teams`` (selectinload 로
        전 팀 + 멤버 fetch) 의 O(N) 경로를 인덱스 기반 단건 조회로 대체.
        """
        stmt = (
            select(Team)
            .where(Team.dept_id == dept_id, Team.name == name)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_leader(self, team_id: uuid.UUID, user_id: uuid.UUID) -> Team | None:
        team = await self.get_team(team_id)
        if team is None:
            return None
        team.leader_user_id = user_id
        return team

    # ── User ──

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_sso_subject(self, sso_subject: str) -> User | None:
        stmt = select(User).where(User.sso_subject == sso_subject)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def flush(self) -> None:
        """Flush pending ORM changes so a failing write surfaces here (e.g. a
        UNIQUE email collision) instead of being deferred to a later commit /
        savepoint release. Lets callers count/act only after a successful flush."""
        await self._session.flush()

    async def update_user_team(self, user_id: uuid.UUID, team_id: uuid.UUID) -> User | None:
        user = await self.get_user(user_id)
        if user is None:
            return None
        user.team_id = team_id
        return user

    async def update_user_role(self, user_id: uuid.UUID, role: UserRole) -> User | None:
        user = await self.get_user(user_id)
        if user is None:
            return None
        user.role = role
        return user

    async def list_all_orgs(self) -> list[Organization]:
        stmt = (
            select(Organization)
            .options(
                selectinload(Organization.departments)
                .selectinload(Department.teams)
                .selectinload(Team.members)
            )
            .order_by(Organization.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_all_teams(self) -> list[Team]:
        stmt = (
            select(Team)
            .options(
                selectinload(Team.members),
                selectinload(Team.department),
            )
            .order_by(Team.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_teams_lite(self) -> list[Team]:
        """멤버 미포함 경량 팀 조회. sync_all 의 팀 캐시 구축용 —
        list_all_teams(members selectinload)의 반복 호출로 인한 메모리 폭발을 회피.

        Team.members/department 는 모델 레벨에서 ``lazy="selectin"`` 이므로,
        ``lazyload`` 로 명시적으로 override 하지 않으면 plain select 여도 여전히
        멤버 그래프를 eager-load 한다. (id, dept_id, name 컬럼만 필요.)"""
        stmt = (
            select(Team)
            .options(lazyload(Team.members), lazyload(Team.department))
            .order_by(Team.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_departments_lite(self) -> list[Department]:
        """멤버·팀 미포함 경량 부서 조회. sync_all 의 부서 캐시 구축용.

        Department.teams/organization 도 ``lazy="selectin"`` 기본값이라 lazyload
        override 로 eager-load 를 차단한다. (id, name 컬럼만 필요.)"""
        stmt = (
            select(Department)
            .options(lazyload(Department.teams), lazyload(Department.organization))
            .order_by(Department.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def prefetch_users_by_subjects(self, subs: set[str]) -> dict[str, dict]:
        """sso_subject 집합에 해당하는 기존 유저의 스칼라 스냅샷을 일괄 조회.

        sync_all 의 유저당 get_by_sso_subject(N+1) 를 1~수 회 배열-바인딩 조회로
        대체한다. ORM 엔티티를 만들지 않고 필요한 컬럼만 SELECT 하여 gate 판정용
        dict 를 반환한다({sso_subject: {id,email,display_name,team_id,role,is_active}}).
        subs 가 크면(수만) 청크로 나눠 조회한다(과대 배열 파라미터 회피).

        포함 매칭이므로 `= ANY(:subs::VARCHAR[])` 를 쓴다(deactivate 의 `!= ALL`
        none-of 와 반대). 배열 단일 파라미터라 요소당 스칼라 바인드 폭발이 없다.
        """
        result: dict[str, dict] = {}
        if not subs:
            return result
        subs_list = list(subs)
        CHUNK = 5000
        for i in range(0, len(subs_list), CHUNK):
            chunk = subs_list[i : i + CHUNK]
            stmt = select(
                User.sso_subject, User.id, User.email, User.display_name,
                User.team_id, User.role, User.is_active,
            ).where(
                User.sso_subject == any_(
                    bindparam("subs", value=chunk, type_=ARRAY(String))
                )
            )
            rows = await self._session.execute(stmt)
            for r in rows:
                result[r.sso_subject] = {
                    "id": r.id, "email": r.email, "display_name": r.display_name,
                    "team_id": r.team_id, "role": r.role, "is_active": r.is_active,
                }
        return result

    async def deactivate_missing_oidc_users(
        self, seen_subjects: set[str], provider: str
    ) -> int:
        """provider 유저 중 seen_subjects 에 없고 현재 활성인 유저를 bulk 비활성화.

        sync_all reconcile 전용. ORM 객체를 로드하지 않고 단일 UPDATE 로 처리해
        1만+ 유저 로드 시의 메모리 폭발을 회피한다. 변경 행 수를 반환한다.
        """
        if seen_subjects:
            subject_filter = User.sso_subject != all_(
                bindparam("seen", value=list(seen_subjects), type_=ARRAY(String))
            )
        else:
            subject_filter = User.sso_subject.isnot(None)
        stmt = (
            update(User)
            .where(User.provider == provider)
            .where(User.is_active.is_(True))
            .where(subject_filter)
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def iter_all_users(self) -> list[User]:
        """예산 요약처럼 **전수**가 필요한 집계용 — limit 없이 모든 사용자를 돌려준다.

        ⚠️ `list_users(limit=N)` 을 크게 잡아 쓰지 말 것. 그건 `created_at desc` 로 정렬한
        뒤 앞에서 N 개만 자르므로, **N 번째 이후 사용자가 조용히 사라진다.** 실제로 예산
        요약이 `list_users(limit=500)` 이라 가입이 오래된 사용자의 예산 행이 통째로 빠진
        채 사용률이 계산됐다 — 화면에 오류 없이 틀린 비율이 떴다.

        ⚠️ `cursor` 페이징으로 우회하는 것도 안 된다. `list_users` 는
        `order_by(created_at desc)` 인데 커서 조건이 `User.id < cursor` 여서 **정렬 키와
        커서 키가 다르다.** 그 조합은 행을 건너뛰거나 같은 페이지를 반복한다(id 순서와
        created_at 순서가 무관하므로). 커서 페이징을 쓰려면 정렬 키로 커서를 잡아야 한다.

        전수 로드가 안전한 근거: 이 메서드는 관리자 화면의 요약 집계에서만 쓰이고,
        auth.users 는 조직 구성원 수(수천 규모) 상한이라 목록 자체가 크지 않다. 사용자가
        수십만 규모가 되면 이 집계를 SQL 쪽 GROUP BY 로 옮겨야 한다 —
        `/admin/dashboard/kpi` 가 이미 그 방식이다(합계만 필요할 때는 그쪽을 쓸 것).
        """
        result = await self._session.execute(
            select(User).order_by(User.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_users(
        self,
        *,
        team_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        email: str | None = None,
        cursor: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[User]:
        stmt = select(User).order_by(User.created_at.desc())
        if team_id:
            stmt = stmt.where(User.team_id == team_id)
        if department_id:
            stmt = stmt.join(Team, User.team_id == Team.id).where(Team.dept_id == department_id)
        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
        if email:
            # email 은 unique 컬럼 → exact 매칭(0/1건). DB 는 email 을 정규화 없이
            # Cognito 값 그대로 저장하므로 대소문자 무시(lower) 비교.
            stmt = stmt.where(func.lower(User.email) == email.lower())
        if cursor:
            stmt = stmt.where(User.id < cursor)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

