# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""user app-group advisory budgets (AICM admin mirror)

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-26

새 테이블만 만든다 — 기존 테이블 rewrite 없음, hot path 에서 읽지 않는 advisory 제어plane
테이블이다. lock_timeout 은 5s, lock timeout 은 지수 backoff 로 재시도한다(AGENTS.md 규칙 6).
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_CREATE = """
CREATE TABLE IF NOT EXISTS budget.user_app_group_budgets (
    user_id           UUID                  NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    app_group         VARCHAR(64)           NOT NULL,
    max_budget_usd    NUMERIC(12,4)         NOT NULL,
    policy            budget.budget_policy  NOT NULL DEFAULT 'HARD_BLOCK',
    alert_thresholds  INTEGER[]             NOT NULL DEFAULT '{80,90,100}',
    allocated_by      UUID                  NOT NULL REFERENCES auth.users(id),
    created_at        TIMESTAMPTZ           NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ           NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, app_group),
    CONSTRAINT ck_user_app_group_budget_name
        CHECK (app_group ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$')
)
"""

_DROP = "DROP TABLE IF EXISTS budget.user_app_group_budgets"


def _is_lock_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "55P03" or "lock timeout" in str(exc).lower()


def _exec_retry(bind, stmt: str) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            bind.execute(text(stmt))
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(min(2 ** attempt, 20))


def _run(stmt: str) -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def upgrade() -> None:
    _run(_CREATE)


def downgrade() -> None:
    _run(_DROP)
