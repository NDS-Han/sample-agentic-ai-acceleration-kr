# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import structlog
from redis.asyncio.cluster import RedisCluster
from sqlalchemy import func, select as sa_select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients import CLIENT_ORDER
from app.core.budget_cache import (
    defer_redis_write_until_commit,
    read_budget_config,
    refresh_user_app_clients,
    write_user_budget_config,
)
from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import (
    CacheInvalidationManager,
    invalidate_after_commit,
)
from app.core.config import get_settings
from app.core.exceptions import (
    BudgetRuleError,
    ConfirmationRequiredError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.usage_filters import cost_period_filter, current_kst_period
from app.models.auth import Team, UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, DowngradePolicy, PeriodType
from app.models.usage import UsageLog
from app.repositories._locks import advisory_xact_lock
from app.repositories.budget_repository import BudgetRepository, DowngradePolicyRepository
from app.repositories.user_repository import UserRepository
from app.schemas.budgets import (
    AllocationEntry,
    AllocateBudgetRequest,
    AutoDowngradeConfigRequest,
    AutoDowngradeConfigResponse,
    BudgetConfigDetailResponse,
    BudgetSummaryItem,
    BudgetSummaryResponse,
    DowngradeRuleResponse,
    SeedSpentItem,
    SeedSpentResponse,
    SeedSpentResult,
    SetBudgetRequest,
    TeamBudgetAllocation,
)

logger = structlog.get_logger()

BUDGET_CONFIG_CACHE_TTL = 300  # 5 min; matches VK_AUTH_CACHE_TTL in key_service

_CENT = Decimal("0.01")


def _check_cent_precision(amount: Decimal) -> None:
    """D-17/§0-3: 예산 입력은 센트(2자리)까지. 초과 정밀도는 거부한다.

    저장은 NUMERIC(12,4) 가 허용하지만 입력 계약은 2자리다 — 소수 3~4자리를
    조용히 받아주면 UI 표시(센트 반올림)와 enforcement(정밀 비교)가 어긋난다.
    """
    try:
        ok = amount == amount.quantize(_CENT)
    except InvalidOperation:
        ok = False
    if not ok:
        raise BudgetRuleError(
            f"Budget amounts are limited to cents (2 decimal places): {amount}",
            "invalid_amount_precision",
        )


async def _lock_user_budget_key(session: AsyncSession, user_id: uuid.UUID) -> None:
    """같은 유저의 총액(A_u)·앱별(app_c)·삭제·일괄 쓰기를 직렬화한다 (§3-0/D-4).

    upsert_config 의 advisory lock 은 (scope, scope_id, client) 키라 총액과
    앱별이 서로 다른 키를 잡는다 — I-2 검증(app_c ≤ A_u)은 둘을 함께 읽으므로
    유저 단위의 별도 직렬화가 필요하다. 행 존재 여부와 무관하게 동작한다.
    """
    await advisory_xact_lock(session, "budget_user_rules", user_id)


def _confirmed_action(base: str, confirm: bool) -> str:
    """§3-0/G-8: confirm=true 재요청은 별도 audit action 으로 구분한다."""
    return f"{base}_CONFIRMED" if confirm else base

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
#: 단일 출처는 core/clients.py 다 — 앱 추가 시 한 곳만 고치면 되도록.
#: 튜플로 유지하는 이유: 기존 호출부가 순서를 가정한 곳이 있다.
_ALLOWED_CLIENTS = CLIENT_ORDER


def _team_display_name(team: Team) -> str:
    """Cognito 그룹명에서 부서_팀 형태(Claude_NDS_Developers → NDS_Developers)가
    깨져 `Team.name`에 팀 부분만 들어가 있을 때, 부서명을 prefix로 붙여 보여준다.
    default 부서 소속은 prefix를 붙이지 않는다(Claude_Developers → Developers)."""
    settings = get_settings()
    default_dept_id = uuid.UUID(settings.DEFAULT_DEPT_ID)
    dept = getattr(team, "department", None)
    if dept is not None and team.dept_id != default_dept_id:
        return f"{dept.name}_{team.name}"
    return team.name


def _redis_usage_key(scope: str, scope_id: str, period: str, client: str | None) -> str:
    """Enforcement counter key. MUST match gateway-proxy budget_check.lua keys.

    USER: budget:user:{<id>}:<period>   (triple-brace = Redis Cluster hash tag)
    TEAM: budget:team:{<id>}:<period>
    APP : budget:user:{<id>}:<client>:<period>
    """
    scope_type = scope.lower()
    if client:
        return f"budget:{scope_type}:{{{scope_id}}}:{client}:{period}"
    return f"budget:{scope_type}:{{{scope_id}}}:{period}"


class BudgetService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    _SEED_UPSERT = text(
        """
        INSERT INTO budget.budget_usages
            (id, scope, scope_id, period, client, used_usd, limit_usd, last_updated)
        VALUES (
            gen_random_uuid(),
            CAST(:scope AS budget.budget_scope),
            CAST(:scope_id AS uuid),
            :period,
            CAST(:client AS varchar),
            :spent,
            COALESCE((
                SELECT max_budget_usd FROM budget.budget_configs
                WHERE scope = CAST(:scope AS budget.budget_scope)
                  AND scope_id = CAST(:scope_id AS uuid)
                  AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
                  AND is_active = true
                ORDER BY effective_from DESC LIMIT 1
            ), 0),
            now()
        )
        ON CONFLICT (scope, scope_id, period, COALESCE(client,''))
        DO UPDATE SET used_usd = EXCLUDED.used_usd, last_updated = now()
        """
    )

    _SEED_SELECT_BEFORE = text(
        """
        SELECT used_usd FROM budget.budget_usages
        WHERE scope = CAST(:scope AS budget.budget_scope)
          AND scope_id = CAST(:scope_id AS uuid)
          AND period = :period
          AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
        """
    )

    async def seed_spent(
        self,
        session: AsyncSession,
        *,
        items: list[SeedSpentItem],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> SeedSpentResponse:
        """Overwrite-inject absolute spent (USD) per item into DB + Redis.

        Migration burn-rate continuation. Overwrite (not accumulate) → idempotent.
        Per-item partial failure: failed items reported, others proceed.
        """
        results: list[SeedSpentResult] = []
        user_repo = UserRepository(session)

        for item in items:
            scope = item.scope.upper()
            client = item.client
            res = SeedSpentResult(
                scope=scope, scope_id=item.scope_id, client=client,
                period=item.period, status="ok",
            )
            try:
                # --- validation ---
                if scope not in ("USER", "TEAM"):
                    raise ValueError(f"invalid scope: {item.scope}")
                if not _PERIOD_RE.match(item.period):
                    raise ValueError(f"invalid period format: {item.period} (expected YYYY-MM)")
                if client is not None:
                    if scope != "USER":
                        raise ValueError("client is only valid with scope=USER")
                    if client not in _ALLOWED_CLIENTS:
                        raise ValueError(f"invalid client: {client}")
                sid = uuid.UUID(item.scope_id)
                if scope == "USER":
                    if await user_repo.get_user(sid) is None:
                        raise ValueError("user not found")
                else:
                    if await user_repo.get_team(sid) is None:
                        raise ValueError("team not found")

                # --- before value ---
                before_row = await session.execute(
                    self._SEED_SELECT_BEFORE,
                    {"scope": scope, "scope_id": str(sid), "period": item.period, "client": client},
                )
                before = before_row.scalar_one_or_none()
                res.before_usd = Decimal(str(before)) if before is not None else Decimal("0")

                # --- DB upsert (overwrite) ---
                await session.execute(
                    self._SEED_UPSERT,
                    {"scope": scope, "scope_id": str(sid), "period": item.period,
                     "client": client, "spent": item.spent_usd},
                )

                # --- Redis SET (best-effort; DB is source of truth) ---
                redis_key = _redis_usage_key(scope, str(sid), item.period, client)
                try:
                    await self._cache_mgr._redis.set(redis_key, str(item.spent_usd))
                except Exception:
                    logger.warning("seed_spent.redis_set_failed", key=redis_key)

                # Per-app seed must also refresh the total key (client=None) so
                # enforcement sees the correct aggregate.
                if client is not None and scope == "USER":
                    from sqlalchemy import text as sa_text
                    total_row = await session.execute(
                        sa_text(
                            "SELECT COALESCE(SUM(used_usd), 0) "
                            "FROM budget.budget_usages "
                            "WHERE scope = 'USER' AND scope_id = :sid AND period = :period"
                        ),
                        {"sid": str(sid), "period": item.period},
                    )
                    total_val = total_row.scalar() or 0
                    total_key = _redis_usage_key(scope, str(sid), item.period, None)
                    try:
                        await self._cache_mgr._redis.set(total_key, str(total_val))
                    except Exception:
                        logger.warning("seed_spent.redis_total_refresh_failed", key=total_key)

                res.after_usd = item.spent_usd

                await audit_logger.log(
                    session,
                    actor_user_id=actor.user_id,
                    actor_role=actor.role.value,
                    action="SEED_BUDGET_SPENT",
                    resource_type="BudgetUsage",
                    resource_id=f"{scope}:{sid}:{client or ''}:{item.period}",
                    changes={"before": str(res.before_usd), "after": str(item.spent_usd)},
                    ip_address=ip_address,
                    request_id=request_id,
                )
            except Exception as exc:
                res.status = "error"
                res.error = str(exc)
            results.append(res)

        succeeded = sum(1 for r in results if r.status == "ok")
        return SeedSpentResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
        )

    async def get_budget_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        actor: CurrentUser,
    ) -> BudgetConfigDetailResponse:
        """다이얼로그 prefill 용 현재 총액 설정 — DB(config) + Redis(thresholds) 병합.

        ``alert_thresholds`` 는 Redis 만이 저장소라 키 미스/만료면 ``None`` 을
        돌린다 — 프론트는 그 경우 PUT 에서 키를 생략해 보존해야 하므로
        "기본값" 과 "값 불명" 을 구분해 줘야 한다.

        BR-BUD-03: 리더는 자기 팀/자기 팀 멤버만 읽는다 — 형제 읽기 경로
        (get_user_app_budgets, get_team_allocation)와 같은 스코핑.
        """
        if actor.role == UserRole.TEAM_LEADER:
            if scope == BudgetScope.TEAM:
                if scope_id != actor.team_id:
                    raise ForbiddenError(
                        "Team leaders can only read budgets for their own team"
                    )
            else:
                target = await UserRepository(session).get_user(scope_id)
                if target is None or actor.team_id is None or target.team_id != actor.team_id:
                    raise ForbiddenError(
                        "Team leaders can only read budgets for their own team members"
                    )

        cfg = await BudgetRepository(session).get_latest_config(scope, scope_id)
        if cfg is None or not cfg.is_active:
            return BudgetConfigDetailResponse(configured=False)
        cached = await read_budget_config(
            self._cache_mgr._redis,
            f"budget:config:{scope.value.lower()}:{{{scope_id}}}",
        )
        prev = cached.get("thresholds") if cached else None
        thresholds = (
            sorted(v for v in prev if isinstance(v, int))
            if isinstance(prev, list)
            else None
        )
        return BudgetConfigDetailResponse(
            configured=True,
            max_budget_usd=cfg.max_budget_usd,
            policy=BudgetPolicy(cfg.policy.value),
            alert_thresholds=thresholds,
            default_user_cap_usd=cfg.default_user_cap_usd,
        )

    async def _resolve_policy_thresholds(
        self,
        data: SetBudgetRequest,
        *,
        latest: BudgetConfig | None,
        config_key: str,
    ) -> tuple[BudgetPolicy, list[int]]:
        """policy/alert_thresholds 미전송 시 기존값을 보존한다.

        SetBudgetRequest 에 스키마 기본값이 있어 "관리자가 기본값을 골랐다" 와
        "키를 안 보냈다" 는 ``model_fields_set`` 으로만 구분된다 — 다이얼로그가
        금액만 바꿔 저장해도 enforcement 정책이 기본값으로 리셋되지 않게 한다.
        thresholds 의 기존값은 Redis(유일한 저장소)에서 읽고, 캐시 미스면
        스키마 기본값으로 돌아간다(게이트웨이도 키 미스 시 같은 기본값을 쓴다).
        """
        fields = data.model_fields_set
        if "policy" in fields:
            policy = BudgetPolicy(data.policy.value)
        else:
            policy = BudgetPolicy(latest.policy.value) if latest else BudgetPolicy.HARD_BLOCK
        if "alert_thresholds" in fields:
            thresholds = list(data.alert_thresholds)
        else:
            cached = await read_budget_config(self._cache_mgr._redis, config_key)
            prev = cached.get("thresholds") if cached else None
            thresholds = (
                [v for v in prev if isinstance(v, int)]
                if isinstance(prev, list) and prev
                else list(data.alert_thresholds)
            )
        return policy, thresholds

    async def set_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: SetBudgetRequest,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        _check_cent_precision(data.max_budget_usd)
        if data.default_user_cap_usd is not None:
            _check_cent_precision(data.default_user_cap_usd)

        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = BudgetRepository(session)

        # §3-1: T 편집은 D 를 건드리지 않는다 — 키가 안 오면 직전 행(비활성 포함,
        # 예산 해제 후 재설정 경로)의 D 를 그대로 이어 쓴다.
        latest = await repo.get_latest_config(BudgetScope.TEAM, team_id)
        if "default_user_cap_usd" in data.model_fields_set:
            new_cap = data.default_user_cap_usd
        else:
            new_cap = latest.default_user_cap_usd if latest else None
        old_cap = latest.default_user_cap_usd if latest else None

        policy, alert_thresholds = await self._resolve_policy_thresholds(
            data, latest=latest, config_key=f"budget:config:team:{{{team_id}}}"
        )

        # 확인 필요 사유를 모두 수집해 한 번에 409 로 돌린다 — 첫 409 에서 못 본
        # 경고가 confirm 재시도에서 조용히 통과되는 일이 없도록.
        period = current_kst_period()
        warnings: list[dict] = []

        # §3-1 감액 확인: team_used ≥ T_new 면 저장 즉시 팀 전원이 차단된다.
        team_used = await self._current_used(repo, BudgetScope.TEAM, team_id, period)
        if team_used >= data.max_budget_usd:
            warnings.append({
                "impact": "team_blocked",
                "team_used_usd": str(team_used),
                "new_budget_usd": str(data.max_budget_usd),
            })

        # §3-2: 이 요청으로 D 가 줄어들거나 새로 생기면, 개별 cap 없는 멤버 중
        # 사용량이 새 D 이상인 사람이 즉시 차단된다 → 확인 필요.
        if new_cap is not None and (old_cap is None or new_cap < old_cap):
            affected = await self._unset_members_over_cap(
                session, repo, team, cap=new_cap, period=period
            )
            if affected:
                warnings.append({
                    "impact": "default_cap_blocks_members",
                    "affected": affected,
                })

        if warnings and not confirm:
            raise ConfirmationRequiredError(
                "This change will block requests immediately after saving — "
                "review the impacts and re-submit with confirm=true.",
                details={"warnings": warnings},
            )

        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.TEAM,
            scope_id=team_id,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=policy,
            allocated_by=actor.user_id,
            effective_from=date.today(),
            default_user_cap_usd=new_cap,
            # ⚠️ migration 0041 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        await invalidate_after_commit(
            session, self._cache_mgr, [f"budget:config:team:{{{team_id}}}"]
        )

        # ⚠️ SET 은 커밋 후로 미룬다 — 커밋 전 SET 은 롤백 시 미커밋 T/D 를 TTL
        #    동안 광고한다(§6-6). alert_thresholds 는 Redis 가 유일한 운반체라
        #    DEL-only 는 불가라, 지연 SET 이 둘 다 지킨다.
        await defer_redis_write_until_commit(
            session,
            lambda: self._sync_redis_thresholds(
                "team", team_id,
                max_budget_usd=data.max_budget_usd,
                policy=policy,
                alert_thresholds=alert_thresholds,
                default_cap=new_cap,
            ),
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("SET_TEAM_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={"after": {"team_id": str(team_id), "max_budget_usd": str(data.max_budget_usd), "policy": policy.value, "alert_thresholds": alert_thresholds, "default_user_cap_usd": str(new_cap) if new_cap is not None else None}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_team_default_cap(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        value: Decimal | None,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> Decimal | None:
        """팀 기본 유저 cap D 만 변경한다(§3-2) — T·정책·thresholds 는 유지.

        T 가 아직 없으면(팀 config 행 없음) T=NULL '사전 준비' 행을 만든다(G-6).
        반환값은 확정된 D 다.
        """
        if value is not None:
            _check_cent_precision(value)

        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = BudgetRepository(session)
        cfg = await repo.get_active_config(BudgetScope.TEAM, team_id)
        old_cap = cfg.default_user_cap_usd if cfg else None
        if old_cap == value:
            # no-op 이어도 팀 캐시를 갱신 — stale/유실된 D 를 즉시 자가치유한다.
            await invalidate_after_commit(
                session, self._cache_mgr, [f"budget:config:team:{{{team_id}}}"]
            )
            return value

        period = current_kst_period()
        warnings: list[dict] = []
        if value is not None and (old_cap is None or value < old_cap):
            affected = await self._unset_members_over_cap(
                session, repo, team, cap=value, period=period
            )
            if affected:
                warnings.append({
                    "impact": "default_cap_blocks_members",
                    "affected": affected,
                })
        if warnings and not confirm:
            raise ConfirmationRequiredError(
                f"The new default user cap (${value}) will immediately block "
                f"members without an individual budget whose usage already meets "
                f"it — re-submit with confirm=true.",
                details={"warnings": warnings},
            )

        if cfg is None:
            # G-6: T=NULL 행 — 'D만 저장된 팀 config'. T=NULL 과 행 없음은
            # enforcement 에서 동일하게 team_budget_unset 으로 판정된다.
            cfg = BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.TEAM,
                scope_id=team_id,
                max_budget_usd=None,
                period_type=PeriodType.MONTHLY,
                policy=BudgetPolicy.HARD_BLOCK,
                allocated_by=actor.user_id,
                effective_from=date.today(),
                default_user_cap_usd=value,
                is_active=True,
            )
            await repo.upsert_config(cfg)
        else:
            cfg.default_user_cap_usd = value
            await session.flush()

        # 캐시는 DEL 만 — commit 전 SET 은 미커밋 D 를 광고하고(§6-6 위반),
        # T=NULL 행의 limit_usd:null 을 미리 쓰면 gateway 가 unset 판정을 하기
        # 전에 위험하다. miss 시 gateway 가 커밋된 DB 행(D 포함)으로 재수화한다.
        await invalidate_after_commit(
            session, self._cache_mgr, [f"budget:config:team:{{{team_id}}}"]
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("SET_TEAM_DEFAULT_CAP", confirm),
            resource_type="BudgetConfig",
            resource_id=str(cfg.id),
            changes={
                "before": {"default_user_cap_usd": str(old_cap) if old_cap is not None else None},
                "after": {"default_user_cap_usd": str(value) if value is not None else None},
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        return value

    async def delete_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """팀 예산 해제 — 팀 전원이 team_budget_unset 으로 차단된다(§3-1).

        A_u·app_c·D 는 보존된다 — 비활성 행에 남은 D 는 T 재설정 시
        get_latest_config 가 이어 받는다.
        """
        repo = BudgetRepository(session)
        cfg = await repo.get_active_config(BudgetScope.TEAM, team_id)
        if cfg is None:
            return

        if not confirm:
            raise ConfirmationRequiredError(
                "Removing the team budget will block ALL requests from this team "
                "(team_budget_unset) — re-submit with confirm=true.",
                details={
                    "warnings": [{
                        "impact": "team_blocked",
                        "team_id": str(team_id),
                    }]
                },
            )

        cfg.is_active = False
        await session.flush()

        await invalidate_after_commit(
            session, self._cache_mgr, [f"budget:config:team:{{{team_id}}}"]
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_TEAM_BUDGET_CONFIRMED",
            resource_type="BudgetConfig",
            resource_id=str(cfg.id),
            changes={
                "before": {
                    "team_id": str(team_id),
                    "max_budget_usd": str(cfg.max_budget_usd)
                    if cfg.max_budget_usd is not None else None,
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def equal_split_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        clear_individual: bool,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> dict:
        """균등분배 도우미 — D = floor_cent(T/N) 을 팀 행에 쓴다(§4-3/D-11).

        clear_individual=True 면 모든 개별 A_u·app_c 를 같은 트랜잭션에서
        제거하고 전원 D 로 통일한다. 반환: 계산된 D 와 초기화된 멤버 수.
        """
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        members = [m for m in team.members if m.is_active]
        n_members = len(members)

        repo = BudgetRepository(session)
        cfg = await repo.get_active_config(BudgetScope.TEAM, team_id)
        team_budget = cfg.max_budget_usd if cfg else None

        cap_d: Decimal | None = None
        if n_members > 0 and team_budget is not None and team_budget > 0:
            cap_d = (team_budget / n_members).quantize(_CENT, rounding=ROUND_DOWN)
        if cap_d is None or cap_d <= 0:
            raise BudgetRuleError(
                "Equal split is not possible — the team has no active members or "
                "no team budget is set (equal_split_nothing_to_allocate).",
                "equal_split_nothing_to_allocate",
            )

        period = current_kst_period()
        warnings: list[dict] = []
        if not confirm:
            if clear_individual:
                # 전원 D 로 통일 — D 이상 사용 중인 멤버 전원이 즉시 차단된다.
                member_ids = [m.id for m in members]
                usages = await repo.get_usages_for_scope_ids(
                    BudgetScope.USER, member_ids, period
                )
                affected = [
                    {
                        "user_id": str(m.id),
                        "name": m.display_name or m.email,
                        "used_usd": str(usages.get(m.id, Decimal("0"))),
                    }
                    for m in members
                    if usages.get(m.id, Decimal("0")) >= cap_d
                ]
            else:
                affected = await self._unset_members_over_cap(
                    session, repo, team, cap=cap_d, period=period
                )
            if affected:
                warnings.append({
                    "impact": "default_cap_blocks_members",
                    "affected": affected,
                })
            if clear_individual:
                configured = await repo.list_configured_member_ids(team_id)
                if configured & {m.id for m in members}:
                    warnings.append({
                        "impact": "individual_budgets_cleared",
                        "count": len(configured & {m.id for m in members}),
                    })
            if warnings:
                raise ConfirmationRequiredError(
                    f"Equal split sets the default cap to ${cap_d} "
                    f"(floor({team_budget} / {n_members})) — some members would "
                    f"be blocked or lose their individual budgets. Re-submit "
                    f"with confirm=true.",
                    details={"warnings": warnings, "computed_default_cap_usd": str(cap_d)},
                )

        # 여기까지 오면 cfg·team_budget 은 확정돼 있다(위 가드에서 T 없으면 종료).
        cleared_users = 0
        cleared_app_keys: list[str] = []
        if clear_individual:
            for m in members:
                await _lock_user_budget_key(session, m.id)
                # app 캐시 키는 deactivate 전에 수집 — rows 가 비면 무효화 대상이 없다.
                app_rows = await repo.list_active_app_configs(m.id)
                cleared_app_keys += [
                    f"budget:config:user:{{{m.id}}}:{r.client}" for r in app_rows
                ]
                removed = await repo.deactivate_configs(BudgetScope.USER, m.id)
                await repo.deactivate_app_configs_for_user(m.id)
                if removed or app_rows:
                    cleared_users += 1
                    cleared_app_keys.append(f"budget:config:user:{{{m.id}}}")
                    # per-app 키(`budget:config:user:{uid}:{client}`)는 이미
                    # cleared_app_keys 에 수집돼 아래 invalidate 가 DEL 한다 —
                    # 별도 _delete_redis_app_config 루프는 중복이라 제거.

        cfg.default_user_cap_usd = cap_d
        await session.flush()

        # DEL 만 — commit 전 SET 시 미커밋 D/clear 결과가 광고된다(§6-6).
        # miss → gateway 가 커밋된 DB 행으로 재수화.
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [f"budget:config:team:{{{team_id}}}", *cleared_app_keys],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("EQUAL_SPLIT_TEAM_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(cfg.id),
            changes={
                "after": {
                    "team_id": str(team_id),
                    "default_user_cap_usd": str(cap_d),
                    "member_count": n_members,
                    "clear_individual": clear_individual,
                    "cleared_users": cleared_users,
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        return {
            "default_user_cap_usd": str(cap_d),
            "member_count": n_members,
            "cleared_users": cleared_users,
        }

    async def set_user_budget(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        data: SetBudgetRequest,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        _check_cent_precision(data.max_budget_usd)

        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))

        # I-4/§4-6: 팀 소속이 없는 유저에게는 개인 예산을 설정할 수 없다.
        if user.team_id is None:
            raise BudgetRuleError(
                f"User {user_id} has no team — budgets can only be assigned to "
                f"team members (no_team_assigned at enforcement).",
                "user_not_in_team",
                403,
            )

        # BR-BUD-03: Team leader can only set budgets for own team
        if actor.role == UserRole.TEAM_LEADER:
            if actor.team_id is None or user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only set budgets for their own team members")
            # D-13/§4-1: 리더는 본인의 예산을 스스로 설정할 수 없다.
            if actor.user_id == user_id:
                raise BudgetRuleError(
                    "Team leaders cannot set their own budget "
                    "(self_allocation_forbidden) — an admin must do it.",
                    "self_allocation_forbidden",
                    403,
                )

        # 유저 단위 직렬화 — 총액·앱별 쓰기가 엇갈려 I-2(app_c ≤ A_u)가 깨지는 것을 막는다.
        await _lock_user_budget_key(session, user_id)

        repo = BudgetRepository(session)

        latest = await repo.get_latest_config(BudgetScope.USER, user_id)
        policy, alert_thresholds = await self._resolve_policy_thresholds(
            data, latest=latest, config_key=f"budget:config:user:{{{user_id}}}"
        )

        # D-4/I-2 거부: A_u_new < max(app_c) → 하위 앱 cap 이 부모를 넘는 모순.
        max_app = await repo.max_app_budget(user_id)
        if max_app is not None and data.max_budget_usd < max_app:
            raise BudgetRuleError(
                f"User budget (${data.max_budget_usd}) is below an existing app cap "
                f"(${max_app}) — lower the app budgets first (user_budget_below_app_cap).",
                "user_budget_below_app_cap",
            )

        # §4-2 확인: A_u_new ≤ used_u → 저장 즉시 해당 유저가 차단된다.
        if not confirm:
            used = await self._current_used(repo, BudgetScope.USER, user_id, current_kst_period())
            if used >= data.max_budget_usd:
                raise ConfirmationRequiredError(
                    f"User's current usage (${used}) already meets or exceeds the new "
                    f"budget (${data.max_budget_usd}) — this user will be blocked "
                    f"immediately.",
                    details={
                        "warnings": [{
                            "impact": "user_blocked",
                            "user_id": str(user_id),
                            "used_usd": str(used),
                            "new_budget_usd": str(data.max_budget_usd),
                        }]
                    },
                )

        # D-1: ΣA_u ≤ T 합계 검증은 없다 — CAP 모델은 초과 약정(overcommit)을
        # 허용하고 팀 총량 T 만 실질 상한으로 enforcement 한다.

        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.USER,
            scope_id=user_id,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=policy,
            allocated_by=actor.user_id,
            effective_from=date.today(),
            # ⚠️ migration 0041 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        await invalidate_after_commit(
            session, self._cache_mgr, [f"budget:config:user:{{{user_id}}}"]
        )

        await defer_redis_write_until_commit(
            session,
            lambda: self._sync_redis_thresholds(
                "user", user_id,
                max_budget_usd=data.max_budget_usd,
                policy=policy,
                alert_thresholds=alert_thresholds,
            ),
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("SET_USER_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={"after": {"user_id": str(user_id), "max_budget_usd": str(data.max_budget_usd), "policy": policy.value, "alert_thresholds": alert_thresholds}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def delete_user_budget(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """개별 cap A_u 해제 — 유저는 팀 기본 cap D 로 이행한다(§4-2).

        D=null 이면 팀 한도만 적용(제한 완화), D 있으면 used_u ≥ D 시 즉시 차단.
        하위 app_c 는 I-3 에 따라 연쇄 삭제된다.
        """
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if actor.role == UserRole.TEAM_LEADER:
            if actor.team_id is None or user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only delete budgets for their own team members")
            if actor.user_id == user_id:
                raise BudgetRuleError(
                    "Team leaders cannot delete their own budget (self_allocation_forbidden).",
                    "self_allocation_forbidden",
                    403,
                )

        await _lock_user_budget_key(session, user_id)

        repo = BudgetRepository(session)
        existing = await repo.get_active_config(BudgetScope.USER, user_id)
        if existing is None:
            return

        # I-3 cascade 대상 — 확인 다이얼로그에 목록으로 보여준다.
        app_rows = await repo.list_active_app_configs(user_id)

        team_cfg = await repo.get_active_config(BudgetScope.TEAM, user.team_id) if user.team_id else None
        cap_d = team_cfg.default_user_cap_usd if team_cfg else None

        warnings: list[dict] = []
        if app_rows:
            warnings.append({
                "impact": "app_budgets_cascaded",
                "clients": [r.client for r in app_rows],
            })
        if cap_d is not None:
            used = await self._current_used(repo, BudgetScope.USER, user_id, current_kst_period())
            if used >= cap_d:
                warnings.append({
                    "impact": "user_blocked_by_default_cap",
                    "used_usd": str(used),
                    "default_cap_usd": str(cap_d),
                })
        if warnings and not confirm:
            raise ConfirmationRequiredError(
                "Deleting the individual budget will cascade-delete the app budgets "
                "and/or block this user under the team default cap — "
                "re-submit with confirm=true.",
                details={"warnings": warnings},
            )

        existing.is_active = False
        cascaded = await repo.deactivate_app_configs_for_user(user_id)
        await session.flush()

        invalidate_keys = [f"budget:config:user:{{{user_id}}}"]
        invalidate_keys += [f"budget:config:user:{{{user_id}}}:{c}" for c in cascaded]
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            invalidate_keys,
            extra=[
                (lambda c=c: self._delete_redis_app_config(user_id, c))
                for c in cascaded
            ],
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("DELETE_USER_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(existing.id),
            changes={
                "before": {
                    "user_id": str(user_id),
                    "max_budget_usd": str(existing.max_budget_usd),
                    "cascaded_app_clients": cascaded,
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_user_client_budget(
        self,
        session,
        *,
        user_id: uuid.UUID,
        client: str,
        data: SetBudgetRequest,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """Set a per-app budget for (user_id, client).

        Guards:
          - client must be one of _ALLOWED_CLIENTS (claude-code / cowork / codex)
          - if the user has a non-empty allowed_clients list, client must be in it
          - I-3: parent USER total budget must exist
          - I-2: app_c <= A_u (부모 유저 총예산을 넘는 앱 cap 은 모순)
        """
        _check_cent_precision(data.max_budget_usd)

        if client not in _ALLOWED_CLIENTS:
            raise ValueError("invalid client")

        # Verify user exists before any further checks (fail-fast, avoids a
        # wasted allowed_clients query for a non-existent user).
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))

        # I-4/§4-6: 팀 없는 유저에는 app cap 도 설정 불가.
        if user.team_id is None:
            raise BudgetRuleError(
                f"User {user_id} has no team — budgets can only be assigned to "
                f"team members.",
                "user_not_in_team",
                403,
            )

        # BR-BUD-03 + D-13: 리더는 자기 팀 멤버만, 본인은 제외.
        if actor.role == UserRole.TEAM_LEADER:
            if actor.team_id is None or user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only set budgets for their own team members")
            if actor.user_id == user_id:
                raise BudgetRuleError(
                    "Team leaders cannot set their own budget (self_allocation_forbidden).",
                    "self_allocation_forbidden",
                    403,
                )

        from app.services.user_allowed_client_service import UserAllowedClientService

        allowed = await UserAllowedClientService(session).get(user_id)
        if allowed and client not in allowed:
            raise ValueError(f"client '{client}' not allowed for this user")

        await _lock_user_budget_key(session, user_id)

        repo = BudgetRepository(session)

        # I-3 (P0-③ invariant): per-app 예산은 USER 총액 예산의 하위 cap 이다.
        # 부모가 없으면 gateway hot path 의 app_clients 게이트가 부모 config 를
        # 읽어 조용히 우회된다 → 거부.
        parent = await repo.get_active_config(BudgetScope.USER, user_id)
        if parent is None or parent.max_budget_usd is None:
            raise BudgetRuleError(
                "Cannot set a per-app budget before the user's total budget is set "
                "(per-app budget is a sub-limit of the user total).",
                "app_budget_requires_user_budget",
            )

        # I-2: app_c > A_u → 거부. 같은 유저 키를 잠근 뒤 검증하므로 동시에
        # A_u 가 줄어드는 경합은 없다(§3-0).
        if data.max_budget_usd > parent.max_budget_usd:
            raise BudgetRuleError(
                f"App cap (${data.max_budget_usd}) exceeds the user's total budget "
                f"(${parent.max_budget_usd}) (app_cap_exceeds_user_budget).",
                "app_cap_exceeds_user_budget",
            )

        # §5 확인: app_c_new ≤ app_used → 저장 즉시 그 앱이 차단된다.
        if not confirm:
            app_used = await self._current_used(
                repo, BudgetScope.USER, user_id, current_kst_period(), client=client
            )
            if app_used >= data.max_budget_usd:
                raise ConfirmationRequiredError(
                    f"This app's usage (${app_used}) already meets or exceeds the new "
                    f"cap (${data.max_budget_usd}) — '{client}' will be blocked "
                    f"immediately for this user.",
                    details={
                        "warnings": [{
                            "impact": "app_blocked",
                            "client": client,
                            "used_usd": str(app_used),
                            "new_budget_usd": str(data.max_budget_usd),
                        }]
                    },
                )

        # 총액과 동일한 보존 규칙 — 앱 cap 금액만 바꿔도 정책이 기본값으로 리셋되지 않게.
        latest_app = await repo.get_first_active_app_config(BudgetScope.USER, user_id, client)
        policy, alert_thresholds = await self._resolve_policy_thresholds(
            data,
            latest=latest_app,
            config_key=f"budget:config:user:{{{user_id}}}:{client}",
        )

        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.USER,
            scope_id=user_id,
            client=client,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=policy,
            allocated_by=actor.user_id,
            effective_from=date.today(),
            # ⚠️ migration 0041 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        # P0-③ durability: DURABLY invalidate (DEL via retry infra → recorded to
        # cache_invalidation_failures on failure) BOTH the per-app config key AND
        # the parent user-config key (whose app_clients list just changed). The
        # subsequent _sync_redis_app_config/_write_user_app_clients SETs are
        # best-effort cache-warmers ONLY, deferred until commit — a rollback
        # never advertises uncommitted budgets, and if they fail the durable DEL
        # guarantees the gateway sees a miss and rehydrates from DB
        # (ensure_config_cached), rather than enforcing a stale app_clients that
        # silently bypasses the per-app limit forever.
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [
                f"budget:config:user:{{{user_id}}}:{client}",
                f"budget:config:user:{{{user_id}}}",
            ],
        )

        await defer_redis_write_until_commit(
            session,
            lambda: self._sync_redis_app_config(
                user_id, client,
                max_budget_usd=data.max_budget_usd,
                policy=policy,
                alert_thresholds=alert_thresholds,
            ),
        )
        # DB 읽기는 트랜잭션 안에서 — 지연 쓰기는 Redis 만 건드린다.
        active_clients = await repo.list_active_app_clients(user_id)
        await defer_redis_write_until_commit(
            session,
            lambda: self._write_user_app_clients(user_id, active_clients),
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("SET_USER_CLIENT_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={
                "after": {
                    "user_id": str(user_id),
                    "client": client,
                    "max_budget_usd": str(data.max_budget_usd),
                    "policy": policy.value,
                    "alert_thresholds": alert_thresholds,
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def clear_user_client_budget(
        self,
        session,
        *,
        user_id: uuid.UUID,
        client: str,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """Deactivate the per-app budget for (user_id, client) and clean up Redis."""
        if client not in _ALLOWED_CLIENTS:
            raise ValueError("invalid client")

        # BR-BUD-03 + D-13: Team leaders can only clear budgets for their own
        # team members, never their own.
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if actor.role == UserRole.TEAM_LEADER:
            if actor.team_id is None or user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only clear budgets for their own team members")
            if actor.user_id == user_id:
                raise BudgetRuleError(
                    "Team leaders cannot delete their own budget (self_allocation_forbidden).",
                    "self_allocation_forbidden",
                    403,
                )

        repo = BudgetRepository(session)
        existing = await repo.get_active_app_config(BudgetScope.USER, user_id, client)
        if existing is None:
            return

        existing.is_active = False
        await session.flush()

        # P0-③ durability: durably DEL both the per-app key and the parent
        # user-config key (app_clients list shrank). See set_user_client_budget.
        await invalidate_after_commit(
            session,
            self._cache_mgr,
            [
                f"budget:config:user:{{{user_id}}}:{client}",
                f"budget:config:user:{{{user_id}}}",
            ],
        )

        # per-app 키는 위 invalidate 목록(`budget:config:user:{uid}:{client}`)에
        # 이미 포함 — 별도 _delete_redis_app_config 호출은 중복이라 제거.
        active_clients = await repo.list_active_app_clients(user_id)
        await defer_redis_write_until_commit(
            session,
            lambda: self._write_user_app_clients(user_id, active_clients),
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CLEAR_USER_CLIENT_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(existing.id),
            changes={
                "before": {
                    "user_id": str(user_id),
                    "client": client,
                    "max_budget_usd": str(existing.max_budget_usd),
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_user_app_budgets(self, session, *, user_id: uuid.UUID, actor) -> list[dict]:
        """Return active per-app budget configs for a user (read-only, for UI prefill).

        BR-BUD-03: team leaders may only read budgets for their own team members.
        """
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if actor.role == UserRole.TEAM_LEADER and (actor.team_id is None or user.team_id != actor.team_id):
            raise ForbiddenError("Team leaders can only read budgets for their own team members")

        repo = BudgetRepository(session)
        out = []
        for client in _ALLOWED_CLIENTS:
            cfg = await repo.get_first_active_app_config(BudgetScope.USER, user_id, client)
            if cfg is not None:
                out.append({
                    "client": client,
                    "max_budget_usd": cfg.max_budget_usd,
                    "policy": cfg.policy,  # DB enum; pydantic coerces via BudgetPolicy
                })
        return out

    async def allocate_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: AllocateBudgetRequest,
        actor: CurrentUser,
        confirm: bool = False,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        # BR-BUD-03: Team leader can only allocate within own team
        if actor.role == UserRole.TEAM_LEADER and actor.team_id != team_id:
            raise ForbiddenError("Team leaders can only allocate budgets within their own team")

        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = BudgetRepository(session)
        team_config = await repo.get_active_config(BudgetScope.TEAM, team_id)
        if team_config is None or team_config.max_budget_usd is None:
            raise ValidationError("Team budget must be set before allocation")

        # 한 배치 안의 중복 user_id — 마지막 값이 이기는 order-dependent 결과를
        # 만들고 audit 에 두 행이 남으므로 전부 거부한다(all-or-nothing).
        seen_ids: set[str] = set()
        for alloc in data.allocations:
            if alloc.user_id in seen_ids:
                raise BudgetRuleError(
                    f"Duplicate user_id {alloc.user_id} in allocation batch "
                    f"(duplicate_allocation).",
                    "duplicate_allocation",
                )
            seen_ids.add(alloc.user_id)

        # D-2/I-4: 배정 대상은 전부 해당 팀의 **현재** 멤버여야 한다.
        member_ids = {m.id for m in team.members}
        for alloc in data.allocations:
            _check_cent_precision(alloc.allocated_usd)
            uid = uuid.UUID(alloc.user_id)
            if uid not in member_ids:
                raise BudgetRuleError(
                    f"User {uid} is not a member of team {team_id} "
                    f"(user_not_in_team).",
                    "user_not_in_team",
                    403,
                )
            # D-13: 리더는 자기 예산을 배정 목록에 넣을 수 없다.
            if actor.role == UserRole.TEAM_LEADER and uid == actor.user_id:
                raise BudgetRuleError(
                    "Team leaders cannot allocate a budget to themselves "
                    "(self_allocation_forbidden).",
                    "self_allocation_forbidden",
                    403,
                )

        # D-1: ΣA_u ≤ T 합계 검증은 없다 — CAP 모델은 초과 약정을 허용한다.

        # 데드락 방지: 유저 키를 정렬된 순서로 잠근다.
        sorted_allocs = sorted(data.allocations, key=lambda a: a.user_id)
        for alloc in sorted_allocs:
            await _lock_user_budget_key(session, uuid.UUID(alloc.user_id))

        # 배치를 쓰기 전에 전수 검증한다 — 부분 성공(일부 유저만 새 cap)은
        # 관리자가 보는 상태와 DB 가 어긋나므로 허용하지 않는다.
        period = current_kst_period()
        warnings: list[dict] = []
        for alloc in sorted_allocs:
            uid = uuid.UUID(alloc.user_id)
            # I-2: 새 A_u 가 기존 app_c 보다 낮아지면 모순 → 배치 전체 거부.
            max_app = await repo.max_app_budget(uid)
            if max_app is not None and alloc.allocated_usd < max_app:
                raise BudgetRuleError(
                    f"Allocation for user {uid} (${alloc.allocated_usd}) is below "
                    f"an existing app cap (${max_app}) "
                    f"(user_budget_below_app_cap).",
                    "user_budget_below_app_cap",
                )
            used = await self._current_used(repo, BudgetScope.USER, uid, period)
            if used >= alloc.allocated_usd:
                warnings.append({
                    "impact": "user_blocked",
                    "user_id": str(uid),
                    "used_usd": str(used),
                    "new_budget_usd": str(alloc.allocated_usd),
                })

        if warnings and not confirm:
            raise ConfirmationRequiredError(
                f"{len(warnings)} member(s) would be blocked immediately by the "
                f"new allocations — re-submit with confirm=true.",
                details={"warnings": warnings},
            )

        # Batch upsert user budgets
        cache_keys: list[str] = []
        for alloc in sorted_allocs:
            uid = uuid.UUID(alloc.user_id)
            config = BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.USER,
                scope_id=uid,
                max_budget_usd=alloc.allocated_usd,
                period_type=PeriodType.MONTHLY,
                policy=team_config.policy,
                allocated_by=actor.user_id,
                effective_from=date.today(),
                # 팀에서 파생된 사용자 예산은 팀의 임계값을 물려받는다 — 여기서 기본값을
                # 다시 쓰면 팀 설정과 어긋난 알림이 나간다.
                alert_thresholds=list(team_config.alert_thresholds or []),
                is_active=True,
            )
            await repo.upsert_config(config)
            cache_keys.append(f"budget:config:user:{{{uid}}}")

        await invalidate_after_commit(session, self._cache_mgr, cache_keys)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=_confirmed_action("ALLOCATE_TEAM_BUDGET", confirm),
            resource_type="BudgetConfig",
            resource_id=str(team_id),
            changes={"after": {"allocations": [a.model_dump() for a in data.allocations]}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_team_allocation(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        period: str,
        actor: CurrentUser,
    ) -> TeamBudgetAllocation | None:
        # 소속 팀만 열람 가능 — 이전엔 actor 검사가 없어 임의 team_id로
        # 타 팀 배정 현황을 읽을 수 있었다(IDOR).
        if actor.role == UserRole.TEAM_LEADER and team_id != actor.team_id:
            raise ForbiddenError("Team leaders can only read allocations for teams they lead")

        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            return None

        repo = BudgetRepository(session)
        team_config = await repo.get_first_active_config(BudgetScope.TEAM, team_id)
        total_budget = (
            team_config.max_budget_usd
            if team_config and team_config.max_budget_usd is not None
            else Decimal("0")
        )
        cap_d = team_config.default_user_cap_usd if team_config else None

        # Team-level entry
        team_usage = await repo.get_usage(BudgetScope.TEAM, team_id, period)
        team_used = team_usage.used_usd if team_usage else Decimal("0")
        team_remaining = total_budget - team_used
        team_pct = (team_used / total_budget * 100) if total_budget > 0 else Decimal("0")

        def _alert_level(pct: Decimal) -> str:
            if pct >= 90:
                return "CRITICAL"
            if pct >= 70:
                return "WARNING"
            return "NORMAL"

        entries: list[AllocationEntry] = [
            AllocationEntry(
                target_id=str(team_id),
                target_name=_team_display_name(team),
                target_type="TEAM",
                allocated_usd=total_budget,
                used_usd=team_used,
                remaining_usd=team_remaining,
                alert_level=_alert_level(team_pct),
            )
        ]

        # Member entries — cap_u = A_u ?? D. 개별 cap 없는 멤버는 D(없으면 팀 한도만).
        sum_allocated = Decimal("0")
        n_unset = 0  # 개별 cap 없이 D 를 쓰는 멤버 수 — overcommit 공식의 D×N 항.
        for member in team.members:
            member_config = await repo.get_first_active_config(BudgetScope.USER, member.id)
            member_usage = await repo.get_usage(BudgetScope.USER, member.id, period)
            member_used = member_usage.used_usd if member_usage else Decimal("0")

            if member_config is not None:
                cap_source = "individual"
                effective_cap = member_config.max_budget_usd
                sum_allocated += member_config.max_budget_usd
            elif cap_d is not None:
                cap_source = "team_default"
                effective_cap = cap_d
                n_unset += 1
            else:
                cap_source = None
                effective_cap = None

            member_alloc = effective_cap if effective_cap is not None else Decimal("0")
            member_remaining = member_alloc - member_used
            member_pct = (member_used / member_alloc * 100) if member_alloc > 0 else Decimal("0")
            entries.append(
                AllocationEntry(
                    target_id=str(member.id),
                    target_name=member.display_name,
                    target_type="USER",
                    target_role=member.role.value if member.role else None,
                    allocated_usd=member_alloc,
                    used_usd=member_used,
                    remaining_usd=member_remaining,
                    alert_level=_alert_level(member_pct),
                    cap_source=cap_source,
                    effective_cap_usd=effective_cap,
                )
            )

        # §0: 초과 약정률 = (ΣA_u + D × N_미설정) / T — D 로 커버되는 멤버의
        # 잠재 cap 도 약정 분자에 포함해야 대시보드 수치가 과소표시되지 않는다.
        committed = sum_allocated + (cap_d * n_unset if cap_d is not None else Decimal("0"))
        overcommit_ratio = (committed / total_budget) if total_budget > 0 else None
        return TeamBudgetAllocation(
            team_id=str(team_id),
            team_name=_team_display_name(team),
            total_budget_usd=total_budget,
            entries=entries,
            default_user_cap_usd=cap_d,
            sum_allocated_usd=sum_allocated,
            overcommit_ratio=overcommit_ratio,
        )

    async def get_budget_summary(
        self,
        session: AsyncSession,
        *,
        redis=None,
        scope: str | None = None,
        target_id: uuid.UUID | None = None,
        period: str,
        # ⚠️ 필수 인자다. 기본값 None 을 두면 actor 를 빠뜨린 호출이 아래 TEAM_LEADER
        #    필터를 건너뛰어 전사 예산·사용액을 그대로 돌려준다(fail-open).
        actor: CurrentUser,
    ) -> BudgetSummaryResponse:
        if not re.match(r'^\d{4}-\d{2}$', period):
            raise ValidationError(f"Invalid period format: {period}. Expected YYYY-MM")

        repo = BudgetRepository(session)
        budget_scope = BudgetScope(scope.upper()) if scope else None

        # Active configs indexed by (scope, scope_id_str). Users/teams without an
        # active config are still listed (limit=0) so admins can set/re-set budgets
        # — e.g., right after transfer_user deactivates the user-scope config.
        active_configs = await repo.list_configs(scope=budget_scope, scope_id=target_id)
        cfg_by_target: dict[tuple[BudgetScope, str], BudgetConfig] = {
            (cfg.scope, str(cfg.scope_id)): cfg for cfg in active_configs
        }

        from app.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        # ⚠️ limit=500 이었다. `list_users` 는 created_at desc 로 정렬한 뒤 앞에서
        #    자르므로, 가입이 오래된 사용자의 예산 행이 **조용히 빠진 채** 사용률이
        #    계산됐다(오류 없이 틀린 비율). 커서 페이징으로 우회할 수도 없다 —
        #    정렬 키(created_at)와 커서 키(id)가 달라 행을 건너뛴다. 전수 조회를 쓴다.
        users = await user_repo.iter_all_users()
        teams = await user_repo.list_all_teams()

        # TEAM_LEADER 는 소속 팀만 — scope/target_id 쿼리 파라미터로 다른
        # 팀을 넘겨도 무시한다(analytics_service.py 의 동일 정책과 일관). ADMIN 은 무제한.
        if actor.role == UserRole.TEAM_LEADER:
            led = {actor.team_id} if actor.team_id else set()
            teams = [t for t in teams if t.id in led]
            users = [u for u in users if u.team_id in led]

        # team_id(str) → (dept_id, dept_name). teams are loaded with
        # selectinload(Team.department), so this needs no extra query.
        dept_by_team: dict[str, tuple[str, str]] = {
            str(t.id): (str(t.department.id), t.department.name)
            for t in teams
            if getattr(t, "department", None) is not None
        }

        target_id_str = str(target_id) if target_id else None

        # 예산 설정(BudgetConfig) 유무와 무관하게 실사용액은 항상 계산해야 한다.
        # 이전엔 cfg 가 없으면(예: 팀 예산만 적용받는 사용자) used=0 으로 하드코딩돼
        # 실제 usage_logs 비용이 있어도 "$0.00" 로 표시되는 버그가 있었다.
        #
        # 1) Redis enforcement 카운터 — 대상 전체를 MGET 한 번으로 조회한다.
        #    대상마다 개별 GET 하면 round-trip 이 표시 대상 수만큼 쌓인다.
        #    0/미스는 "아직 증가 안 함"과 구분이 안 되므로 신뢰하지 않고
        # 2) usage_logs 일괄 그룹집계로 채운다 — 표시 대상만 IN 으로 좁힌다
        #    (팀 리더는 위에서 소속 팀/멤버로 이미 좁혀져 있다).
        user_sids = [
            str(u.id) for u in users
            if (budget_scope is None or budget_scope == BudgetScope.USER)
            and (not target_id_str or str(u.id) == target_id_str)
        ]
        team_sids = [
            str(t.id) for t in teams
            if (budget_scope is None or budget_scope == BudgetScope.TEAM)
            and (not target_id_str or str(t.id) == target_id_str)
        ]
        key_pairs = (
            [(BudgetScope.USER, sid) for sid in user_sids]
            + [(BudgetScope.TEAM, sid) for sid in team_sids]
        )

        used_by_key: dict[tuple[BudgetScope, str], Decimal] = {}
        if redis is not None and key_pairs:
            try:
                # 카운터 키의 해시태그가 sid 마다 달라 슬롯이 흩어진다 — prod 는
                # RedisCluster 라 mget 은 CROSSSLOT 으로 실패해 클러스터는
                # non-atomic fan-out 으로 간다(결과 순서·개수는 mget 과 동일).
                keys = [
                    f"budget:{sc.value.lower()}:{{{sid}}}:{period}"
                    for sc, sid in key_pairs
                ]
                if isinstance(redis, RedisCluster):
                    raws = await redis.mget_nonatomic(keys)
                else:
                    raws = await redis.mget(keys)
                for (sc, sid), raw in zip(key_pairs, raws):
                    if raw:
                        val = Decimal(raw.decode() if isinstance(raw, bytes) else raw)
                        if val != 0:
                            used_by_key[(sc, sid)] = val
            except Exception:
                logger.warning(
                    "budget_summary.redis_read_failed", exc_info=True,
                    hint="사용액을 usage_logs 집계로 대체합니다",
                )
                used_by_key = {}

        # Redis 미스분만 usage_logs 에서 그룹집계(N+1·대상 단위 쿼리 방지).
        # 비용 집계 표준(§59): SUCCESS 만 + KST 월 경계. 대시보드 Top 사용자/팀·
        # chat 과 동일 기준으로 통일(실패 호출 비용 제외, UTC 9시간 오차 제거).
        # TEAM_LEADER 는 표시 대상이 소속 팀/멤버뿐이라 IN 으로 좁힌다. ADMIN 은
        # 표시 대상이 전사라 전체 집계가 기본이지만, target 지정 요청처럼 미스
        # 대상이 소수일 때는 IN 이 훨씬 싸다 — 그 경우에도 좁힌다.
        narrow = actor.role == UserRole.TEAM_LEADER
        user_miss = [uuid.UUID(s) for s in user_sids if (BudgetScope.USER, s) not in used_by_key]
        team_miss = [uuid.UUID(s) for s in team_sids if (BudgetScope.TEAM, s) not in used_by_key]

        async def _aggregate(scope_enum: BudgetScope, col, miss: list[uuid.UUID]) -> None:
            if not miss:
                return  # Redis 가 표시 대상 전부 커버 — SQL 불필요
            conds = [cost_period_filter(period)]
            if narrow or len(miss) <= 500:
                conds.append(col.in_(miss))
            rows = (
                await session.execute(
                    sa_select(col, func.coalesce(func.sum(UsageLog.cost_usd), 0))
                    .where(*conds)
                    .group_by(col)
                )
            ).all()
            used_by_key.update(
                {
                    (scope_enum, str(sid)): Decimal(str(cost))
                    for sid, cost in rows
                    if sid is not None and (scope_enum, str(sid)) not in used_by_key
                }
            )

        await _aggregate(BudgetScope.USER, UsageLog.user_id, user_miss)
        await _aggregate(BudgetScope.TEAM, UsageLog.team_id, team_miss)

        # TEAM 행의 다운그레이드 배지 — "최신 저장 배치" 규칙 수 + 활성 여부.
        # get_current_rules 와 같은 기준(max created_at 배치, is_active 무관)이라
        # 꺼진 규칙도 펼친 패널과 배지가 일치한다. 팀당 1행 그룹집계(N+1 없음).
        # 표시 대상 팀이 없으면(scope=user 등) 쿼리 자체를 건너뛴다.
        downgrade_by_team: dict[str, tuple[int, bool]] = {}
        if team_sids:
            latest_batch = (
                sa_select(
                    DowngradePolicy.scope_id,
                    func.max(DowngradePolicy.created_at).label("latest"),
                )
                .where(DowngradePolicy.scope == BudgetScope.TEAM)
                .group_by(DowngradePolicy.scope_id)
                .subquery()
            )
            downgrade_rows = (
                await session.execute(
                    sa_select(
                        DowngradePolicy.scope_id,
                        func.count().label("cnt"),
                        func.bool_or(DowngradePolicy.is_active).label("enabled"),
                    )
                    .join(
                        latest_batch,
                        (DowngradePolicy.scope_id == latest_batch.c.scope_id)
                        & (DowngradePolicy.created_at == latest_batch.c.latest),
                    )
                    .where(DowngradePolicy.scope == BudgetScope.TEAM)
                    .group_by(DowngradePolicy.scope_id)
                )
            ).all()
            downgrade_by_team = {
                str(sid): (cnt, bool(enabled)) for sid, cnt, enabled in downgrade_rows
            }

        items: list[BudgetSummaryItem] = []

        def _append(
            scope_enum: BudgetScope,
            sid: str,
            name: str,
            team_id: str | None = None,
            is_active: bool = True,
            department_id: str | None = None,
            department_name: str | None = None,
        ) -> None:
            cfg = cfg_by_target.get((scope_enum, sid))
            used = used_by_key.get((scope_enum, sid), Decimal("0"))
            cap_source: str | None = None
            default_cap: Decimal | None = None
            if scope_enum == BudgetScope.TEAM:
                default_cap = cfg.default_user_cap_usd if cfg else None
            thresholds = list(cfg.alert_thresholds or []) if cfg is not None else None
            if cfg is not None and cfg.max_budget_usd is not None:
                limit = cfg.max_budget_usd
                remaining = limit - used
                pct = (used / limit * 100) if limit > 0 else Decimal("0")
                if scope_enum == BudgetScope.USER:
                    cap_source = "individual"
            elif scope_enum == BudgetScope.USER and team_id:
                # 개별 cap 없음 — 팀 기본 cap D 가 있으면 그게 실효 cap 이다.
                team_cfg = cfg_by_target.get((BudgetScope.TEAM, team_id))
                team_d = team_cfg.default_user_cap_usd if team_cfg else None
                if team_d is not None:
                    limit = team_d
                    remaining = limit - used
                    pct = (used / limit * 100) if limit > 0 else Decimal("0")
                    cap_source = "team_default"
                    thresholds = list(team_cfg.alert_thresholds or [])
                else:
                    limit = None
                    remaining = None
                    pct = None
            else:
                limit = None
                remaining = None
                pct = None
            items.append(
                BudgetSummaryItem(
                    target_type=scope_enum.value.lower(),
                    target_id=sid,
                    target_name=name,
                    team_id=team_id,
                    is_active=is_active,
                    limit_usd=limit,
                    used_usd=used,
                    remaining_usd=remaining,
                    usage_pct=pct,
                    department_id=department_id,
                    department_name=department_name,
                    default_user_cap_usd=default_cap,
                    cap_source=cap_source,
                    alert_thresholds=thresholds,
                    **(
                        {
                            "downgrade_rule_count": downgrade_by_team[sid][0],
                            "downgrade_enabled": downgrade_by_team[sid][1],
                        }
                        if scope_enum == BudgetScope.TEAM and sid in downgrade_by_team
                        else {}
                    ),
                )
            )

        if budget_scope is None or budget_scope == BudgetScope.USER:
            for u in users:
                uid = str(u.id)
                if target_id_str and uid != target_id_str:
                    continue
                u_team_id = getattr(u, "team_id", None)
                u_dept = dept_by_team.get(str(u_team_id)) if u_team_id else None
                _append(
                    BudgetScope.USER,
                    uid,
                    u.display_name or u.email,
                    team_id=str(u_team_id) if u_team_id else None,
                    is_active=u.is_active,
                    department_id=u_dept[0] if u_dept else None,
                    department_name=u_dept[1] if u_dept else None,
                )

        if budget_scope is None or budget_scope == BudgetScope.TEAM:
            for t in teams:
                tid = str(t.id)
                if target_id_str and tid != target_id_str:
                    continue
                has_active_members = any(m.is_active for m in t.members)
                t_dept = dept_by_team.get(tid)
                _append(
                    BudgetScope.TEAM,
                    tid,
                    _team_display_name(t),
                    is_active=has_active_members,
                    department_id=t_dept[0] if t_dept else None,
                    department_name=t_dept[1] if t_dept else None,
                )

        return BudgetSummaryResponse(period=period, summary=items)

    async def _current_used(
        self,
        repo: BudgetRepository,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        period: str,
        client: str | None = None,
    ) -> Decimal:
        """월 사용량 — Redis(enforcement 카운터, 최신) 우선, DB fallback.

        확인 판정(used ≥ new cap)은 hot path 와 같은 카운터를 봐야 한다 —
        budget_usages 는 워커 배치 반영이라 수 초 뒤처질 수 있다.
        """
        scope_type = scope.value.lower()
        redis = self._cache_mgr._redis
        try:
            key = _redis_usage_key(scope_type, str(scope_id), period, client)
            raw = await redis.get(key)
            if raw is not None:
                return Decimal(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
        if client is None:
            usage = await repo.get_usage(scope, scope_id, period)
            return usage.used_usd if usage else Decimal("0")
        return await repo.get_app_usage(scope, scope_id, period, client)

    async def _unset_members_over_cap(
        self,
        session: AsyncSession,
        repo: BudgetRepository,
        team: Team,
        *,
        cap: Decimal,
        period: str,
    ) -> list[dict]:
        """개별 cap(A_u)이 없는 활성 멤버 중 월 사용량 ≥ cap 인 목록 — 확인 다이얼로그용."""
        configured = await repo.list_configured_member_ids(team.id)
        unset_members = [
            m for m in team.members if m.is_active and m.id not in configured
        ]
        if not unset_members:
            return []
        usages = await repo.get_usages_for_scope_ids(
            BudgetScope.USER, [m.id for m in unset_members], period
        )
        return [
            {
                "user_id": str(m.id),
                "name": m.display_name or m.email,
                "used_usd": str(usages.get(m.id, Decimal("0"))),
            }
            for m in unset_members
            if usages.get(m.id, Decimal("0")) >= cap
        ]

    async def _write_team_config_cache(
        self,
        scope_id: uuid.UUID,
        max_budget_usd: Decimal | None,
        policy: BudgetPolicy,
        alert_thresholds: list[int],
        default_cap: Decimal | None = None,
    ) -> None:
        """budget:config:team:{<scope_id>} 를 Redis에 SET.

        budget_check.lua 가 기대하는 JSON shape:
          limit_usd, policy (lowercase), thresholds,
          default_user_cap_usd (팀 기본 유저 cap D — 미설정 멤버 cap, §3-2)
        Lua / gateway-proxy 기본값(soft_limit_pct, throttle_rpm_pct)은
        DB 스키마에 없으므로 Python 기본값은 포함하지 않음 — Lua 내 기본값 사용.
        """
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:team:{{{scope_id}}}"
            config_data = {
                "limit_usd": str(max_budget_usd) if max_budget_usd is not None else None,
                "policy": policy.value.lower(),
                "thresholds": sorted(alert_thresholds),
                "default_user_cap_usd": str(default_cap) if default_cap is not None else None,
            }
            await redis.set(config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL)
        except Exception:
            logger.warning("redis_team_config_cache_write_failed", scope_id=str(scope_id))

    async def _sync_redis_thresholds(
        self, scope_type: str, scope_id: uuid.UUID,
        *,
        max_budget_usd: Decimal,
        policy: BudgetPolicy,
        alert_thresholds: list[int],
        default_cap: Decimal | None = None,
    ) -> None:
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:{scope_type}:{{{scope_id}}}"
            # budget_check.lua / budget_deduct.lua compare policy against lowercase
            # constants ('hard_block', 'soft_warning', 'throttle'). admin-api's
            # BudgetPolicy enum stores UPPERCASE values; convert here so enforcement
            # actually fires on the Redis fast path. Other admin-api paths that
            # write this key (cli_service, internal.py) already use .lower().
            config_data = {
                "limit_usd": str(max_budget_usd),
                "policy": policy.value.lower(),
                "thresholds": sorted(alert_thresholds),
            }
            if scope_type == "team":
                # team 캐시에만 D 를 싣는다 — gateway 가 미설정 멤버 cap_u 를
                # 읽는 유일한 경로(§6-6).
                config_data["default_user_cap_usd"] = (
                    str(default_cap) if default_cap is not None else None
                )
            # ⚠️ app_clients 보존을 **애플리케이션에서** GET-modify-SET 으로 하면 안 된다.
            #    `await redis.get` 이 이벤트 루프를 양보하므로 uvicorn 워커 하나 안에서도
            #    두 요청(set_user_budget / set_user_client_budget /
            #    clear_user_client_budget)이 교차하고, 나중에 SET 하는 쪽이 상대의 필드를
            #    지운다. 그리고 그 손실은 조용하다 — budget_check.lua 는 없는 필드를
            #    빈 테이블로 읽고, 게이트웨이는 앱별 예산 평가를 통째로 건너뛴다.
            #    Lua 로 Redis 안에서 병합한다(core/budget_cache.py).
            if scope_type == "user":
                await write_user_budget_config(
                    redis, scope_id, config_data, BUDGET_CONFIG_CACHE_TTL
                )
            else:
                await redis.set(
                    config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL
                )
        except Exception:
            logger.warning("redis_threshold_sync_failed", scope_type=scope_type, scope_id=str(scope_id))

    async def _sync_redis_app_config(
        self, user_id: uuid.UUID, client: str,
        *,
        max_budget_usd: Decimal,
        policy: BudgetPolicy,
        alert_thresholds: list[int],
    ) -> None:
        """Write the per-app Redis config key that the gateway Lua reads.

        Key: budget:config:user:{<user_id>}:{client}
        Shape: {limit_usd, policy (lowercase), thresholds}
        """
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            config_data = {
                "limit_usd": str(max_budget_usd),
                "policy": policy.value.lower(),
                "thresholds": sorted(alert_thresholds),
            }
            await redis.set(config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL)
        except Exception:
            logger.warning(
                "redis_app_config_sync_failed", user_id=str(user_id), client=client
            )

    async def _delete_redis_app_config(self, user_id: uuid.UUID, client: str) -> None:
        """Delete the per-app Redis config key (on clear)."""
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            await redis.delete(config_key)
        except Exception:
            logger.warning(
                "redis_app_config_delete_failed", user_id=str(user_id), client=client
            )

    async def _write_user_app_clients(
        self, user_id: uuid.UUID, active_clients: list[str]
    ) -> None:
        """Update the user-config Redis key's app_clients field WITHOUT clobbering
        other fields. If the key is absent, does nothing (gateway re-derives on miss).

        Redis 만 건드린다 — 호출자가 ``defer_redis_write_until_commit`` 로 커밋 후
        실행을 예약할 수 있게 DB 접근은 호출자가 먼저 끝낸다.
        """
        try:

            # ⚠️ 여기가 가장 아픈 GET-modify-SET 이었다. 이 쓰기는 **새로 추가된
            #    client 를 싣는** 쓰기이므로, 경합에서 지면 단순 staleness 가 아니라
            #    그 앱의 예산 한도가 적용되지 않는 상태가 된다(우회).
            #    키가 없으면 아무것도 하지 않는다 — 총액 필드를 모르는 채로 키를 만들면
            #    게이트웨이가 한도 없는 설정으로 읽는다. 게이트웨이가 DB 에서 재도출한다.
            await refresh_user_app_clients(
                self._cache_mgr._redis, user_id, active_clients, BUDGET_CONFIG_CACHE_TTL
            )
        except Exception:
            logger.warning("redis_refresh_user_app_clients_failed", user_id=str(user_id))

    async def warm_team_budget_cache(self, session: AsyncSession) -> int:
        """startup 시 활성 TEAM 예산 설정을 Redis에 일괄 동기화.

        admin-api 시작 전 init SQL / alembic backfill 로 DB에 삽입된
        TEAM BudgetConfig 행이 Redis에 존재하지 않아 gateway-proxy 가
        team_budget_unset 429 를 반환하는 cold-cache 문제를 봉합한다.

        Returns:
            synced 건수
        """
        repo = BudgetRepository(session)
        configs = await repo.list_configs(scope=BudgetScope.TEAM)
        count = 0
        for cfg in configs:
            if cfg.max_budget_usd is None:
                # T=NULL 'D-only' 행 — limit_usd:null 캐시를 쓰지 않는다.
                # 키 부재 = gateway 가 unset 으로 fail-closed 판정(§3-1).
                continue
            await self._write_team_config_cache(
                scope_id=cfg.scope_id,
                max_budget_usd=cfg.max_budget_usd,
                policy=cfg.policy,
                # migration 0041 이후 DB 가 진실의 원천이다(예전 하드코딩은 운영자
                # 설정을 워밍업이 덮어쓰게 만들었다).
                alert_thresholds=list(cfg.alert_thresholds or []),
                default_cap=cfg.default_user_cap_usd,
            )
            count += 1
        logger.info("team_budget_cache.warmed", count=count)
        return count

    async def detect_orphan_app_budgets(self, session: AsyncSession) -> int:
        """startup 시 '부모 USER 총예산 없는 per-app 예산'(orphan) 행을 탐지·로깅.

        P0-③ review(MF4): 신규 orphan 은 set_user_client_budget 가드가 막지만,
        가드 도입 이전에 생성된 **기존 orphan 행**은 gateway hot path 에서 여전히
        우회된다(app_clients 게이트가 부모 config 를 읽으므로). 자동 마이그레이션은
        위험(임의로 부모 예산을 만들거나 per-app 을 끄는 건 정책 결정)하므로,
        여기서는 **read-only 로 탐지해 WARN 로그**만 남겨 운영자가 수동 조치하게 한다.

        Returns: orphan 건수 (0 이면 clean).
        """
        from sqlalchemy import text as _text

        result = await session.execute(
            _text(
                """
                SELECT c.scope_id, c.client
                FROM budget.budget_configs c
                WHERE c.scope = 'USER' AND c.client IS NOT NULL AND c.is_active = true
                  AND NOT EXISTS (
                    SELECT 1 FROM budget.budget_configs p
                    WHERE p.scope = 'USER' AND p.scope_id = c.scope_id
                      AND p.client IS NULL AND p.is_active = true
                  )
                """
            )
        )
        orphans = result.fetchall()
        if orphans:
            logger.warning(
                "orphan_app_budgets_detected",
                count=len(orphans),
                note="per-app budgets without a parent USER total budget bypass the "
                "gateway hot path; set a parent USER budget or clear these per-app rows",
                samples=[(str(r[0]), r[1]) for r in orphans[:20]],
            )
        else:
            logger.info("orphan_app_budgets_none")
        return len(orphans)

    # ── Auto-Downgrade Config ──

    async def get_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        actor: CurrentUser,
    ) -> AutoDowngradeConfigResponse:
        # BR-BUD-03: 리더는 자기 팀/자기 팀 멤버의 정책만 읽는다 — 형제 읽기 경로
        # (get_budget_config)와 동일 스코핑. 예전엔 actor 검사 자체가 없어
        # TEAM_LEADER 가 임의 팀/유저의 다운그레이드 정책을 열람할 수 있었다(IDOR).
        if actor.role == UserRole.TEAM_LEADER:
            if scope == BudgetScope.TEAM:
                if scope_id != actor.team_id:
                    raise ForbiddenError(
                        "Team leaders can only read budgets for their own team"
                    )
            else:
                target = await UserRepository(session).get_user(scope_id)
                if target is None or actor.team_id is None or target.team_id != actor.team_id:
                    raise ForbiddenError(
                        "Team leaders can only read budgets for their own team members"
                    )

        rule_repo = DowngradePolicyRepository(session)
        # 최신 저장 배치 — 비활성화(끄기)된 규칙도 포함해 화면에서 사라지지 않게 한다.
        rules = await rule_repo.get_current_rules(scope, scope_id)

        return AutoDowngradeConfigResponse(
            scope=scope.value,
            scope_id=str(scope_id),
            enabled=any(r.is_active for r in rules),
            rules=[
                DowngradeRuleResponse(
                    id=str(r.id),
                    from_model_alias=r.from_model_alias,
                    to_model_alias=r.to_model_alias,
                    threshold_pct=r.threshold_pct,
                    is_active=r.is_active,
                    created_at=r.created_at.isoformat(),
                )
                for r in rules
            ],
        )

    async def set_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        data: AutoDowngradeConfigRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AutoDowngradeConfigResponse:
        from app.repositories.model_repository import ModelRepository

        # enabled=False 는 "끄기" 저장이다. 별도의 enabled 플래그 컬럼은 없고 활성
        # 규칙의 존재 자체가 enabled 이므로, 끄기 = 활성 규칙 전부 비활성화다.
        # 규칙·예산 검증을 거치지 않는다 — 예산 미설정이거나 반쯤 편집된 규칙이
        # 있어도 끄기는 항상 성공해야 한다(행은 is_active=False 로 남는다).
        if not data.enabled:
            rule_repo = DowngradePolicyRepository(session)
            removed = await rule_repo.deactivate_rules(scope, scope_id)

            cache_key = f"budget:downgrade:{scope.value.lower()}:{scope_id}"
            await invalidate_after_commit(session, self._cache_mgr, [cache_key])

            await audit_logger.log(
                session,
                actor_user_id=actor.user_id,
                actor_role=actor.role.value,
                action="SET_AUTO_DOWNGRADE",
                resource_type="DowngradePolicy",
                resource_id=str(scope_id),
                changes={"after": {"enabled": False}, "deactivated_rules": removed},
                ip_address=ip_address,
                request_id=request_id,
            )
            return AutoDowngradeConfigResponse(
                scope=scope.value,
                scope_id=str(scope_id),
                enabled=False,
                rules=[],
            )

        # ⚠️ USER scope 규칙 저장은 거부한다 — gateway-proxy 다운그레이드 미들웨어는
        #    team_id 의 TEAM scope 규칙만 평가하므로, USER 규칙은 저장돼도 런타임에
        #    적용되지 않는 죽은 설정이 된다(임계치 기준도 팀 예산 사용률이라 유저별
        #    규칙에는 정의되지 않는다). 끄기/삭제는 과거 행의 정리 경로이므로 허용한다.
        if scope != BudgetScope.TEAM:
            raise ValidationError(
                "Downgrade rules are only supported for TEAM scope "
                "(the gateway evaluates team policies only)"
            )

        if not data.rules:
            raise ValidationError("At least one downgrade rule is required when enabled")

        model_repo = ModelRepository(session)
        all_aliases = {alias for rule in data.rules for alias in (rule.from_model_alias, rule.to_model_alias)}
        # ⚠️ 행을 버리지 말고 들고 있는다 — 아래 provider 대조에 필요하다(존재 확인만 하고
        #    버리면 alias 당 조회를 두 번 하게 된다).
        alias_rows: dict[str, object] = {}
        for alias in all_aliases:
            row = await model_repo.get_by_alias(alias)
            if row is None:
                raise NotFoundError("ModelAlias", alias)
            alias_rows[alias] = row

        for rule in data.rules:
            if rule.from_model_alias == rule.to_model_alias:
                raise ValidationError(f"Source and target model cannot be the same: {rule.from_model_alias}")

            # ⚠️ provider 가 다른 규칙은 **저장 자체를 거부한다.**
            #
            #    강등은 요청 본문의 model 을 그대로 바꿔치기한다. 그런데 각 라우트는 자기
            #    provider 로 필터해서 alias 를 해석한다 — /v1/messages 는
            #    resolve_bedrock_model(provider == BEDROCK)이다. 그래서 BEDROCK alias 를
            #    BEDROCK_MANTLE/RUNTIME_OPENAI alias 로 바꾸는 규칙은 임계값을 넘는 순간
            #    LookupError → **404** 가 되고, 그 스코프의 모든 사용자가 한꺼번에 끊긴다.
            #    비용 절감 설정이 팀을 오프라인으로 만드는 것이고, 404 본문에는 강등 규칙이
            #    원인이라는 단서가 없다.
            #
            #    반대 방향(mantle → runtime plane)은 더 조용하고 더 나쁘다: 두 provider 가
            #    같은 리졸버를 통과하므로 HTTP 200 인 채로 인증 방식(bearer vs SigV4), 단가,
            #    AWS 쪽 invocation 로깅이 함께 바뀐다.
            #
            #    저장 시점이 막을 수 있는 유일한 지점이다 — 요청 시점에는 이미 늦었고
            #    (그 요청은 실패한다) 화면은 200 을 받은 뒤다.
            from_provider = getattr(alias_rows[rule.from_model_alias], "provider", None)
            to_provider = getattr(alias_rows[rule.to_model_alias], "provider", None)
            if from_provider != to_provider:
                raise ValidationError(
                    f"Downgrade target must use the same provider as the source: "
                    f"'{rule.from_model_alias}' is {getattr(from_provider, 'value', from_provider)} "
                    f"but '{rule.to_model_alias}' is {getattr(to_provider, 'value', to_provider)}. "
                    f"A cross-provider rewrite does not resolve on the serving route, so every "
                    f"request in this scope would fail once the threshold is crossed."
                )

        # R4: 강등 대상이 팀의 allowed-models 밖이면, 강등된 요청이 서빙 경로에서
        # 모델 게이트 403 으로 죽는다 — provider 불일치와 같은 부류(저장 시점이
        # 막을 수 있는 유일한 지점). 팀에 허용 목록이 없으면 제한 없음으로 통과.
        from app.repositories.model_repository import TeamAllowedModelRepository

        team_allowed = set(
            await TeamAllowedModelRepository(session).list_by_team(scope_id)
        )
        if team_allowed:
            for rule in data.rules:
                if rule.to_model_alias not in team_allowed:
                    raise ValidationError(
                        f"Downgrade target '{rule.to_model_alias}' is not in this team's "
                        f"allowed models — a downgraded request would be rejected (403) "
                        f"by the model gate once the threshold is crossed."
                    )

        budget_repo = BudgetRepository(session)
        config = await budget_repo.get_first_active_config(scope, scope_id)
        if config is None:
            raise ValidationError("Budget must be configured before setting downgrade rules")
        if config.max_budget_usd <= 0:
            raise ValidationError("Budget max_budget_usd must be greater than 0 for downgrade rules")

        rule_repo = DowngradePolicyRepository(session)
        new_rules = [
            DowngradePolicy(
                scope=scope,
                scope_id=scope_id,
                from_model_alias=r.from_model_alias,
                to_model_alias=r.to_model_alias,
                threshold_pct=r.threshold_pct,
                is_active=True,
                created_by=actor.user_id,
            )
            for r in data.rules
        ]
        await rule_repo.set_rules(scope, scope_id, new_rules)

        cache_key = f"budget:downgrade:{scope.value.lower()}:{scope_id}"
        await invalidate_after_commit(session, self._cache_mgr, [cache_key])

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_AUTO_DOWNGRADE",
            resource_type="DowngradePolicy",
            resource_id=str(scope_id),
            changes={"after": {
                "enabled": data.enabled,
                "rules": [r.model_dump() for r in data.rules],
            }},
            ip_address=ip_address,
            request_id=request_id,
        )

        return await self.get_downgrade_config(
            session, scope=scope, scope_id=scope_id, actor=actor
        )

    async def delete_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        rule_repo = DowngradePolicyRepository(session)
        # Clear 는 삭제 — soft-delete 로 두면 최신 배치가 비활성 규칙으로 다시 보인다.
        await rule_repo.clear_rules(scope, scope_id)

        cache_key = f"budget:downgrade:{scope.value.lower()}:{scope_id}"
        await invalidate_after_commit(session, self._cache_mgr, [cache_key])

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_AUTO_DOWNGRADE",
            resource_type="DowngradePolicy",
            resource_id=str(scope_id),
            changes={"after": {"disabled": True}},
            ip_address=ip_address,
            request_id=request_id,
        )
