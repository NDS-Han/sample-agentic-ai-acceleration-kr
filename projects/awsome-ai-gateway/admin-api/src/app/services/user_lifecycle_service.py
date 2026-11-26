# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""사용자 수명주기(외부 통합용) — provision / deactivate.

/aicm/v1 라우터만 부른다(``AICM_API_ENABLED=false`` 면 import 되지 않는다). 업무 규칙은 기존
서비스를 그대로 쓴다: 게이트웨이 DB 행 생성·갱신은 ``CognitoSyncService.sync_user``, VK 폐기는
``KeyService.revoke_keys_for_users`` + ``finalize_revoked_keys``, 리더 포인터 정리는
``release_stale_leader_pointer``.

Cognito 호출은 모두 ``CognitoAdmin`` 을 거친다(타임아웃·쿼터 가드·오류 매핑).
각 단계는 "확인 후 실행" 이라 같은 요청을 다시 보내면 끊긴 지점부터 이어 간다 — 보상 삭제는 없다.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.cache_invalidation import publish_gateway_auth_invalidation
from app.core.config import get_settings
from app.core.exceptions import IntegrationError, NotFoundError
from app.core.oidc_identity import flatten_groups, team_resolution_groups
from app.models.auth import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.cognito_admin import USER_READ, CognitoAdmin, CognitoClientError, attr
from app.services.cognito_sync_service import CognitoSyncService
from app.services.user_team_service import release_stale_leader_pointer

logger = structlog.get_logger()

#: 시스템 합성 사용자(03_seed_data.sql) — 통합이 비활성화할 수 없다.
_SYSTEM_USER_IDS = frozenset({
    uuid.UUID("00000000-0000-4000-a000-000000000010"),
    uuid.UUID("00000000-0000-4000-a000-000000000011"),
})

_EMAIL_RE = re.compile(r"^[^@\s\"\\]{1,64}@[^@\s\"\\]{1,255}\.[^@\s\"\\]{1,63}$")
_LINK_ATTR_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
#: Cognito 사용자 지정 속성 값 상한.
_ATTR_MAX = 2048
DEACTIVATION_REASON = "integration_deactivated"
_NEW_TEAM = object()


@dataclass(frozen=True)
class _ProvisionRefusal:
    code: str
    message: str
    reason_code: str
    details: dict


@dataclass(frozen=True)
class _ProvisionPlan:
    write_groups: list[str]
    groups_update_needed: bool


def _err(status: int, code: str, message: str, **details) -> IntegrationError:
    return IntegrationError(message, code=code, status_code=status, details=details or None)


def _safe_filter_value(v: str) -> str:
    # ListUsers Filter 는 "attr = \"value\"" 형식 — 따옴표/역슬래시가 들어간 값은 필터를 깨뜨린다.
    if not v or '"' in v or "\\" in v:
        raise _err(422, "invalid_identifier", "value cannot be used in a Cognito filter")
    return v


class UserLifecycleService:
    def __init__(self, cognito: CognitoAdmin, *, key_service, cache_mgr, redis=None) -> None:
        self._cog = cognito
        self._key_service = key_service
        self._cache_mgr = cache_mgr
        self._redis = redis

    def _sync_service(self) -> CognitoSyncService:
        return CognitoSyncService(self._cog.client, key_service=self._key_service, cache_mgr=self._cache_mgr)

    # ── provision ────────────────────────────────────────────────────────────

    async def _pool_groups(self, username: str) -> list[str]:
        """쓰기 전 판정용 pool 그룹 전체(페이지네이션)."""
        out: list[str] = []
        token = None
        while True:
            kw = {"Username": username, "Limit": 60}
            if token:
                kw["NextToken"] = token
            resp = await self._cog.call("admin_list_groups_for_user", **kw)
            out.extend(g["GroupName"] for g in resp.get("Groups", []))
            token = resp.get("NextToken")
            if not token:
                return out

    @staticmethod
    async def _target_rows(session: AsyncSession, *, sub: str | None, emails: list[str]) -> list[User]:
        clauses = []
        if sub:
            clauses.append(User.sso_subject == sub)
        if emails:
            clauses.append(User.email.in_(emails))
        if not clauses:
            return []
        stmt = (
            select(User)
            .where(or_(*clauses) if len(clauses) > 1 else clauses[0])
            .order_by(User.id)
            .with_for_update()
        )
        return list((await session.execute(stmt)).scalars())

    @staticmethod
    def _refusal(code: str, message: str, reason_code: str, rows: list[User], **details) -> _ProvisionRefusal:
        return _ProvisionRefusal(
            code=code,
            message=message,
            reason_code=reason_code,
            details={
                "reason_code": reason_code,
                "target_user_ids": [str(r.id) for r in rows],
                **details,
            },
        )

    @staticmethod
    def _merge_write_groups(current: list[str], desired: list[str]) -> tuple[list[str], bool]:
        """관리 대상(팀 그룹)만 바꾼 속성값과 쓰기 필요 여부.

        AICM 이 모르는 비-팀 그룹은 지우지 않고 앞쪽 순서를 유지한다. 팀 해석은
        첫 파싱 가능 그룹이므로 비-팀 그룹의 위치는 역할/팀에 영향이 없다.
        """
        current_team = [g for g in current if CognitoSyncService._parse_group(g) is not None]
        if current_team == desired:
            return list(current), False
        merged: list[str] = []
        for g in current:
            if CognitoSyncService._parse_group(g) is not None:
                continue
            if g not in merged:
                merged.append(g)
        for g in desired:
            if g not in merged:
                merged.append(g)
        return merged, True

    async def _predicted_team_id(self, session: AsyncSession, effective_groups: list[str]):
        s = get_settings()
        parsed = None
        for g in effective_groups:
            parsed = CognitoSyncService._parse_group(g)
            if parsed is not None:
                break
        if parsed is None:
            return uuid.UUID(s.DEFAULT_TEAM_ID)
        dept_name, team_name = parsed
        cache = await self._sync_service()._build_team_cache(UserRepository(session))
        dept_id = cache.default_dept_id if dept_name is None else cache.depts_by_name.get(dept_name)
        if dept_id is None:
            return _NEW_TEAM
        return cache.teams.get((dept_id, team_name), _NEW_TEAM)

    async def _provision_preflight(
        self, session: AsyncSession, *, email: str, cognito_user: dict | None,
        desired_groups: list[str], pool_groups: list[str],
    ) -> _ProvisionPlan | _ProvisionRefusal:
        """Cognito/DB 쓰기 전 보호 대상·역할 변경 가능성 판정.

        반환된 계획만 이후 쓰기에 쓴다. 거부는 예외가 아니라 403 응답 본문으로
        돌려주어 멱등키 저장소가 같은 403 을 재생할 수 있게 한다.
        """
        s = get_settings()
        groups_attr = s.AICM_PROVISION_GROUPS_ATTRIBUTE
        username = cognito_user.get("Username") if cognito_user else None
        sub = (attr(cognito_user, "sub") if cognito_user else None) or username
        cognito_email = (attr(cognito_user, "email") if cognito_user else None) or email
        emails = list(dict.fromkeys(e for e in (email, cognito_email) if e))
        rows = await self._target_rows(session, sub=sub, emails=emails)

        for row in rows:
            if row.id in _SYSTEM_USER_IDS:
                return self._refusal(
                    "target_is_system_user", "system users cannot be changed by an integration",
                    "system_user", [row],
                )
        for row in rows:
            if row.is_service_account:
                return self._refusal(
                    "target_is_service_account", "service accounts cannot be changed by an integration",
                    "service_account", [row],
                )
        for row in rows:
            if row.role == UserRole.ADMIN:
                return self._refusal(
                    "target_is_admin", "gateway admins cannot be changed by an integration",
                    "gateway_admin", [row],
                )

        admin_emails = {e.lower() for e in s.ADMIN_EMAILS}
        if any(e.lower() in admin_emails for e in emails):
            return self._refusal(
                "target_is_admin", "admin emails cannot be changed by an integration",
                "admin_email", rows,
            )

        groups_attr_names = list(dict.fromkeys((groups_attr, s.OIDC_GROUPS_CLAIM)))
        attr_groups: list[str] = []
        current_write_attr: list[str] = []
        if cognito_user is not None:
            for name in groups_attr_names:
                values = flatten_groups(attr(cognito_user, name))
                attr_groups.extend(values)
                if name == groups_attr:
                    current_write_attr = values
        if any(g in set(s.ADMIN_GROUPS) for g in attr_groups + pool_groups):
            return self._refusal(
                "target_is_admin", "Cognito admin group members cannot be changed by an integration",
                "cognito_admin_group", rows,
            )

        if cognito_user is None:
            write_groups = list(desired_groups)
            groups_update_needed = False
        else:
            write_groups, groups_update_needed = self._merge_write_groups(current_write_attr, desired_groups)
            if len(";".join(write_groups)) > _ATTR_MAX:
                raise _err(422, "groups_too_long", f"joined groups exceed {_ATTR_MAX} characters")

        if any(row.role == UserRole.TEAM_LEADER for row in rows):
            # sync_user 가 읽을 사후 속성 기준으로 팀을 읽기 전용으로 예측한다.
            # TEAM_LEADER 를 다른 팀으로 옮기면 현재 sync 규칙은 DEVELOPER 강등이므로
            # 그런 요청은 Cognito 속성/링크를 쓰기 전에 거부한다.
            if cognito_user is None:
                post_sync_attr = write_groups if groups_attr == s.OIDC_GROUPS_CLAIM else []
            elif groups_attr == s.OIDC_GROUPS_CLAIM:
                post_sync_attr = write_groups
            else:
                post_sync_attr = flatten_groups(attr(cognito_user, s.OIDC_GROUPS_CLAIM))
            effective = team_resolution_groups(post_sync_attr + pool_groups)
            predicted_team = await self._predicted_team_id(session, effective)
            for row in rows:
                if (
                    row.role == UserRole.TEAM_LEADER
                    and row.team_source != "manual"
                    and row.team_id != predicted_team
                ):
                    return self._refusal(
                        "team_leader_change_forbidden",
                        "provisioning must not change a team leader's role or team leadership",
                        "team_leader_change", [row],
                        current_team_id=str(row.team_id) if row.team_id else None,
                    )
        return _ProvisionPlan(write_groups=write_groups, groups_update_needed=groups_update_needed)

    async def _refuse_provision(
        self, session: AsyncSession, *, email: str, cognito_user: dict | None,
        refusal: _ProvisionRefusal, requested_groups: list[str], actor,
        ip_address: str, request_id: str,
    ) -> tuple[int, dict, None]:
        username = cognito_user.get("Username") if cognito_user else None
        sub = (attr(cognito_user, "sub") if cognito_user else None) or username
        target_ids = refusal.details.get("target_user_ids", [])
        resource_id = target_ids[0] if target_ids else email
        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="AICM_PROVISION_REFUSED",
            resource_type="User",
            resource_id=resource_id,
            result="FAILURE",
            changes={
                "email": email,
                "cognito_username": username,
                "cognito_sub": sub,
                "requested_groups": requested_groups,
                **refusal.details,
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        error = {"type": "forbidden", "message": refusal.message, "code": refusal.code}
        if refusal.details:
            error["details"] = refusal.details
        return 403, {"error": error}, None


    @staticmethod
    def validate_groups(groups: list[str]) -> list[str]:
        s = get_settings()
        admin = set(s.ADMIN_GROUPS)
        out: list[str] = []
        for g in groups:
            g = g.strip()
            if not g:
                continue
            if g in admin:
                raise _err(403, "admin_group_forbidden", f"integration cannot assign admin group {g!r}")
            if ";" in g or "," in g:
                raise _err(422, "invalid_group", f"group name must not contain ';' or ',': {g!r}")
            if not g.startswith(s.OIDC_GROUP_PREFIX):
                raise _err(422, "group_not_allowed",
                           f"group {g!r} does not start with {s.OIDC_GROUP_PREFIX!r}")
            if g not in out:
                out.append(g)
        if not out:
            raise _err(422, "no_team_group", "at least one team group is required")
        if not any(CognitoSyncService._parse_group(g) is not None for g in out):
            raise _err(422, "no_team_group", "no group maps to a gateway team",
                       prefix=s.OIDC_GROUP_PREFIX)
        if len(";".join(out)) > _ATTR_MAX:
            raise _err(422, "groups_too_long", f"joined groups exceed {_ATTR_MAX} characters")
        return out

    async def provision(
        self, session: AsyncSession, *, email: str, display_name: str | None, groups: list[str],
        link: dict | None, actor, ip_address: str, request_id: str,
    ) -> tuple[int, dict, object | None]:
        """Cognito 생성/연결/속성 정정 + 게이트웨이 sync.

        반환은 ``(status, body, after_commit)``. ``sync_user(commit=False)`` 로 게이트웨이 DB 변경·
        감사·멱등키 완료 행을 한 트랜잭션에 묶고, 커밋 후 ``after_commit`` 이 sync 중 수집된
        캐시/VK 부수효과를 적용한다. 보호 대상 거부는 403 응답도 멱등 저장소에 남긴다.
        """
        s = get_settings()
        email = (email or "").strip()
        if len(email) > 320 or not _EMAIL_RE.fullmatch(email):
            raise _err(422, "invalid_email", "email is not a valid address")
        groups = self.validate_groups(groups)
        provider = s.COGNITO_FEDERATED_PROVIDER_NAME.strip()
        if link and not provider:
            raise _err(400, "federation_not_configured",
                       "link given but COGNITO_FEDERATED_PROVIDER_NAME is not configured")
        if provider and not link:
            # 연결 없는 네이티브 프로필은 ADFS 전용 앱 클라이언트로 로그인할 수 없고, 이후 ADFS
            # 로그인은 sub 가 다른 별도 프로필을 만든다 — 보수적으로 거부한다.
            raise _err(422, "link_required",
                       f"link is required when federation is configured (provider {provider!r})")
        if link:
            la, lv = str(link.get("attribute", "")), str(link.get("value", ""))
            if not _LINK_ATTR_RE.fullmatch(la):
                raise _err(422, "invalid_link", "link.attribute is invalid")
            if not lv or len(lv) > _ATTR_MAX or any(ord(c) < 32 for c in lv):
                raise _err(422, "invalid_link", "link.value is invalid")
        if not self._cog.pool_id:
            raise _err(503, "cognito_not_configured", "COGNITO_USER_POOL_ID is not configured")
        groups_attr = s.AICM_PROVISION_GROUPS_ATTRIBUTE
        joined = ";".join(groups)

        # 같은 이메일 동시 provision 을 replica 간 직렬화 — 최종 커밋(=sync·감사·멱등키)까지 유지.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"itg-provision:{email.lower()}"},
        )

        users = await self._cog.find_by_email(_safe_filter_value(email))
        user = self._pick(users)
        created = False
        plan = None
        if user is None:
            preflight = await self._provision_preflight(
                session, email=email, cognito_user=None, desired_groups=groups, pool_groups=[],
            )
            if isinstance(preflight, _ProvisionRefusal):
                return await self._refuse_provision(
                    session, email=email, cognito_user=None, refusal=preflight,
                    requested_groups=groups, actor=actor, ip_address=ip_address, request_id=request_id,
                )
            plan = preflight
            user, created = await self._create_or_recover(email, display_name, groups_attr, joined)

        username = user["Username"]
        if not created:
            pool_groups = await self._pool_groups(username)
            preflight = await self._provision_preflight(
                session, email=email, cognito_user=user, desired_groups=groups, pool_groups=pool_groups,
            )
            if isinstance(preflight, _ProvisionRefusal):
                return await self._refuse_provision(
                    session, email=email, cognito_user=user, refusal=preflight,
                    requested_groups=groups, actor=actor, ip_address=ip_address, request_id=request_id,
                )
            plan = preflight

        federated = user.get("UserStatus") == "EXTERNAL_PROVIDER"
        state = "created" if created else ("already_federated" if federated else "existing")

        newly_linked = False
        if provider and link and not federated:
            newly_linked = await self._ensure_linked(user, username, provider, link, created=created)
        if created and not provider:
            state = "created_unlinked"
        elif newly_linked and not created:
            state = "linked"

        groups_updated = False
        if not created and plan.groups_update_needed:
            await self._call_after_create(
                created, "admin_update_user_attributes", Username=username,
                UserAttributes=[{"Name": groups_attr, "Value": ";".join(plan.write_groups)}],
            )
            groups_updated = True

        # sync_user 의 읽기 호출 2회(AdminGetUser + AdminListGroupsForUser)만 통합 쿼터에서 선점한다.
        # 실제 boto3 client 는 같은 타임아웃 설정 client 를 공유하고, DB 변경은 commit=False 로
        # 이 요청 트랜잭션에 남긴다.
        await self._cog.acquire(USER_READ, 2)
        sync_service = self._sync_service()
        result = await sync_service.sync_user(
            session, username, commit=False, deactivate_missing=False,
        )
        if result.user_id is None:
            raise _err(502, "gateway_sync_failed", "Cognito user exists but gateway sync failed",
                       state=state, cognito_username=username, errors=result.errors[:5])
        row = await session.get(User, uuid.UUID(result.user_id), populate_existing=True)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="PROVISION_USER",
            resource_type="User",
            resource_id=result.user_id,
            changes={
                "cognito_username": username,
                "state": state,
                "provider": provider or None,
                # 연결 값(NameID 등)은 남기지 않는다 — 속성 이름만.
                "link_attribute": (link or {}).get("attribute"),
                "groups": groups,
                "groups_updated": groups_updated,
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        body = {
            "user_id": result.user_id,
            "cognito_username": username,
            "state": state,
            "team_id": str(row.team_id) if row and row.team_id else None,
            "role": row.role.value if row else None,
            "is_active": bool(row.is_active) if row else None,
            "sync_errors": result.errors,
        }

        async def after_commit() -> None:
            await sync_service._flush_pending_revocations(session, result)

        return 200, body, after_commit

    async def _create_or_recover(self, email: str, display_name: str | None, groups_attr: str,
                                 joined: str) -> tuple[dict, bool]:
        attrs = [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "name", "Value": (display_name or email.split("@", 1)[0])[:_ATTR_MAX]},
            {"Name": groups_attr, "Value": joined},
        ]
        try:
            resp = await self._cog.call(
                "admin_create_user", passthrough={"UsernameExistsException"},
                Username=email, MessageAction="SUPPRESS", UserAttributes=attrs,
            )
        except CognitoClientError:
            # 동시 생성 경합 — 다시 찾는다.
            picked = self._pick(await self._cog.find_by_email(email))
            if picked is None:
                raise _err(409, "cognito_user_conflict", "Cognito reports the user exists but it was not found")
            return picked, False
        return resp["User"], True

    @staticmethod
    def _pick(users: list[dict]) -> dict | None:
        # 같은 이메일의 프로필이 2개 이상이면 어느 쪽을 골라도 잘못 연결/갱신할 수 있다.
        # 네이티브+연합 프로필 혼합도 포함해 보수적으로 409 — 운영자가 풀 상태를 먼저 정리한다.
        if len(users) > 1:
            raise _err(409, "ambiguous_cognito_users", "more than one Cognito user has this email",
                       usernames=[u.get("Username") for u in users])
        if not users:
            return None
        user = users[0]
        # 이미 ADFS 로 로그인한 사람(<Provider>_<id> 프로필) — 연결하려면 그 프로필을 지워야
        # 하고(sub 가 바뀐다) 그건 하지 않는다. 동기화만 한다.
        return user

    async def _call_after_create(self, created: bool, op: str, **kwargs) -> dict:
        try:
            return await self._cog.call(op, **kwargs)
        except IntegrationError as e:
            if created:
                e.details = {**(e.details or {}), "partial_state": "created"}
                e.message = f"{e.message} (Cognito user was created; retry to finish)"
            raise

    async def _ensure_linked(self, user: dict, username: str, provider: str, link: dict, *,
                             created: bool) -> bool:
        raw = attr(user, "identities")
        if raw:
            try:
                if any(i.get("providerName") == provider for i in json.loads(raw)):
                    return False
            except (ValueError, TypeError, AttributeError):
                pass
        try:
            await self._cog.call(
                "admin_link_provider_for_user",
                passthrough={"AliasExistsException"},
                DestinationUser={"ProviderName": "Cognito", "ProviderAttributeValue": username},
                SourceUser={
                    "ProviderName": provider,
                    "ProviderAttributeName": str(link["attribute"]),
                    "ProviderAttributeValue": str(link["value"]),
                },
            )
        except CognitoClientError as e:
            raise _err(409, "link_conflict",
                       "the federated identity cannot be linked (already linked to another user "
                       "or already signed in)", cognito_code=e.code,
                       partial_state="created" if created else None)
        except IntegrationError as e:
            if created:
                e.details = {**(e.details or {}), "partial_state": "created"}
            raise
        return True

    # ── deactivate ───────────────────────────────────────────────────────────

    async def _load_target(self, session: AsyncSession, user_id: uuid.UUID) -> User:
        user = await session.get(User, user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if user_id in _SYSTEM_USER_IDS:
            raise _err(403, "target_is_system_user", "system users cannot be changed by an integration")
        if user.is_service_account:
            raise _err(403, "target_is_service_account", "service accounts cannot be changed by an integration")
        if user.role == UserRole.ADMIN:
            raise _err(403, "target_is_admin", "gateway admins cannot be changed by an integration")
        return user

    def _is_cognito_user(self, user: User) -> bool:
        return bool(self._cog.pool_id) and user.provider == get_settings().OIDC_PROVIDER_NAME

    async def deactivate(
        self, session: AsyncSession, *, user_id: uuid.UUID, reason: str, global_sign_out: bool,
        actor, ip_address: str, request_id: str,
    ):
        """Cognito 먼저(Disable + 선택 GlobalSignOut) → 한 트랜잭션(is_active=false, VK 폐기,
        리더 포인터, 감사). 반환 (body, after_commit) — after_commit 은 커밋 후 Redis DEL·pub/sub.

        Cognito 를 먼저 끄는 이유: DB 만 끄면 다음 sync_user 가 Enabled=true 를 보고 되살린다.
        Cognito 오류면 DB 는 건드리지 않는다.
        """
        user = await self._load_target(session, user_id)
        cognito_state = "not_cognito_user"
        if self._is_cognito_user(user):
            if not user.sso_subject:
                raise _err(409, "user_missing_sso_subject",
                           "OIDC user has no sso_subject; refusing DB-only deactivation")
            cu = await self._cog.find_by_sub(_safe_filter_value(user.sso_subject))
            if cu is None:
                cognito_state = "not_in_pool"
            else:
                uname = cu["Username"]
                if cu.get("Enabled", True):
                    await self._cog.call("admin_disable_user", Username=uname)
                    cognito_state = "disabled"
                else:
                    cognito_state = "already_disabled"
                if global_sign_out:
                    await self._cog.call("admin_user_global_sign_out", Username=uname)

        locked = await session.get(User, user_id, with_for_update=True, populate_existing=True)
        was_active = bool(locked.is_active)
        locked.is_active = False
        await session.flush()
        hashed = await self._key_service.revoke_keys_for_users(
            session, user_ids=[user_id], reason=DEACTIVATION_REASON
        )
        try:
            await release_stale_leader_pointer(
                session, user_id=user_id, team_id=locked.team_id, role=locked.role, is_active=False,
            )
        except Exception:
            logger.warning("aicm.deactivate.leader_pointer_cleanup_failed", user_id=str(user_id), exc_info=True)
        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            service_token_id=getattr(actor, "service_token_id", None),
            action="DEACTIVATE_USER",
            resource_type="User",
            resource_id=str(user_id),
            changes={
                "before": {"is_active": was_active},
                "after": {"is_active": False},
                "reason": reason,
                "revoked_key_count": len(hashed),
                "cognito": cognito_state,
                "global_sign_out": bool(global_sign_out) and cognito_state in ("disabled", "already_disabled"),
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        body = {
            "user_id": str(user_id),
            "state": "deactivated" if was_active else "already_inactive",
            "cognito": cognito_state,
            "revoked_key_count": len(hashed),
        }

        async def after_commit() -> None:
            if hashed:
                await self._key_service.finalize_revoked_keys(session, hashed, reason=DEACTIVATION_REASON)
            # 게이트웨이 pod 의 로컬 인증 캐시(is_active 포함)를 사용자 단위로 즉시 비운다.
            if self._redis is not None:
                await publish_gateway_auth_invalidation(self._redis, user_ids=[str(user_id)])

        return body, after_commit
