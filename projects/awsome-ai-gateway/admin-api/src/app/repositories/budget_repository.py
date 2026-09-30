# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage, DowngradePolicy
from app.repositories._locks import advisory_xact_lock


class BudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_config(self, config: BudgetConfig) -> BudgetConfig:
        # Serialize concurrent upserts of the same logical key. Without this, two overlapping
        # writes each deactivate-then-insert and leave two is_active=true rows behind, after
        # which get_active_config() raises MultipleResultsFound forever (regression #52).
        # The lock key must match the predicate below exactly, client included.
        await advisory_xact_lock(
            self._session, "budget_configs", config.scope.value, config.scope_id, config.client
        )
        # Deactivate existing for same scope+scope_id+(client if given)
        stmt = update(BudgetConfig).where(
            BudgetConfig.scope == config.scope,
            BudgetConfig.scope_id == config.scope_id,
            BudgetConfig.is_active.is_(True),
        )
        if config.client is not None:
            stmt = stmt.where(BudgetConfig.client == config.client)
        else:
            stmt = stmt.where(BudgetConfig.client.is_(None))
        stmt = stmt.values(is_active=False)
        await self._session.execute(stmt)
        self._session.add(config)
        await self._session.flush()
        return config

    async def get_active_config(self, scope: BudgetScope, scope_id: uuid.UUID) -> BudgetConfig | None:
        stmt = select(BudgetConfig).where(
            BudgetConfig.scope == scope,
            BudgetConfig.scope_id == scope_id,
            BudgetConfig.client.is_(None),
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> BudgetConfig | None:
        """Return the active per-app BudgetConfig for (scope, scope_id, client)."""
        stmt = select(BudgetConfig).where(
            BudgetConfig.scope == scope,
            BudgetConfig.scope_id == scope_id,
            BudgetConfig.client == client,
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_first_active_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> BudgetConfig | None:
        """Like get_active_app_config but tolerates duplicate active rows (read path)."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client == client,
                BudgetConfig.is_active.is_(True),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def deactivate_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> int:
        """Deactivate the active per-app BudgetConfig for (scope, scope_id, client)."""
        stmt = (
            update(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client == client,
                BudgetConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def list_active_app_clients(self, user_id: uuid.UUID) -> list[str]:
        """Return client values of active per-app BudgetConfig rows for user_id."""
        from sqlalchemy import distinct

        stmt = select(distinct(BudgetConfig.client)).where(
            BudgetConfig.scope == BudgetScope.USER,
            BudgetConfig.scope_id == user_id,
            BudgetConfig.client.is_not(None),
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def get_latest_config(self, scope: BudgetScope, scope_id: uuid.UUID) -> BudgetConfig | None:
        """최신 총액 config 행 — is_active 무관. T 재설정 시 이전 행의
        default_user_cap_usd 를 이어 받기 위한 용도(§3-1: A_u·app_c·D 보존)."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client.is_(None),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active_app_configs(self, user_id: uuid.UUID) -> list[BudgetConfig]:
        """유저의 활성 per-app config 행 전부 — cascade 삭제·확인 다이얼로그용."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == BudgetScope.USER,
                BudgetConfig.scope_id == user_id,
                BudgetConfig.client.is_not(None),
                BudgetConfig.is_active.is_(True),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def deactivate_app_configs_for_user(self, user_id: uuid.UUID) -> list[str]:
        """유저의 활성 per-app config 전부 비활성화 — 비활성화된 client 목록 반환.

        I-3 cascade: 부모 USER 총액 예산이 사라지는 경로(delete_user_budget,
        transfer_user, 균등분배 초기화)는 하위 app 예산을 함께 끈다.
        """
        rows = await self.list_active_app_configs(user_id)
        clients = [r.client for r in rows if r.client is not None]
        for r in rows:
            r.is_active = False
        if rows:
            await self._session.flush()
        return clients

    async def max_app_budget(self, user_id: uuid.UUID) -> Decimal | None:
        """유저의 활성 per-app 예산 중 최대값 — I-2 검증(app_c ≤ A_u)용.

        '최소 하나라도 app_c > A_u_new 면 거부' 이므로 MAX 하나면 충분하다."""
        stmt = select(func.max(BudgetConfig.max_budget_usd)).where(
            BudgetConfig.scope == BudgetScope.USER,
            BudgetConfig.scope_id == user_id,
            BudgetConfig.client.is_not(None),
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_configured_member_ids(self, team_id: uuid.UUID) -> set[uuid.UUID]:
        """팀 멤버 중 활성 USER 총액 예산(A_u)이 있는 user_id 집합 — '미설정 멤버' 판별용."""
        from app.models.auth import User

        stmt = (
            select(BudgetConfig.scope_id)
            .join(User, BudgetConfig.scope_id == User.id)
            .where(
                BudgetConfig.scope == BudgetScope.USER,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
                User.team_id == team_id,
            )
        )
        result = await self._session.execute(stmt)
        return {row[0] for row in result.fetchall()}

    async def get_app_usage(
        self, scope: BudgetScope, scope_id: uuid.UUID, period: str, client: str
    ) -> Decimal:
        """per-app(client) 월 사용량 — 없으면 0. get_usage 는 client IS NULL 전용."""
        stmt = select(BudgetUsage).where(
            BudgetUsage.scope == scope,
            BudgetUsage.scope_id == scope_id,
            BudgetUsage.period == period,
            BudgetUsage.client == client,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return row.used_usd if row else Decimal("0")

    async def get_usages_for_scope_ids(
        self, scope: BudgetScope, scope_ids: list[uuid.UUID], period: str
    ) -> dict[uuid.UUID, Decimal]:
        """여러 scope_id 의 월 사용량을 한 번에 — 확인 다이얼로그의 영향 멤버 목록용."""
        if not scope_ids:
            return {}
        stmt = select(BudgetUsage).where(
            BudgetUsage.scope == scope,
            BudgetUsage.scope_id.in_(scope_ids),
            BudgetUsage.period == period,
            BudgetUsage.client.is_(None),
        )
        result = await self._session.execute(stmt)
        return {row.scope_id: row.used_usd for row in result.scalars().all()}

    async def get_first_active_config(self, scope: BudgetScope, scope_id: uuid.UUID) -> BudgetConfig | None:
        """Like get_active_config but tolerates duplicate rows. Excludes per-app rows."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def deactivate_configs(self, scope: BudgetScope, scope_id: uuid.UUID) -> int:
        """Deactivate total-budget configs (client IS NULL) for scope+scope_id."""
        stmt = (
            update(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def sum_member_budgets(self, team_id: uuid.UUID) -> Decimal:
        """Sum of active USER total budgets (client IS NULL) for members of a team.

        Per-app rows (client IS NOT NULL) are intentionally excluded — the team
        cap check (BR-BUD-01) compares the per-user *total* budget, not per-app
        sub-limits, so including them would double-count and cause false rejections.
        """
        from app.models.auth import User

        stmt = (
            select(BudgetConfig)
            .join(User, BudgetConfig.scope_id == User.id)
            .where(
                BudgetConfig.scope == BudgetScope.USER,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
                User.team_id == team_id,
            )
        )
        result = await self._session.execute(stmt)
        configs = result.scalars().all()
        return sum((c.max_budget_usd for c in configs), Decimal("0"))

    async def get_usage(self, scope: BudgetScope, scope_id: uuid.UUID, period: str) -> BudgetUsage | None:
        # client IS NULL 행만 — 총합 usage. per-app 행까지 매칭되면 같은
        # (scope, scope_id, period) 에 복수 행이 걸려 scalar_one_or_none 이
        # MultipleResultsFound 를 던진다(바로 아래 list_configs 주석과 같은 함정).
        stmt = select(BudgetUsage).where(
            BudgetUsage.scope == scope,
            BudgetUsage.scope_id == scope_id,
            BudgetUsage.period == period,
            BudgetUsage.client.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_configs(
        self,
        scope: BudgetScope | None = None,
        scope_id: uuid.UUID | None = None,
    ) -> list[BudgetConfig]:
        """Return active total-budget configs (client IS NULL only).

        Per-app rows are excluded here because all callers (get_budget_summary,
        warm_team_budget_cache) key results on (scope, scope_id) — a per-app row
        would silently collide with the total row in that dict and corrupt the
        budget summary dashboard.
        """
        stmt = select(BudgetConfig).where(
            BudgetConfig.is_active.is_(True),
            BudgetConfig.client.is_(None),
        )
        if scope:
            stmt = stmt.where(BudgetConfig.scope == scope)
        if scope_id:
            stmt = stmt.where(BudgetConfig.scope_id == scope_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_configs_with_usage(
        self,
        scope: BudgetScope | None = None,
        scope_id: uuid.UUID | None = None,
        period: str | None = None,
    ) -> list[tuple[BudgetConfig, BudgetUsage | None]]:
        stmt = select(BudgetConfig).where(BudgetConfig.is_active.is_(True))
        if scope:
            stmt = stmt.where(BudgetConfig.scope == scope)
        if scope_id:
            stmt = stmt.where(BudgetConfig.scope_id == scope_id)
        result = await self._session.execute(stmt)
        configs = result.scalars().all()

        pairs: list[tuple[BudgetConfig, BudgetUsage | None]] = []
        for cfg in configs:
            usage = None
            if period:
                usage = await self.get_usage(cfg.scope, cfg.scope_id, period)
            pairs.append((cfg, usage))
        return pairs


class DowngradePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_rules(self, scope: BudgetScope, scope_id: uuid.UUID) -> list[DowngradePolicy]:
        stmt = (
            select(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .order_by(DowngradePolicy.threshold_pct)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_current_rules(
        self, scope: BudgetScope, scope_id: uuid.UUID
    ) -> list[DowngradePolicy]:
        """가장 최근 저장 배치 — is_active 무관하게 돌려준다.

        저장할 때마다 이전 배치는 비활성화되고 새 배치가 INSERT 된다. 한 배치의
        행들은 같은 트랜잭션에서 INSERT 되므로 created_at(server_default=func.now(),
        트랜잭션 시각)이 모두 같다 — max(created_at) 이 곧 최신 배치다.
        비활성화된 배치도 반환하므로 '끄기' 상태의 규칙이 화면에서 사라지지 않는다.
        """
        latest = (
            select(func.max(DowngradePolicy.created_at))
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
            )
            .scalar_subquery()
        )
        stmt = (
            select(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.created_at == latest,
            )
            .order_by(DowngradePolicy.threshold_pct)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def set_rules(
        self, scope: BudgetScope, scope_id: uuid.UUID, rules: list[DowngradePolicy]
    ) -> list[DowngradePolicy]:
        await self._session.execute(
            update(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self._session.flush()
        for rule in rules:
            self._session.add(rule)
        await self._session.flush()
        return rules

    async def deactivate_rules(self, scope: BudgetScope, scope_id: uuid.UUID) -> int:
        """활성 규칙을 비활성화만 한다(끄기) — 행은 남아 GET 이 최신 배치로 돌려준다."""
        stmt = (
            update(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def clear_rules(self, scope: BudgetScope, scope_id: uuid.UUID) -> int:
        """규칙 행 자체를 삭제한다(Clear). soft-delete 로 두면 비활성 배치가
        get_current_rules 의 최신 배치로 다시 노출되므로, 삭제 의미를 지키기
        위해 hard delete 한다. 이력은 audit_log 가 보존한다."""
        stmt = delete(DowngradePolicy).where(
            DowngradePolicy.scope == scope,
            DowngradePolicy.scope_id == scope_id,
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]
