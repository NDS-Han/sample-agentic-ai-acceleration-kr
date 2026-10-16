# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC JWT → Virtual Key exchange + 사용자/팀 자동 프로비저닝.

전체 흐름 (POST /v1/auth/exchange):
    1. OIDCVerifier 로 JWT 서명/만료/audience/issuer 검증
    2. claim 추출 (sub, email, name, groups)
    3. (provider, sub) 로 사용자 조회 (없으면 신규 프로비저닝)
    4. groups → team 매핑 (자동 생성 시 budget=$0 INSERT)
    5. 기존 사용자의 team 변경 감지 시 ``UserTeamService.transfer_user`` 호출
       (budget/RL deactivate + VK cache invalidate + reverse-index swap 모두 자동)
    6. role 결정 (ADMIN_EMAILS / ADMIN_GROUPS)
    7. ``KeyService.issue_key`` 로 VK 발급
    8. ``CLIService._cache_for_gateway`` 로 budget/model Redis 캐시 hydrate

이 서비스는 cli_service.verify_sts_and_issue_key 와 **dual-mode 로 공존**합니다.
사용자는 ``users.provider`` 컬럼으로 분리됨 ('sts' vs 'oidc:keycloak' 등).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.oidc_identity import admin_mapping_configured, derive_role
from app.core.oidc_verifier import OIDCConfigError, OIDCVerifier, OIDCVerifyError
from app.models.auth import Department, Team, User, UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.schemas.cli import VirtualKeyIssueResponse
from app.services.cli_service import CLIService
from app.services.key_service import KeyService
from app.services.user_team_service import UserTeamService, release_stale_leader_pointer

logger = structlog.get_logger()


from app.core.oidc_identity import claims_truthy as _truthy


class OIDCAuthError(Exception):
    """JWT 검증 실패 (401)."""


class OIDCNotProvisionableError(Exception):
    """필수 claim 누락 또는 매칭 그룹 없음 등 (403)."""


class OIDCService:
    """JWT → VK exchange. cli_service 와 별개의 진입점."""

    def __init__(
        self,
        verifier: OIDCVerifier,
        key_service: KeyService,
        user_team_service: UserTeamService,
        http_client: httpx.AsyncClient,
        cognito_client=None,
    ) -> None:
        self._verifier = verifier
        self._key_service = key_service
        self._user_team_service = user_team_service
        self._http_client = http_client
        # optional: enrich missing email via Cognito AdminGetUser (best-effort).
        # Cognito access_token 에는 email claim 이 없어 ID token 만 갖던 fallback 을 보완.
        self._cognito_client = cognito_client

    async def _email_attrs_from_cognito(self, sub: str) -> tuple[str | None, bool]:
        """Cognito AdminGetUser 로 (email, email_verified) 조회 (best-effort, never raise).

        이 풀들은 UsernameAttributes=[email] 이므로 내부 Username 이 sub(UUID) 와 동일.
        Cognito 미구성/조회 실패 시 (None, False) 반환 → 호출부가 <sub>@unknown 으로 fallback.

        ``email_verified`` 는 **서버측 풀 속성**이라 토큰 클레임과 달리 사용자가
        쓸 수 없다 — 이메일 기반 계정 연결의 검증 근거로 쓴다.
        """
        settings = get_settings()
        pool = settings.COGNITO_USER_POOL_ID
        if not self._cognito_client or not pool:
            return None, False
        try:
            resp = await asyncio.to_thread(
                self._cognito_client.admin_get_user,
                UserPoolId=pool,
                Username=sub,
            )
            email = verified = None
            for attr in resp.get("UserAttributes", []):
                if attr.get("Name") == "email":
                    email = attr.get("Value")
                elif attr.get("Name") == "email_verified":
                    verified = attr.get("Value")
            return email, _truthy(verified)
        except Exception:
            logger.warning("oidc.email_from_cognito_failed", sub=sub, exc_info=True)
            return None, False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def exchange_jwt_for_vk(
        self,
        session: AsyncSession,
        *,
        redis,
        token: str,
        device_name: str | None,
        sso_session_expires_at=None,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> VirtualKeyIssueResponse:
        """OIDC access JWT 를 VK 로 교환."""
        settings = get_settings()

        # 1. JWT 검증
        try:
            claims = await self._verifier.verify_async(token, self._http_client)
        except OIDCVerifyError as e:
            raise OIDCAuthError(str(e)) from e
        except OIDCConfigError as e:
            # IDP discovery / JWKS unreachable. 503 에 매핑되도록 별도 예외로 던짐.
            raise

        # 2. claim 추출
        sub = claims.get(settings.OIDC_USER_ID_CLAIM)
        email = claims.get(settings.OIDC_EMAIL_CLAIM, "")
        name = claims.get(settings.OIDC_NAME_CLAIM) or email or sub
        groups = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []
        if not isinstance(groups, list):
            groups = [groups]

        if not sub:
            raise OIDCAuthError(f"missing claim: {settings.OIDC_USER_ID_CLAIM}")
        # email_verified 클레임은 "email" **속성**에 대한 검증 — 매핑된 이메일
        # 클레임이 email 이 아니면(예: preferred_username, upn) 이 값으로 그
        # 클레임을 증명할 수 없다.
        email_verified = (
            _truthy(claims.get("email_verified"))
            if settings.OIDC_EMAIL_CLAIM == "email"
            else False
        )
        if not email:
            # email claim 없는 토큰(Cognito access_token 등) → preferred_username 시도.
            email = claims.get("preferred_username") or ""
            if email:
                # preferred_username 은 사용자가 자유 기재하는 미검증 속성 —
                # email_verified 클레임은 **email** 속성에 대한 것이지 이 값을
                # 증명하지 않는다. 다른 계정의 주소를 적어 넣으면 연결 게이트를
                # 통과하므로 미검증으로 고정한다.
                email_verified = False
        if not email:
            # 그래도 없으면 Cognito AdminGetUser 로 enrich (best-effort).
            # 풀 속성은 서버측 값이라 verified 도 함께 신뢰할 수 있다.
            # 실패 시 마지막 수단으로 <sub>@unknown.
            pool_email, pool_verified = await self._email_attrs_from_cognito(str(sub))
            email = pool_email or f"{sub}@unknown"
            email_verified = pool_verified
        elif not email_verified:
            # 이메일 클레임은 있는데 verified 가 보이지 않는다(Cognito access_token
            # 형태) — 풀 속성으로 한 번 보강한다. 단 pool_verified 는 풀의 **email**
            # 속성에 대한 값이라 주소가 일치할 때만 승계한다(위 preferred_username
            # 경로는 일치하지 않아 미검증 유지).
            pool_email, pool_verified = await self._email_attrs_from_cognito(str(sub))
            if pool_email and pool_email == email:
                email_verified = pool_verified

        # 3. 게이팅 (옵션)
        if settings.OIDC_REQUIRED_GROUP and settings.OIDC_REQUIRED_GROUP not in groups:
            raise OIDCNotProvisionableError(
                f"required_group_missing: {settings.OIDC_REQUIRED_GROUP}"
            )

        # 4. 팀 결정 (필요 시 자동 생성)
        team_id = await self._resolve_team(session, groups)

        # 5. Role 결정 — ADMIN_EMAILS 매칭은 검증된 이메일만
        role = self._derive_role(email, groups, email_verified=email_verified)

        # 6. User upsert (provider, sso_subject 기준)
        user, was_created, team_changed = await self._upsert_user(
            session=session,
            sso_subject=str(sub),
            email=str(email),
            display_name=str(name),
            team_id=team_id,
            role=role,
            email_verified=email_verified,
        )

        # 7. 기존 사용자의 팀 변경 → transfer_user (모든 cache/RL/budget hook)
        #    단 team_source='manual'(관리자가 admin-ui 로 배정)인 사용자는
        #    되돌리지 않는다 — 그룹-유도 팀으로의 자동 복귀가 수동 배정을
        #    지우고 이관 부수효과를 다시 발화한다(migration 0048).
        if not was_created and team_changed and user.team_source != "manual":
            actor = CurrentUser(
                user_id=user.id, email=user.email, role=user.role, team_id=team_id,
            )
            # 롤백은 savepoint 안에서 건드린 ORM 객체를 expire 시킨다 — except
            # 이후에도 쓰는 값(user.id 등)은 미리 값으로 빼둔다(R21 r2 S-2).
            uid = user.id
            try:
                async with session.begin_nested():
                    await self._user_team_service.transfer_user(
                        session,
                        user_id=uid,
                        new_team_id=team_id,
                        actor=actor,
                        ip_address=ip_address,
                        request_id=request_id,
                        team_source="sync",
                        from_sync=True,
                    )
                # transfer_user 가 user.team_id 업데이트
            except Exception:
                # savepoint 롤백으로 부분 이관(예산만 비활성) 상태를 되돌린다 —
                # 잡지 않던 예외가 반쪽 커밋을 남기던 결함(R21 S8).
                # expire 된 user 를 아래 is_active/team_id 접근 전에 복구한다 —
                # 안 하면 MissingGreenlet 으로 로그인 자체가 500 이 된다.
                try:
                    await session.refresh(user)
                except Exception:
                    pass
                logger.exception(
                    "oidc.transfer_user_failed",
                    user_id=str(uid),
                    new_team_id=str(team_id),
                )
                # 실패해도 인증은 진행 (다음 로그인 때 재시도)

        if not user.is_active:
            raise OIDCNotProvisionableError("user_deactivated")

        # 8. VK 발급 — OIDC 흐름은 spec 에 따라 짧은 TTL (default 24h) 명시.
        actor = CurrentUser(
            user_id=user.id, email=user.email, role=user.role, team_id=user.team_id,
        )
        vk_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            hours=settings.OIDC_VK_TTL_HOURS
        )
        key_result = await self._key_service.issue_key(
            session,
            user_id=user.id,
            actor=actor,
            expires_at=vk_expires_at,
            sso_session_expires_at=sso_session_expires_at,
            ip_address=ip_address,
            request_id=request_id,
            user=user,  # 같은 session upsert 한 객체 재사용 → issue_key 내부 재조회 skip
        )

        # 9. Gateway-proxy 용 budget/model 캐시 hydrate.
        # auto_provision 신규 INSERT 케이스는 USER scope budget 이 없음 → None 반환되는 것이 정상.
        # 기존 유저는 여전히 조회 필요 (최적화 대상 아님).
        budget_repo = BudgetRepository(session)
        user_budget = await budget_repo.get_first_active_config(BudgetScope.USER, user.id)
        if redis is not None:
            await CLIService._cache_for_gateway(redis, user, user_budget)

        return VirtualKeyIssueResponse(
            virtual_key=key_result.virtual_key or "",
            expires_at=key_result.expires_at,
            gateway_endpoint="",  # CLI 가 ANTHROPIC_BASE_URL 에서 가져옴
            otel_endpoint="",
            user_id=str(user.id),
            team_id=str(user.team_id) if user.team_id else None,
            max_budget_usd=user_budget.max_budget_usd if user_budget else None,
            used_usd=None,
            tpm_limit=None,
            rpm_limit=None,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_group(group_name: str) -> tuple[str | None, str] | None:
        """Cognito 그룹명 → (department_name | None, team_name) 또는 None.

        - underscore 1개 ("Claude_team"): (None, "team") — Default Department 팀
        - underscore 2개 ("Claude_dept_team"): ("dept", "team")
        - 그 외 (prefix 불일치, underscore 0/3+, 빈 세그먼트): None → reject

        ``ClaudeAdmin`` 같은 admin 부트스트랩 그룹도 prefix 없이 None 반환
        (팀 매핑 로직 skip, _derive_role 이 ADMIN_GROUPS 별도 체크).
        """
        settings = get_settings()
        prefix = settings.OIDC_GROUP_PREFIX
        if not group_name.startswith(prefix):
            return None
        tail = group_name[len(prefix):]
        if not tail:
            return None
        parts = tail.split("_")
        if len(parts) == 1:
            team = parts[0]
            return (None, team) if team else None
        if len(parts) == 2:
            dept, team = parts
            return (dept, team) if (dept and team) else None
        return None

    async def _resolve_team(
        self, session: AsyncSession, groups: list[str]
    ) -> uuid.UUID:
        """그룹 리스트 → team_id. 매칭 안 되면 DEFAULT_TEAM_ID 또는 거부."""
        settings = get_settings()
        repo = UserRepository(session)

        for group_name in groups:
            parsed = self._parse_group(group_name)
            if parsed is None:
                continue
            dept_name, team_name = parsed
            team = await self._get_or_create_team(
                repo, session, dept_name=dept_name, team_name=team_name,
            )
            return team.id

        if settings.OIDC_REJECT_UNMATCHED_GROUPS:
            raise OIDCNotProvisionableError("no_matching_team_group")

        return uuid.UUID(settings.DEFAULT_TEAM_ID)

    async def _get_or_create_team(
        self,
        repo: UserRepository,
        session: AsyncSession,
        *,
        dept_name: str | None,
        team_name: str,
    ) -> Team:
        """(dept_name, team_name) 으로 팀 조회/생성.

        - dept_name is None  → Default Department 하위에서 이름으로 팀 조회/생성
        - dept_name 주어짐    → 그 부서 조회/생성 후 그 아래에서 이름으로 팀 조회/생성

        동일 이름 팀이 다른 부서에 있어도 서로 독립적으로 취급 (dept_id + name 유니크 스코프).
        """
        settings = get_settings()

        # 1) 대상 부서 결정 (조회 또는 생성)
        dept = await self._get_or_create_department(repo, dept_name=dept_name)

        # 2) 부서 하위에서 팀 검색 — (dept_id, name) 단건 SELECT.
        # 10k boot storm 부하테스트 v4 에서 list_all_teams (전 팀 + 멤버 selectinload)
        # 를 매 exchange 마다 호출하던 경로가 DB connection 장시간 점유의 주원인이었음.
        team = await repo.get_team_by_dept_and_name(dept.id, team_name)
        if team is not None:
            return team

        # 3) 자동 생성 — check-then-act 경합 대비:
        #    동시 exchange 가 같은 (dept_id, name) 을 넣으면 uq_teams_dept_name
        #    인덱스가 패자의 INSERT 를 승자 커밋까지 블록한 뒤 IntegrityError 를 낸다.
        #    savepoint 로 감싸 외부 트랜잭션을 살리고, 승자 행을 재조회해 반환한다.
        try:
            async with session.begin_nested():
                team = Team(id=uuid.uuid4(), dept_id=dept.id, name=team_name)
                await repo.create_team(team)

                # Budget=$0 (HARD_BLOCK) — admin 이 예산 관리에서 풀어줄 때까지 deny.
                # team_allowed_models 행은 추가 안 함 → auth_service 가 None=전체 허용으로 처리.
                budget_repo = BudgetRepository(session)
                await budget_repo.upsert_config(
                    BudgetConfig(
                        id=uuid.uuid4(),
                        scope=BudgetScope.TEAM,
                        scope_id=team.id,
                        max_budget_usd=Decimal("0"),
                        period_type=PeriodType.MONTHLY,
                        policy=BudgetPolicy.HARD_BLOCK,
                        allocated_by=uuid.UUID(settings.SYSTEM_USER_ID),
                        effective_from=date.today(),
                        is_active=True,
                    )
                )
        except IntegrityError:
            # UNIQUE 충돌은 패자 트랜잭션이 승자 커밋을 기다린 뒤에야 발생하므로
            # 여기서 재조회하면 승자 행이 반드시 보인다.
            team = await repo.get_team_by_dept_and_name(dept.id, team_name)
            if team is not None:
                logger.info(
                    "oidc.team_create_race_lost",
                    team_name=team_name, dept_id=str(dept.id),
                    winner_team_id=str(team.id),
                )
                return team
            raise

        logger.info(
            "oidc.team_auto_created",
            team_id=str(team.id),
            team_name=team_name,
            dept_id=str(dept.id),
            dept_name=dept.name,
        )
        return team

    async def _get_or_create_department(
        self,
        repo: UserRepository,
        *,
        dept_name: str | None,
    ) -> Department:
        """dept_name is None → Default Department 반환.
        dept_name 주어짐 → 동일 org 에서 이름 매칭, 없으면 자동 생성.
        """
        settings = get_settings()

        if dept_name is None:
            default_dept_id = uuid.UUID(settings.DEFAULT_DEPT_ID)
            dept = await repo.get_department(default_dept_id)
            if dept is None:
                raise OIDCConfigError(
                    f"Default department not found (DEFAULT_DEPT_ID={default_dept_id}). "
                    "Check db/init seed."
                )
            return dept

        # 기명 부서: default org (또는 첫 org) 내에서 이름 매칭 — 단건 SELECT.
        # list_all_orgs 는 org × dept × team × members 를 전부 로드하므로 hot path
        # 에서 회피. 단일 org 운영 가정 (기존 로직과 동일).
        target_org = await repo.get_default_org()
        if target_org is None:
            raise OIDCConfigError(
                "No organization found. Check db/init seed (default org required)."
            )
        dept_existing = await repo.get_department_by_name(target_org.id, dept_name)
        if dept_existing is not None:
            return dept_existing

        # 자동 생성 — 첫 번째 org 아래에 (단일 org 운영 가정).
        # 팀과 같은 check-then-act 경합: uq_departments_org_name 인덱스가 패자를
        # IntegrityError 로 만들고, 승자 커밋 후 재조회하면 행이 보인다.
        try:
            async with repo._session.begin_nested():
                dept = Department(id=uuid.uuid4(), org_id=target_org.id, name=dept_name)
                await repo.create_department(dept)
        except IntegrityError:
            dept_existing = await repo.get_department_by_name(target_org.id, dept_name)
            if dept_existing is not None:
                logger.info(
                    "oidc.dept_create_race_lost",
                    dept_name=dept_name, org_id=str(target_org.id),
                    winner_dept_id=str(dept_existing.id),
                )
                return dept_existing
            raise
        logger.info(
            "oidc.department_auto_created",
            dept_id=str(dept.id),
            dept_name=dept_name,
            org_id=str(target_org.id),
        )
        return dept

    @staticmethod
    def _derive_role(email: str, groups: list[str], *, email_verified: bool = False) -> UserRole:
        """ADMIN_EMAILS / ADMIN_GROUPS 둘 중 하나라도 매칭되면 ADMIN.

        ⚠️ 정책 본체는 ``core.oidc_identity.derive_role`` 하나뿐이다. 여기서 복제하면
           admin_jwt(리소스 서버) 경로와 조용히 어긋난다 — 그 부류의 사고가 이미
           한 번 있었다(oidc_identity 모듈 docstring 참조). 이 래퍼는 기존 호출부와
           테스트를 유지하기 위해서만 남긴다.

        TEAM_LEADER 는 Cognito 그룹으로 부트스트랩하지 않는다 — "어느 팀"의
        리더인지까지 그룹명만으로 명확히 표현하려면 팀 매핑용 그룹과 별도로 또
        하나의 그룹(예: ClaudeTeamLeader)에 동시 가입해야 해서 운영 부담이 크고,
        두 그룹 매핑이 어긋나면 엉뚱한 팀의 리더가 될 위험도 있다. 대신 관리자가
        admin-ui 에서 팀원 한 명을 리더로 지정한다 (``PUT /admin/teams/{id}/leader``,
        ``UserTeamService.set_team_leader``) — 팀이 명확히 고정되어 모호함이 없다.
        ``_upsert_user`` 가 이 수동 지정을 Cognito 재로그인/재동기화 때 덮어쓰지
        않도록 보존한다.
        """
        return derive_role(email, groups, email_verified=email_verified)

    async def _upsert_user(
        self,
        *,
        session: AsyncSession,
        sso_subject: str,
        email: str,
        display_name: str,
        team_id: uuid.UUID,
        role: UserRole,
        email_verified: bool = False,
    ) -> tuple[User, bool, bool]:
        """
        Returns (user, was_created, team_changed).

        provider 컬럼 추가 마이그레이션 적용 후 ``(provider, sso_subject)`` 복합키로 식별.
        마이그레이션 전 단계에서는 ``sso_subject`` 단독으로 동작.

        ``email_verified`` — 이메일 기반 계정 연결(2차 fallback)의 전제다.
        이메일은 검증 가능한 식별자가 아니다: IdP 가 검증하지 않은 이메일 클레임을
        그대로 믿고 기존 계정에 sso_subject 를 갈아끼우면, 공격자가 피해자 주소를
        자기 것이라고 주장하는 것만으로 계정을 탈취할 수 있다. 검증 근거는
        ``email_verified`` 클레임 또는 Cognito AdminGetUser 의 서버측 속성뿐이다.
        """
        settings = get_settings()
        repo = UserRepository(session)

        # provider scoping 은 마이그레이션 이후 활성. 현재는 sso_subject 만.
        # OIDC 의 sub 가 STS 의 ARN 과 충돌할 가능성은 거의 0 (포맷 다름).
        # 1차: sso_subject 로 조회. Cognito 재생성/재link 로 sub 가 바뀌면 miss 가능.
        existing = await repo.get_by_sso_subject(sso_subject)

        # 2차 fallback: email 로 조회. 같은 사람이 새 sub 로 들어온 경우(이메일 동일)
        # 기존 row 재사용 + sso_subject 갱신 → email UNIQUE 충돌(500) 방지.
        # ⚠️ 미검증 이메일로는 연결하지 않는다 — 아래는 계정 탈취 경로를 닫는 게이트.
        # 그냥 넘기면 새 행 생성이 email UNIQUE 에 걸려 500 이므로 여기서 명시 거부.
        if existing is None:
            by_email = await repo.get_by_email(email)
            if by_email is not None:
                if not email_verified:
                    logger.warning(
                        "oidc.email_link_refused_unverified",
                        email=email,
                        existing_user_id=str(by_email.id),
                        new_sso_subject=sso_subject,
                    )
                    raise OIDCNotProvisionableError(
                        "email_unverified: 이메일이 IdP 에서 검증되지 않아 기존 계정과 연결할 수 없다"
                    )
                logger.info(
                    "oidc.sso_subject_reconciled",
                    user_id=str(by_email.id),
                    email=email,
                    old_sso_subject=by_email.sso_subject,
                    new_sso_subject=sso_subject,
                )
                by_email.sso_subject = sso_subject
                existing = by_email

        if existing is None:
            user = User(
                id=uuid.uuid4(),
                # 미검증 이메일은 DB 에 싣지 않는다 — 저장된 주소는 이후 세션의
                # ADMIN_EMAILS 폴백 매칭(auth.py)과 이메일 조회의 신뢰 근거가
                # 되므로, 검증 안 된 주소를 저장하면 그 신뢰가 위조된다.
                email=email if email_verified else f"{sso_subject}@unknown",
                display_name=display_name,
                role=role,
                sso_subject=sso_subject,
                team_id=team_id,
                is_active=True,
                # provider 컬럼은 마이그레이션 이후 활성. 일단 model 에 setattr 로 시도.
            )
            # provider 속성이 모델에 추가되면 자동 반영. 아직 없으면 silently skip.
            if hasattr(User, "provider"):
                user.provider = settings.OIDC_PROVIDER_NAME
            # 동시 최초 로그인 경합: 같은 sub 로 두 요청이 나란히 오면 패자는
            # sso_subject/email UNIQUE 에 IntegrityError — savepoint 로 외부
            # 트랜잭션을 살리고 승자 행을 재조회해 기존사용자 경로로 내린다.
            try:
                async with session.begin_nested():
                    user = await repo.create_user(user)
            except IntegrityError:
                winner = await repo.get_by_sso_subject(sso_subject)
                if winner is not None:
                    logger.info(
                        "oidc.user_create_race_lost",
                        sso_subject=sso_subject,
                        winner_user_id=str(winner.id),
                    )
                    existing = winner
                else:
                    raise
            else:
                logger.info(
                    "oidc.user_auto_provisioned",
                    user_id=str(user.id),
                    email=email,
                    role=role.value,
                    team_id=str(team_id),
                    provider=settings.OIDC_PROVIDER_NAME,
                )
                return user, True, False

        team_changed = existing.team_id != team_id
        # 기존 사용자: email/role/display_name 만 동기화. team 은 transfer_user 로 별도 처리.
        # email 가드: <sub>@unknown synthetic 이 admin Cognito-sync 가 채운 실제 email 을
        # 덮어쓰지 않도록. (Cognito access_token 엔 email claim 이 없어 매 로그인마다
        # synthetic 으로 회귀하던 버그 봉합.) 기존 email 도 synthetic 이면 갱신 허용.
        # 미검증 클레임 이메일은 DB 이메일을 덮지 않는다 — 저장된 이메일은
        # verified 통과분만 유지한다.
        incoming_is_synthetic = email.endswith("@unknown")
        existing_is_synthetic = (existing.email or "").endswith("@unknown")
        if (
            email_verified
            and existing.email != email
            and (not incoming_is_synthetic or existing_is_synthetic)
        ):
            existing.email = email
        existing.display_name = display_name
        # role 변경은 admin 권한 변동이므로 명시 로그.
        # TEAM_LEADER 는 Cognito 그룹이 아니라 admin-ui("팀 리더 지정")에서만 설정되므로
        # (_derive_role 은 ADMIN/DEVELOPER 만 반환), Cognito 재로그인 때 DEVELOPER 로
        # 되돌아가지 않도록 보존한다. ADMIN_GROUPS 매칭(승격) / 제외(강등)는 그대로 반영.
        if existing.role == UserRole.TEAM_LEADER and role == UserRole.DEVELOPER:
            role = UserRole.TEAM_LEADER
        # auth.py:429 와 같은 규칙 — ADMIN 출처가 IdP 로 설정되지 않은 환경에서는
        # DB role 이 유일한 원천이라 클레임 기준 강등을 하면 수동 부여 admin 이
        # 첫 로그인/다음 sync 에서 잠긴다.
        elif (
            existing.role == UserRole.ADMIN
            and role != UserRole.ADMIN
            and not admin_mapping_configured()
        ):
            role = UserRole.ADMIN
        role_changed = existing.role != role
        if role_changed:
            logger.info(
                "oidc.user_role_changed",
                user_id=str(existing.id),
                old_role=existing.role.value,
                new_role=role.value,
            )
            existing.role = role
        # 팀/역할이 바뀌면서 옛 팀의 leader_user_id 가 이 사람을 계속 가리키는
        # 스테일 포인터를 정리한다 — 승격(ADMIN)·그룹 변경 강등·팀 이동 경로.
        # 변동 없는 로그인마다 savepoint+SELECT 를 타지 않도록 변경 시에만 돌린다
        # — 이 경로는 issue_key 의 10k boot storm 에 맞춰 튜닝된 핫 패스다.
        # 과거에 남은 스테일 포인터는 DELETE /teams/{id}/leaders/{uid} 로 정리.
        # 표시용 포인터라 정리 실패가 로그인 자체를 깨면 안 된다(최선노력).
        # id 는 try 전에 빼둔다 — 세션이 깨진 뒤 속성 접근이 다시 터질 수 있다.
        if team_changed or role_changed or not existing.is_active:
            uid = existing.id
            try:
                await release_stale_leader_pointer(
                    session,
                    user_id=uid,
                    team_id=existing.team_id,
                    role=existing.role,
                    is_active=existing.is_active,
                )
            except Exception:
                logger.warning(
                    "oidc.stale_leader_pointer_cleanup_failed",
                    user_id=str(uid),
                    exc_info=True,
                )
        return existing, False, team_changed
