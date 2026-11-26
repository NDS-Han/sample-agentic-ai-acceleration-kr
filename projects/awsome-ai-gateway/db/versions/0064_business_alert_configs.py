# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""business Slack alerts — notification config seed + read grants + dead-letter growth index

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-25

## 무엇
1. business alert 3종을 ``notification.notification_configs`` 에 **enabled=false** 로 시드한다.
   운영자가 켜기 전까지 notification-worker 가 아무 Slack 알림도 보내지 않는다. recipient_roles 는
   빈 배열이다 — business alert 는 사용자 이메일이 아니라 운영 Slack webhook 으로 간다.
2. notification-worker 가 읽는 usage 테이블 3개에 최소 SELECT 권한을 준다.
3. ``usage.cost_dead_letters(created_at)`` 인덱스를 추가한다. dead-letter "growth" 는 replayed 여부와
   무관하게 최근 insert 를 본다. 테이블은 작게 유지되는 것이 목표지만 장애 시 커질 수 있으므로
   CONCURRENTLY 로 만든다.

## 락 (lock-safe, AGENTS.md 규칙 6)
- 기존 테이블 rewrite 없음. 인덱스는 ``CREATE INDEX CONCURRENTLY``.
- 문장 단위 autocommit + ``SET lock_timeout = '5s'`` + 재시도(0062/0063 과 같은 방식).
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_CONFIG_INSERT = """
INSERT INTO notification.notification_configs (event_type, recipient_roles, enabled)
VALUES
    ('reconciliation_mismatch', '[]', false),
    ('dead_letter_growth',      '[]', false),
    ('pricing_missing',         '[]', false)
ON CONFLICT (event_type) DO NOTHING
"""

_GRANTS: list[str] = [
    "GRANT USAGE ON SCHEMA usage TO notification_worker_user",
    "GRANT SELECT (status, mismatch_count, report_date, period, started_at, finished_at, checks) "
    "ON usage.reconciliation_runs TO notification_worker_user",
    "GRANT SELECT (reason, created_at) ON usage.cost_dead_letters TO notification_worker_user",
    "GRANT SELECT (model_alias, input_tokens, output_tokens, requested_at, pricing_missing) "
    "ON usage.usage_logs TO notification_worker_user",
]

_REVOKE: list[str] = [
    "REVOKE SELECT (model_alias, input_tokens, output_tokens, requested_at, pricing_missing) "
    "ON usage.usage_logs FROM notification_worker_user",
    "REVOKE SELECT (reason, created_at) ON usage.cost_dead_letters FROM notification_worker_user",
    "REVOKE SELECT (status, mismatch_count, report_date, period, started_at, finished_at, checks) "
    "ON usage.reconciliation_runs FROM notification_worker_user",
    "REVOKE USAGE ON SCHEMA usage FROM notification_worker_user",
]

_DELETE_CONFIGS = """
DELETE FROM notification.notification_configs
WHERE event_type IN ('reconciliation_mismatch', 'dead_letter_growth', 'pricing_missing')
"""


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
            print(f"[0064] lock timeout (attempt {attempt}) — retrying: "
                  f"{' '.join(stmt.split())[:80]}", flush=True)
            time.sleep(min(2 ** attempt, 20))


def _drop_invalid_index_if_needed(bind) -> None:
    """실패한 CONCURRENTLY 시도가 남긴 invalid index 는 IF NOT EXISTS 를 무의미하게 만든다."""
    row = bind.execute(text("""
        SELECT i.indisvalid
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'usage' AND c.relname = 'idx_cost_dead_letters_created'
    """)).scalar()
    if row is False:
        _exec_retry(bind, "DROP INDEX CONCURRENTLY IF EXISTS usage.idx_cost_dead_letters_created")


def _upgrade_all() -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        _exec_retry(bind, _CONFIG_INSERT)
        for stmt in _GRANTS:
            _exec_retry(bind, stmt)
        _drop_invalid_index_if_needed(bind)
        _exec_retry(
            bind,
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cost_dead_letters_created "
            "ON usage.cost_dead_letters (created_at DESC)",
        )
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _downgrade_all() -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        _exec_retry(bind, "DROP INDEX CONCURRENTLY IF EXISTS usage.idx_cost_dead_letters_created")
        for stmt in _REVOKE:
            _exec_retry(bind, stmt)
        _exec_retry(bind, _DELETE_CONFIGS)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(fn) -> None:
    """autocommit 실행 — CREATE/DROP INDEX CONCURRENTLY 가 트랜잭션 안에서 실패하지 않게 한다."""
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        fn()
        return
    with op.get_context().autocommit_block():
        fn()


def upgrade() -> None:
    _run(_upgrade_all)


def downgrade() -> None:
    _run(_downgrade_all)
