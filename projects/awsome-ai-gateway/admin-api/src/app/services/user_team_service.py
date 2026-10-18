# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import (
    CacheInvalidationManager,
    invalidate_after_commit,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import Department, Team, User, UserAllowedClient, UserRole
from app.models.budget import BudgetScope
from app.models.model import RateLimitScope, UserAllowedModel
from app.repositories._cursor import decode_cursor
from app.repositories.budget_repository import BudgetRepository
from app.repositories.model_repository import RateLimitConfigRepository
from app.repositories.user_repository import UserRepository
from app.schemas.users import (
    DepartmentResponse,
    OrgNodeMeta,
    OrgTreeNode,
    TeamListItem,
    TeamResponse,
    UserResponse,
    UserSearchItem,
)
from app.services.key_service import KeyService

logger = structlog.get_logger()


def _repoint_leader(team: Team, *, exclude_user_id: uuid.UUID) -> None:
    """leader_user_id 를 남은 TEAM_LEADER 멤버 중 한 명으로 옮기고, 없으면 비운다."""
    remaining = next(
        (
            m for m in team.members
            if m.id != exclude_user_id and m.role == UserRole.TEAM_LEADER and m.is_active
        ),
        None,
    )
    team.leader_user_id = remaining.id if remaining else None


async def release_stale_leader_pointer(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None,
    role: UserRole,
    is_active: bool = True,
) -> int:
    """``leader_user_id`` 가 ``user_id`` 를 가리키는 팀 중, 더 이상 유효하지 않은
    포인터를 정리한다 — ``(team_id, role, is_active)`` 는 호출자가 방금 확정한
    새 상태다.

    Cognito sync·OIDC 로그인이 팀/역할을 바꾸거나 비활성화하는 경로는
    ``transfer_user`` 를 거치지 않아 옛 팀의 표시용 포인터가 스테일로 남았다 —
    탈퇴한 사람이 팀 리더로 표시되고(team_leader 알림의 폴백 근거),
    unset_team_leader 도 ValidationError 로 못 지운다. 같은 팀의 활성
    TEAM_LEADER 를 가리키는 유효한 포인터는 건너뛰고, 정리한 팀 수를 돌려준다.
    """
    # SAVEPOINT 로 이 함수의 SELECT·포인터 UPDATE 를 격리한다 — 이 안의 문장이
    # 실패하면 savepoint 만 롤백되고 외부 트랜잭션은 산다. 단, 호출자의 보류
    # 변경은 SAVEPOINT 생성 시점(begin_nested 내부 flush)에 먼저 나간다 — 그
    # flush 실패는 savepoint 이전이라 격리할 수 없지만, 그 변경은 호출자 소유라
    # 어차피 호출자의 flush/commit 에서 실패할 운명이었다.
    async with session.begin_nested():
        teams = (
            await session.execute(select(Team).where(Team.leader_user_id == user_id))
        ).scalars().all()
        fixed = 0
        for team in teams:
            if team.id == team_id and role == UserRole.TEAM_LEADER and is_active:
                continue  # 아직 유효한 포인터
            _repoint_leader(team, exclude_user_id=user_id)
            fixed += 1
        return fixed


async def repoint_inactive_leader_pointers(session: AsyncSession) -> int:
    """비활성 사용자를 가리키는 ``leader_user_id`` 를 일괄 정리한다.

    bulk 비활성화(``deactivate_missing_oidc_users``)는 ORM 을 거치지 않는 단일
    UPDATE 라 사용자별 ``release_stale_leader_pointer`` 를 못 돌린다 — 이
    헬퍼로 포인터가 가리키는 사람이 비활성인 팀만 골라 한 번에 재지정한다.
    정리한 팀 수를 돌려준다.
    """
    # begin_nested — 호출자 트랜잭션에 pending 변경이 있으면 SELECT 의 autoflush
    # 실패가 호출 트랜잭션까지 좀비로 만든다. release_stale_leader_pointer 와
    # 같은 이유로 savepoint 안에서 돌린다.
    async with session.begin_nested():
        # populate_existing — bulk UPDATE(synchronize_session=False)가 identity map 의
        # 로드된 User 를 갱신하지 않아, 방금 비활성화된 리더가 is_active=True 로 남아
        # 재지정 후보로 뽑히는 걸 막는다.
        teams = (
            await session.execute(
                select(Team)
                .options(selectinload(Team.members))
                .where(
                    Team.leader_user_id.in_(
                        select(User.id).where(User.is_active.is_(False))
                    )
                )
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for team in teams:
            _repoint_leader(team, exclude_user_id=team.leader_user_id)
    return len(teams)


async def collect_team_transfer_side_effects(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    old_team_id: uuid.UUID | None,
    new_team_id: uuid.UUID,
    cache_mgr: CacheInvalidationManager | None,
    key_service: KeyService | None,
    errors: list[str] | None = None,
) -> tuple[list[str], list[str], list]:
    """팀 이관 공통 부수효과 (R16 — transfer_user·cognito sync 공용).

    DB 측: 개인 예산(BR-BUD-04 — 예산은 팀 속성이라 유저를 따라가지 않는다),
    하위 app 예산, USER 스코프 레이트리밋 설정을 비활성화한다. 사용량 카운터는
    유저 귀속으로 유지해 팀 이동으로 한도를 초기화하는 우회를 막는다.

    반환값: ``(keys, patterns, ops)`` — 전부 **commit 후** 적용할 무효화 자산.
    호출부는 ``invalidate_after_commit`` 또는 SyncResult.pending_cache_* 로
    commit 이후에 적용해야 한다 — commit 전 DEL 은 미커밋 구정책 재캐시 창을
    연다(R4-A2-3).

    ``errors`` 가 주어지면 VK 해시 수집 실패는 거기 기록하고 이관은 계속한다
    (cognito sync 의 최선노력 정책); ``None`` 이면 그대로 전파(transfer_user —
    대화형 작업은 조용한 부분실패보다 500 이 낫다).
    """
    budget_repo = BudgetRepository(session)
    removed_app_clients = await budget_repo.list_active_app_clients(user_id)
    await budget_repo.deactivate_configs(BudgetScope.USER, user_id)
    await budget_repo.deactivate_app_configs_for_user(user_id)
    await RateLimitConfigRepository(session).deactivate_configs(
        RateLimitScope.USER, user_id
    )

    vk_hashes: list[str] = []
    if key_service is not None:
        try:
            vk_hashes = await key_service.list_active_vk_hashes_for_user(
                session, user_id
            )
        except Exception as e:
            if errors is None:
                raise
            errors.append(f"VK hash collection on transfer failed: {e}")

    keys: list[str] = [
        f"user_context:{user_id}",
        f"budget:config:user:{{{user_id}}}",
        *[
            f"budget:config:user:{{{user_id}}}:{c}"
            for c in removed_app_clients
        ],
        *[f"key:cache:vk:{h}" for h in vk_hashes],
    ]
    patterns: list[str] = []
    ops: list = []
    if cache_mgr is not None:
        # 게이트웨이의 USER 스코프 RL 설정 캐시(rl:config:USER:{uid}:*, 300s TTL)
        # 도 함께 지운다 — DB 만 비활성화하면 구 한도가 최대 5분 더 적용된다.
        patterns.append(f"rl:config:USER:{user_id}:*")
        if old_team_id is not None and vk_hashes:
            async def _swap() -> None:
                # 실패 기록(cache_invalidation_failures)을 위해 전용 세션 사용 —
                # 요청 세션은 지연 실행 시점에 이미 닫혀 있다.
                from app.core.db import AsyncSessionLocal

                async with AsyncSessionLocal() as s3:
                    try:
                        await cache_mgr.swap_reverse_index_membership(
                            old_key=f"team:vk_hashes:{old_team_id}",
                            new_key=f"team:vk_hashes:{new_team_id}",
                            members=vk_hashes,
                            session=s3,
                        )
                    except Exception:
                        # swap 이 실패하면 실패행이 session.add 된다 — 그대로
                        # 재전파하면 `async with` 롤백이 기록까지 지워 retry_failed
                        # 의 대상에서 영원히 빠진다. 기록을 커밋한 뒤 재전파(R16 V3-S2).
                        try:
                            await s3.commit()
                        except Exception:
                            logger.warning(
                                "user_team.swap_failure_persist_failed",
                                exc_info=True,
                            )
                        raise
                    await s3.commit()

            ops.append(_swap)
    return keys, patterns, ops


class UserTeamService:
    def __init__(self, cache_mgr: CacheInvalidationManager, key_service: KeyService) -> None:
        self._cache_mgr = cache_mgr
        self._key_service = key_service

    async def create_department(
        self,
        session: AsyncSession,
        *,
        name: str,
        org_id: uuid.UUID | None = None,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> DepartmentResponse:
        repo = UserRepository(session)

        # Default to first org if not specified
        if org_id is None:
            org = await repo.get_default_org()
            if org is None:
                raise NotFoundError("Organization", "default")
            org_id = org.id

        dept = Department(
            id=uuid.uuid4(),
            org_id=org_id,
            name=name,
        )
        await repo.create_department(dept)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="CREATE_DEPARTMENT",
            resource_type="Department",
            resource_id=str(dept.id),
            changes={"after": {"name": name}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return DepartmentResponse(
            id=str(dept.id),
            name=dept.name,
            org_id=str(dept.org_id),
            created_at=dept.created_at,
        )

    async def create_team(
        self,
        session: AsyncSession,
        *,
        name: str,
        department_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> TeamResponse:
        repo = UserRepository(session)
        dept = await repo.get_department(department_id)
        if dept is None:
            raise NotFoundError("Department", str(department_id))

        team = Team(
            id=uuid.uuid4(),
            dept_id=department_id,
            name=name,
        )
        await repo.create_team(team)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="CREATE_TEAM",
            resource_type="Team",
            resource_id=str(team.id),
            changes={"after": {"name": name, "department_id": str(department_id)}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return TeamResponse(
            id=str(team.id),
            name=team.name,
            department_id=str(team.dept_id),
            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
            created_at=team.created_at,
        )

    async def set_team_leader(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> TeamResponse:
        """팀원 한 명을 팀 리더로 지정한다.

        팀 하나에 여러 명이 리더일 수 있다 — 이미 리더인 다른 사람을 내리지 않는다
        (role=TEAM_LEADER 는 팀당 배타적 단일값이 아니라 팀원 각자의 속성).
        ``Team.leader_user_id`` 는 "가장 최근에 지정된 리더"를 가리키는 표시용
        포인터일 뿐, 실제 권한(``require_team_leader_of`` 등)은 각 사용자의
        ``role``+``team_id`` 로 판정되므로 이 값과 무관하게 정상 동작한다.
        """
        repo = UserRepository(session)

        team = await repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        target_user = await repo.get_user(user_id)
        if target_user is None:
            raise NotFoundError("User", str(user_id))
        if target_user.team_id != team_id:
            raise ValidationError(
                f"User {user_id} does not belong to team {team_id} — transfer them first."
            )

        if target_user.role == UserRole.ADMIN:
            raise ValidationError("Admin users cannot be set as a team leader.")
        updated = await repo.update_user_role(user_id, UserRole.TEAM_LEADER)
        if updated is None:
            # get_user 와 update 사이에 삭제된 경우 — 포인터가 없는 행을 가리키는
            # FK 위반(500) 대신 404 로 끝낸다.
            raise NotFoundError("User", str(user_id))
        team.leader_user_id = user_id

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="SET_TEAM_LEADER",
            resource_type="Team",
            resource_id=str(team_id),
            changes={"after": {"leader_user_id": str(user_id)}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return TeamResponse(
            id=str(team.id),
            name=team.name,
            department_id=str(team.dept_id),
            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
            created_at=team.created_at,
        )

    async def unset_team_leader(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> TeamResponse:
        """팀 리더 지정 해제(한 명) — 그 사람의 role 만 DEVELOPER 로 되돌린다.

        팀에 리더가 여러 명일 수 있으므로 어느 사용자를 해제할지 반드시 지정해야
        한다. ``Team.leader_user_id`` (표시용 포인터)가 이 사람을 가리키고 있었다면
        남은 리더 중 한 명으로 옮기고, 아무도 없으면 비운다.

        ADMIN 은 영향받지 않는다(_derive_role 이 ADMIN_GROUPS 로 별도 판정) — 여기서
        DEVELOPER 로 되돌려도 그 사람이 ClaudeAdmin 이면 다음 Cognito 로그인 때 다시
        ADMIN 으로 복원된다.
        """
        repo = UserRepository(session)

        team = await repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        target_user = await repo.get_user(user_id)
        if target_user is None:
            raise NotFoundError("Team leader", str(user_id))
        if target_user.team_id != team_id or target_user.role != UserRole.TEAM_LEADER:
            # 스테일 포인터 정리 경로 — 포인터가 이 사람을 가리키는데 더 이상 이
            # 팀의 유효한 리더가 아니면(팀 이동·Cognito sync 강등·직접 수정 등)
            # 오류 대신 포인터만 다시 맞춘다. 이 경로가 없으면 깨진 포인터를
            # 되돌릴 API 가 없다.
            if team.leader_user_id == user_id:
                before_pointer = team.leader_user_id
                _repoint_leader(team, exclude_user_id=user_id)
                # 리더십 변경인데 감사 행이 없던 결함 — 스테일 포인터 정리도
                # 누가 언제 바꿨는지 남아야 한다(R21 N8).
                await audit_logger.log(
                    session,
                    actor_user_id=actor.user_id,
                    actor_role=actor.role.value,
                    service_token_id=getattr(actor, "service_token_id", None),
                    action="UNSET_TEAM_LEADER",
                    resource_type="Team",
                    resource_id=str(team_id),
                    changes={
                        "stale_pointer_fix": True,
                        "before": {"leader_user_id": str(before_pointer) if before_pointer else None},
                        "after": {
                            "leader_user_id": str(team.leader_user_id) if team.leader_user_id else None
                        },
                    },
                    ip_address=ip_address,
                    request_id=request_id,
                )
                return TeamResponse(
                    id=str(team.id),
                    name=team.name,
                    department_id=str(team.dept_id),
                    leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
                    created_at=team.created_at,
                )
            if target_user.team_id != team_id:
                raise NotFoundError("Team leader", str(user_id))
            raise ValidationError(f"User {user_id} is not currently a team leader of this team.")

        await repo.update_user_role(user_id, UserRole.DEVELOPER)

        if team.leader_user_id == user_id:
            _repoint_leader(team, exclude_user_id=user_id)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="UNSET_TEAM_LEADER",
            resource_type="Team",
            resource_id=str(team_id),
            changes={
                "before": {"leader_user_id": str(user_id)},
                "after": {"leader_user_id": str(team.leader_user_id) if team.leader_user_id else None},
            },
            ip_address=ip_address,
            request_id=request_id,
        )

        return TeamResponse(
            id=str(team.id),
            name=team.name,
            department_id=str(team.dept_id),
            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
            created_at=team.created_at,
        )

    async def transfer_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        new_team_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
        team_source: str = "manual",
        from_sync: bool = False,
    ) -> UserResponse:
        """``team_source`` — 'manual'(관리자 이관, 기본)이면 이후 OIDC 로그인·
        Cognito sync 가 그룹-유도 팀으로 되돌리지 않는다. 'sync' 는 동기화
        경로가 부르는 값(또 다시 덮일 수 있다)이고, 관리자 API 가 'sync' 를
        명시하면 핀 해제(동기화 관리 복귀)다.

        ``from_sync`` — True(OIDC/Cognito 동기화 경로)이면 'manual' 사용자를
        되돌리지 않는다. 관리자 API 호출은 False 로 둬야 한다 — 값이 'sync' 인
        요청이 이 가드에 걸리면 관리자의 핀 해제가 200 조용히 무시된다(R21 r4)."""
        repo = UserRepository(session)

        user = await repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if isinstance(session, AsyncSession):
            # FOR UPDATE + populate_existing — OIDC 로그인/sync 와 관리자 이관이
            # 같은 행을 동시에 쓰면 아이덴티티맵의 스테일 객체 기준으로 team_id /
            # team_source 가 갱신돼 어느 한쪽 쓰기가 유실된다(R21 r3 S2).
            # populate_existing 은 미플러시 변경을 덮어쓰므로(OIDC 경로가
            # _upsert_user 의 갱신을 달고 들어온다) 잠금 전에 flush 해 보존한다.
            # 테스트 더블 세션(MagicMock)은 단일 실행이라 이 잠금을 건너뛴다.
            await session.flush()
            user = await session.get(
                User, user_id, with_for_update=True, populate_existing=True
            )
            if user is None:
                raise NotFoundError("User", str(user_id))
        old_team_id = user.team_id

        # 잠금 획득 뒤의 수동-배정 재확인 — 우리가 잠그기 전에 커밋된 수동
        # 배정(admin-ui)이 있으면 sync 호출은 그 배정을 되돌리지 않는다.
        # ⚠️ from_sync 로 caller 를 구분한다 — 관리자 API 가 명시한
        #    team_source='sync'(핀 해제)가 이 가드에 걸리면 안 된다.
        if from_sync and user.team_source == "manual":
            return self._to_user_response(user)

        # 같은 팀 재저장은 이관 부수효과(collect_team_transfer_side_effects)를
        # 발화하지 않는다 — 개별 예산/USER rate-limit 비활성화가 재저장만으로
        # 사용자 제한을 초기화하던 결함(M4). 단 team_source 가 실제로 바뀌면
        # (수동 고정 또는 핀 해제) 그 변경만 반영하고 감사한다 — 조용한 핀은
        # 이후 sync 동작을 바꾸므로 감사가 필요하다(R21 r2 S-3, r3 N2).
        if new_team_id == old_team_id:
            if user.team_source != team_source:
                prev_source = user.team_source
                user.team_source = team_source
                await audit_logger.log(
                    session,
                    actor_user_id=actor.user_id,
                    actor_role=actor.role.value,
                    service_token_id=getattr(actor, "service_token_id", None),
                    action="TRANSFER_USER",
                    resource_type="User",
                    resource_id=str(user_id),
                    changes={
                        "before": {
                            "team_id": str(old_team_id) if old_team_id else None,
                            "team_source": prev_source,
                        },
                        "after": {
                            "team_id": str(new_team_id),
                            "team_source": team_source,
                        },
                        "note": "same-team: team_source only",
                    },
                    ip_address=ip_address,
                    request_id=request_id,
                )
            return self._to_user_response(user)
        # 관리자가 team 을 바꾸면서 'sync' 를 명시하는 건 거부한다 — 다음
        # OIDC 로그인에서 그룹-유도 팀으로 다시 옮겨져 이관 부수효과(예산/RL
        # 비활성화)가 두 번 발화한다. 'sync' 요청은 같은 팀의 핀 해제(위의
        # no-op 분기)에만 유효하다(R21 r5 N7).
        if team_source == "sync" and not from_sync:
            raise ValidationError(
                "team_source='sync' unpin은 같은 팀 요청에만 유효합니다 — "
                "cross-team 이관은 'manual' 을 사용하세요"
            )
        # 존재하지 않는 team_id 는 FK 위반 500 이라 먼저 확인해 404 로 돌린다.
        new_team = await repo.get_team(new_team_id)
        if new_team is None:
            raise NotFoundError("Team", str(new_team_id))

        # 팀 리더십은 팀별 속성이라 이관되지 않는다 — 그대로 두면 옛 팀의 leader_user_id
        # 가 이제 그 팀 소속도 아닌 사람을 계속 가리키고(스테일 포인터), 동시에
        # require_team_leader_of(role+team_id 로만 판정)가 새 팀에서 이 사람을
        # 명시적 지정 없이 리더로 인가해버리는 의도치 않은 권한 상승이 발생한다.
        # 호출 경로는 OIDC 로그인 시 팀 동기화와 관리자 이관 API 뿐이고, Cognito sync
        # 는 같은 정책을 cognito_sync_service._effective_role 에서 별도로 적용한다.
        # 같은 팀으로의 no-op 호출(new_team_id == old_team_id)까지 강등시키지 않도록
        # 실제로 팀이 바뀌는 경우에만 적용한다.
        if user.role == UserRole.TEAM_LEADER and new_team_id != old_team_id:
            await repo.update_user_role(user_id, UserRole.DEVELOPER)
            if old_team_id is not None:
                old_team = await repo.get_team(old_team_id)
                if old_team is not None and old_team.leader_user_id == user_id:
                    _repoint_leader(old_team, exclude_user_id=user_id)

        # BR-BUD-04 + D-15/I-3: 팀 이동 부수효과 — 개별 예산/USER RL 비활성화 +
        # user_context·예산·VK·rl:config 캐시 + team:vk_hashes 인덱스 스왑.
        # cognito_sync_service 와 같은 공통 헬퍼로 모은다(R16).
        cache_keys, patterns, ops = await collect_team_transfer_side_effects(
            session,
            user_id=user_id,
            old_team_id=old_team_id,
            new_team_id=new_team_id,
            cache_mgr=self._cache_mgr,
            key_service=self._key_service,
        )

        # Transfer team — 출처 표지도 함께 갱신한다(migration 0048).
        user = await repo.update_user_team(user_id, new_team_id)
        if user is not None:
            user.team_source = team_source

        # R4-A2-3: DEL/swap 은 commit 후로 지연 — CommittingRoute 가 핸들러
        # 반환 후 commit 하므로 여기서 즉시 DEL 하면 DEL→commit 창에 게이트웨이가
        # 미커밋 구 정책을 재캐시한다(R3-8 부류).
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            cache_keys,
            pattern=patterns[0] if patterns else None,
            extra=ops,
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="TRANSFER_USER",
            resource_type="User",
            resource_id=str(user_id),
            changes={
                "before": {"team_id": str(old_team_id) if old_team_id else None},
                "after": {"team_id": str(new_team_id)},
            },
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_user_response(user)

    async def list_users(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        email: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[UserResponse], bool]:
        repo = UserRepository(session)
        # 복합 커서(created_at|id) 우선, 구 형식(bare UUID)은 레거시 조건으로 폴백.
        decoded = decode_cursor(cursor) if cursor else None
        legacy_uuid = None
        if cursor and decoded is None:
            try:
                legacy_uuid = uuid.UUID(cursor)
            except ValueError:
                raise ValidationError(f"invalid cursor: {cursor!r}")
        users = await repo.list_users(
            team_id=team_id,
            department_id=department_id,
            is_active=is_active,
            email=email,
            cursor=decoded,
            legacy_id_cursor=legacy_uuid,
            limit=limit + 1,
        )
        has_more = len(users) > limit
        if has_more:
            users = users[:limit]
        return [self._to_user_response(u) for u in users], has_more

    async def list_teams(self, session: AsyncSession) -> list[TeamListItem]:
        repo = UserRepository(session)
        teams = await repo.list_all_teams()
        return [
            TeamListItem(
                id=str(t.id),
                name=t.name,
                department_id=str(t.dept_id),
                department_name=t.department.name if t.department else None,
                leader_user_id=str(t.leader_user_id) if t.leader_user_id else None,
                member_count=len([m for m in t.members if m.is_active]),
            )
            for t in teams
        ]

    # 검색 결과 상한. 드롭다운에 보여줄 수 있는 양이고, 타이핑마다 호출되는 경로라
    # 의도적으로 작게 유지한다. 초과분은 truncated=True 로 UI 에 알린다.
    SEARCH_LIMIT = 20
    # 최소 검색어 길이. 1자 검색은 사실상 전량 매칭이라 무의미하고 비싸다.
    SEARCH_MIN_LEN = 2

    async def search_users(
        self,
        session: AsyncSession,
        *,
        term: str,
        limit: int | None = None,
    ) -> tuple[list[UserSearchItem], bool]:
        """이메일/이름 부분 일치 사용자 검색. ``(items, truncated)`` 반환.

        검색어가 ``SEARCH_MIN_LEN`` 미만이면 **DB 를 치지 않고** 빈 결과를 준다.

        비활성 사용자는 제외한다 — 조직 트리도 비활성을 빼고 만들기 때문에
        (get_org_tree 의 ``active_members``), 검색에서 나온 사람을 선택했더니 트리에
        없다는 상태가 생기면 UI 가 조상 경로를 펼칠 대상을 찾지 못한다.
        """
        stripped = term.strip()
        if len(stripped) < self.SEARCH_MIN_LEN:
            return [], False

        effective = limit if limit is not None else self.SEARCH_LIMIT
        repo = UserRepository(session)
        # limit+1 로 조회해 잘림 여부를 판정한다 — 별도 COUNT 쿼리를 피한다.
        rows = await repo.search_users(term=stripped, is_active=True, limit=effective + 1)
        truncated = len(rows) > effective
        if truncated:
            rows = rows[:effective]
        return [
            UserSearchItem(
                id=str(user_id),
                email=email,
                display_name=display_name,
                role=role,
                team_id=str(team_id) if team_id else None,
                team_name=team_name,
            )
            for user_id, email, display_name, role, team_id, team_name in rows
        ], truncated

    async def get_org_tree(
        self, session: AsyncSession, include_empty: bool = False
    ) -> OrgTreeNode | None:
        """조직 트리. ``include_empty`` 가 True 면 활성 멤버 0인 팀도 포함한다 —
        신규 Cognito 팀에 멤버 배정 전 정책을 미리 설정할 진입점이 필요하기 때문.
        팀이 하나도 없는 부서는 include_empty 에서도 제외한다(노이즈)."""
        repo = UserRepository(session)
        orgs = await repo.list_all_orgs()
        if not orgs:
            return None
        org = orgs[0]

        # 개별 정책(앱/모델 override)을 가진 사용자 id — 트리 노드별 점 표시용.
        # 멤버당 N+1 조회 대신 DISTINCT union 1방으로 모은다(테이블이 작다).
        custom_user_ids: set[uuid.UUID] = set(
            (
                await session.execute(
                    select(UserAllowedClient.user_id).union(
                        select(UserAllowedModel.user_id)
                    )
                )
            ).scalars()
        )

        dept_nodes: list[OrgTreeNode] = []
        for dept in org.departments:
            team_nodes: list[OrgTreeNode] = []
            for team in dept.teams:
                leader = next(
                    (m for m in team.members if team.leader_user_id and m.id == team.leader_user_id),
                    None,
                )
                active_members = [m for m in team.members if m.is_active]
                if not active_members and not include_empty:
                    continue
                member_nodes: list[OrgTreeNode] = []
                for member in active_members:
                    member_nodes.append(
                        OrgTreeNode(
                            id=str(member.id),
                            name=member.display_name,
                            type="USER",
                            children=[],
                            meta=OrgNodeMeta(
                                member_count=None,
                                team_count=None,
                                leader_name=None,
                                email=member.email,
                                role=member.role.value,
                                team_name=team.name,
                                has_custom_policies=member.id in custom_user_ids,
                            ),
                        )
                    )
                team_nodes.append(
                    OrgTreeNode(
                        id=str(team.id),
                        name=team.name,
                        type="TEAM",
                        children=member_nodes,
                        meta=OrgNodeMeta(
                            member_count=len(active_members),
                            # TEAM 은 팀이 아니라 사람을 담는다 — 팀 수는 의미가 없다.
                            team_count=None,
                            leader_name=leader.display_name if leader else None,
                            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
                            email=leader.email if leader else None,
                            role=None,
                            team_name=None,
                            custom_policy_count=sum(
                                1 for m in active_members if m.id in custom_user_ids
                            ),
                        ),
                    )
                )
            if not team_nodes:
                continue
            dept_nodes.append(
                OrgTreeNode(
                    id=str(dept.id),
                    name=dept.name,
                    type="DEPARTMENT",
                    children=team_nodes,
                    meta=OrgNodeMeta(
                        # 하위 팀들의 **활성 사용자** 합. 팀 수가 아니다(team_count 참조).
                        member_count=sum(len(n.children) for n in team_nodes),
                        team_count=len(team_nodes),
                        leader_name=None,
                        email=None,
                        role=None,
                        team_name=None,
                    ),
                )
            )

        return OrgTreeNode(
            id=str(org.id),
            name=org.name,
            type="ORGANIZATION",
            children=dept_nodes,
            meta=OrgNodeMeta(
                # ⚠️ 예전엔 `sum(len(t.members) ...)` 였다 — ORM 의 **전체** 멤버라
                #    비활성 사용자까지 셌다. 반면 팀/부서는 active_members 만 센다.
                #    그래서 조직 합계가 부서 합계의 총합과 달라, 어느 쪽이 맞는지 화면에서
                #    판단할 수 없었다. 이미 만들어 둔 노드에서 세면 계층이 일관된다.
                member_count=sum(
                    len(team.children) for dept in dept_nodes for team in dept.children
                ),
                team_count=sum(len(dept.children) for dept in dept_nodes),
                leader_name=None,
                email=None,
                role=None,
                team_name=None,
            ),
        )

    @staticmethod
    def _to_user_response(user: User) -> UserResponse:
        return UserResponse(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            team_id=str(user.team_id) if user.team_id else None,
            team_name=user.team.name if user.team else None,
            is_active=user.is_active,
            created_at=user.created_at,
        )
