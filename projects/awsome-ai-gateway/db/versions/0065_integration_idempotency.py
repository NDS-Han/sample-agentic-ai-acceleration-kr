# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""integration idempotency keys (AICM /aicm/v1) — auth.integration_idempotency_keys

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-25

## 무엇
``/aicm/v1`` mutation 의 ``Idempotency-Key`` 저장소. 키 범위는 ``(integration, idem_key)``.
요청 트랜잭션 안에서 ``INSERT … ON CONFLICT DO NOTHING`` 으로 선점하고(진행 중 = status_code
NULL), 성공 응답을 같은 행에 적는다. 같은 키 + 같은 요청 해시 재전송 → 저장된 응답 재생,
다른 해시 → 422. 비밀값(rotate 의 새 토큰 원문)은 저장하지 않는다.

## 락 (lock-safe, AGENTS.md 규칙 6)
새 빈 테이블과 인덱스만. ``SET lock_timeout = '5s'`` + 재시도.
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS auth.integration_idempotency_keys (
        integration     VARCHAR(32)   NOT NULL,
        idem_key        VARCHAR(128)  NOT NULL,
        method          VARCHAR(8)    NOT NULL,
        path            VARCHAR(512)  NOT NULL,
        request_sha256  VARCHAR(64)   NOT NULL,
        status_code     INTEGER,
        response        JSONB,
        created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
        completed_at    TIMESTAMPTZ,
        PRIMARY KEY (integration, idem_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_integration_idempotency_created "
    "ON auth.integration_idempotency_keys (created_at)",
]

_DOWNGRADE: list[str] = [
    "DROP TABLE IF EXISTS auth.integration_idempotency_keys",
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
            print(f"[0065] lock timeout (attempt {attempt}) — retrying: "
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
    _run(lambda: _apply(_DOWNGRADE))
