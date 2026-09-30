#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""배포 전 예산 데이터 점검 (budget-rules.md D-19).

보고 항목:
  I-2  app_c > A_u — 활성 앱별 cap 이 부모 유저 cap 을 초과
  I-3  부모 없는 앱 예산 — 활성 앱별 config 인데 부모 유저 config 가 비활성/없음
  I-4  비멤버 예산 — team_id IS NULL 유저에게 걸린 예산 config (§4-6: 정상
       운영에서는 Cognito sync 가 항상 팀을 배정하므로 0 건이어야 함)
  T-0  team_id IS NULL 유저 수 — no_team_assigned 로 차단될 유저 목록
  D-ONLY T=NULL 팀 config 행 — D 만 설정된 행(정상, 정보용)

실행 (admin-api venv, DATABASE_URL 은 앱 settings/환경변수와 동일):
    .venv/bin/python scripts/budget_data_audit.py
    DATABASE_URL=postgresql+asyncpg://... .venv/bin/python scripts/budget_data_audit.py

읽기 전용. 위반이 하나라도 있으면 exit 1 — 배포 전 gate 로 쓸 수 있다.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import and_, func, select

sys.path.insert(0, "src")  # 스크립트를 repo 의 admin-api/ 에서 실행한다고 가정

from app.core.db import AsyncSessionLocal, engine  # noqa: E402
from app.models.auth import User  # noqa: E402
from app.models.budget import BudgetConfig, BudgetScope  # noqa: E402


async def main() -> int:
    violations = 0

    async with AsyncSessionLocal() as s:
        # ── I-2: app_c > A_u ─────────────────────────────────────────────
        app_cfg = BudgetConfig.__table__.alias("app_cfg")
        parent_cfg = BudgetConfig.__table__.alias("parent_cfg")
        stmt = (
            select(
                app_cfg.c.scope_id,
                app_cfg.c.client,
                app_cfg.c.max_budget_usd,
                parent_cfg.c.max_budget_usd,
            )
            .select_from(
                app_cfg.join(
                    parent_cfg,
                    and_(
                        parent_cfg.c.scope == BudgetScope.USER,
                        parent_cfg.c.scope_id == app_cfg.c.scope_id,
                        parent_cfg.c.client.is_(None),
                        parent_cfg.c.is_active.is_(True),
                    ),
                )
            )
            .where(app_cfg.c.scope == BudgetScope.USER)
            .where(app_cfg.c.client.is_not(None))
            .where(app_cfg.c.is_active.is_(True))
            .where(app_cfg.c.max_budget_usd > parent_cfg.c.max_budget_usd)
        )
        rows = (await s.execute(stmt)).all()
        print(f"[I-2] app_c > A_u 위반: {len(rows)}건")
        for scope_id, client, app_c, a_u in rows:
            print(f"  user={scope_id} client={client} app_c={app_c} > A_u={a_u}")
        violations += len(rows)

        # ── I-3: 부모 없는 앱 예산 ───────────────────────────────────────
        orphan_parent = parent_cfg
        stmt = (
            select(app_cfg.c.scope_id, app_cfg.c.client, app_cfg.c.max_budget_usd)
            .select_from(
                app_cfg.outerjoin(
                    orphan_parent,
                    and_(
                        orphan_parent.c.scope == BudgetScope.USER,
                        orphan_parent.c.scope_id == app_cfg.c.scope_id,
                        orphan_parent.c.client.is_(None),
                        orphan_parent.c.is_active.is_(True),
                    ),
                )
            )
            .where(app_cfg.c.scope == BudgetScope.USER)
            .where(app_cfg.c.client.is_not(None))
            .where(app_cfg.c.is_active.is_(True))
            .where(orphan_parent.c.id.is_(None))
        )
        rows = (await s.execute(stmt)).all()
        print(f"[I-3] 부모 없는 앱 예산: {len(rows)}건")
        for scope_id, client, app_c in rows:
            print(f"  user={scope_id} client={client} app_c={app_c}")
        violations += len(rows)

        # ── I-4: 비멤버 예산 (team_id NULL 인 유저의 예산 config) ─────────
        stmt = (
            select(BudgetConfig.scope_id, BudgetConfig.client, BudgetConfig.max_budget_usd)
            .join(User, User.id == BudgetConfig.scope_id)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.is_active.is_(True))
            .where(User.team_id.is_(None))
        )
        rows = (await s.execute(stmt)).all()
        print(f"[I-4] 비멤버 예산(team_id NULL 유저의 config): {len(rows)}건")
        for scope_id, client, cap in rows:
            print(f"  user={scope_id} client={client} cap={cap}")
        violations += len(rows)

        # ── T-0: team_id IS NULL 유저 (no_team_assigned 차단 대상) ───────
        stmt = select(User.id, User.display_name, User.email).where(User.team_id.is_(None))
        rows = (await s.execute(stmt)).all()
        print(f"[T-0] team_id IS NULL 유저: {len(rows)}건 (§4-6: 정상이면 0)")
        for uid, name, email in rows:
            print(f"  user={uid} name={name} email={email}")
        violations += len(rows)

        # ── D-only: T=NULL 팀 config 행 (정상 — 정보용) ──────────────────
        # 0039 마이그레이션 전 배포본에는 컬럼이 없다 — 그 환경에서는 건너뛴다.
        if hasattr(BudgetConfig, "default_user_cap_usd"):
            stmt = (
                select(BudgetConfig.scope_id, BudgetConfig.default_user_cap_usd)
                .where(BudgetConfig.scope == BudgetScope.TEAM)
                .where(BudgetConfig.is_active.is_(True))
                .where(BudgetConfig.max_budget_usd.is_(None))
            )
            rows = (await s.execute(stmt)).all()
            print(f"[INFO] T=NULL(D-only) 팀 config: {len(rows)}건 — T 미설정으로 enforcement 는 unset 판정")
            for scope_id, cap_d in rows:
                print(f"  team={scope_id} D={cap_d}")
        else:
            print("[INFO] T=NULL(D-only) 검사 건너뜀 — 배포본이 0039 마이그레이션 이전")
            stmt = (
                select(func.count())
                .select_from(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.TEAM)
                .where(BudgetConfig.is_active.is_(True))
                .where(BudgetConfig.max_budget_usd.is_(None))
            )
            n = (await s.execute(stmt)).scalar_one()
            if n:
                print(f"  주의: max_budget_usd IS NULL 인 활성 TEAM 행 {n}건 존재")

        # ── 요약 카운트 ──────────────────────────────────────────────────
        counts = await s.execute(
            select(BudgetConfig.scope, func.count())
            .where(BudgetConfig.is_active.is_(True))
            .group_by(BudgetConfig.scope)
        )
        print("[INFO] 활성 config 수:", {r[0].value: r[1] for r in counts})

    await engine.dispose()

    if violations:
        print(f"\nFAIL: 위반 {violations}건 — 정리 후 배포할 것")
        return 1
    print("\nOK: 위반 없음")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
