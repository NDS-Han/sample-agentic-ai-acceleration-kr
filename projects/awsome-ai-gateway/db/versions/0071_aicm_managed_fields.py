# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AICM managed-field ownership records — auth.external_management

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-26

새 제어plane 테이블만 만든다. gateway-proxy hot path 는 이 테이블을 읽지 않는다.
기존 테이블 ALTER/rewrite 없음, ``usage.usage_logs`` 무관. lock_timeout 5s + 재시도
(AGENTS.md 규칙 6). PK `(resource_type, resource_id, facet)` 가 사람 관리자 쓰기
잠금 검사의 단일 인덱스 조회를 제공한다.
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS auth.external_management (
        resource_type     VARCHAR(16)   NOT NULL,
        resource_id       UUID          NOT NULL,
        facet             VARCHAR(96)   NOT NULL,
        managed_by        VARCHAR(32)   NOT NULL,
        state             VARCHAR(16)   NOT NULL,
        value_state       VARCHAR(16)   NOT NULL,
        last_written_at   TIMESTAMPTZ   NOT NULL,
        token_id          UUID          REFERENCES auth.service_tokens(id) ON DELETE SET NULL,
        lineage_id        UUID,
        request_id        VARCHAR(128)  NOT NULL DEFAULT '',
        released_at       TIMESTAMPTZ,
        released_by       UUID          REFERENCES auth.users(id) ON DELETE SET NULL,
        release_reason    TEXT,
        created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
        PRIMARY KEY (resource_type, resource_id, facet),
        CONSTRAINT ck_external_management_resource_type
            CHECK (resource_type IN ('user', 'team')),
        CONSTRAINT ck_external_management_state
            CHECK (state IN ('managed', 'released')),
        CONSTRAINT ck_external_management_value_state
            CHECK (value_state IN ('set', 'cleared'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_external_management_managed_by_state "
    "ON auth.external_management (managed_by, state)",
]

_DOWNGRADE: list[str] = [
    "DROP TABLE IF EXISTS auth.external_management",
]


def _is_lock_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code is None and orig is not None:
        code = getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return code == "55P03" or "lock timeout" in str(exc).lower()


def _exec_retry(bind, stmt: str) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            bind.execute(text(stmt))
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == MAX_ATTEMPTS:
                raise
            print(f"[0071] lock timeout (attempt {attempt}) — retrying: "
                  f"{' '.join(stmt.split())[:80]}", flush=True)
            time.sleep(min(2 ** attempt, 20))


def _apply(stmts: list[str]) -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in stmts:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(fn) -> None:
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        fn()
        return
    with op.get_context().autocommit_block():
        fn()


def upgrade() -> None:
    _run(lambda: _apply(_TABLES))


def downgrade() -> None:
    # ⚠️ 잠금 소유 기록이 사라진다 — 되돌리기 전 사람에게 ownership 이 넘어간다.
    _run(lambda: _apply(_DOWNGRADE))
