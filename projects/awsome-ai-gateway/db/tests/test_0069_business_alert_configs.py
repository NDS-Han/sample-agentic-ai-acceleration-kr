# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0069 — business Slack alert notification configs + dead-letter created_at index."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_MIG_PATH = _DB / "versions" / "0069_business_alert_configs.py"
_MIG = _MIG_PATH.read_text()
_SEED = (_DB / "init" / "06_seed_notification_configs.sql").read_text()
_INIT_TABLES = (_DB / "init" / "02_create_tables.sql").read_text()
_NOTIF_SCHEMA = (_DB / "init" / "05_create_notification_schema.sql").read_text()
_GRANTS = (_DB / "init" / "07_grant_notification_privileges.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def test_revision_chain():
    assert re.search(r'^revision = "0069"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0065"$', _MIG, re.M)


def test_business_alert_rows_are_seeded_disabled_in_migration_and_init():
    for event_type in ("reconciliation_mismatch", "dead_letter_growth", "pricing_missing"):
        assert event_type in _MIG
        assert event_type in _SEED
    for text in (_MIG, _SEED):
        for event_type in ("reconciliation_mismatch", "dead_letter_growth", "pricing_missing"):
            assert re.search(rf"\('{event_type}'[^\n]*false", text, re.I)


def test_notification_config_json_column_is_in_migration_and_init():
    init_schema = (_DB / "init" / "05_create_notification_schema.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS config JSONB" in _MIG
    assert "DROP COLUMN IF EXISTS config" in _MIG
    assert re.search(r"^\s+config\s+JSONB\s+NOT NULL DEFAULT '\{\}'", init_schema, re.M)
    # Seed rows may omit config: the migration/init column default of '{}' applies.


def test_lock_safe_and_concurrent_index():
    assert "LOCK_TIMEOUT" in _MIG and "'5s'" in _MIG
    assert "autocommit_block" in _MIG
    assert "CREATE INDEX CONCURRENTLY" in _MIG
    assert "idx_cost_dead_letters_created" in _MIG
    assert "idx_cost_dead_letters_created" in _INIT_TABLES
    # 최근 발송행 조회용 부분 인덱스 — 마이그레이션과 init 스키마 양쪽에 있어야 한다.
    assert "idx_notification_logs_biz_alert_latest" in _MIG
    assert "idx_notification_logs_biz_alert_latest" in _NOTIF_SCHEMA


def test_notification_worker_usage_read_grants_are_in_migration_and_init():
    for text in (_MIG, _GRANTS):
        assert "GRANT USAGE ON SCHEMA usage TO notification_worker_user" in text
        assert "usage.reconciliation_runs" in text
        assert "usage.cost_dead_letters" in text
        assert "usage.usage_logs" in text


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=check,
        capture_output=True,
        text=True,
    )


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_DB,
        check=check,
        env={**os.environ, "DB_MASTER_URL": PG_URL},
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip_real_pg():
    _alembic("upgrade", "head")
    assert _psql(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='notification' AND table_name='notification_configs' "
        "AND column_name='config')"
    ).stdout.strip() == "t"
    rows = _psql(
        "SELECT event_type || ':' || enabled || ':' || COALESCE(config::text, 'null') "
        "FROM notification.notification_configs WHERE event_type IN "
        "('reconciliation_mismatch', 'dead_letter_growth', 'pricing_missing') ORDER BY event_type"
    ).stdout.strip().splitlines()
    assert rows == [
        "dead_letter_growth:false:{}",
        "pricing_missing:false:{}",
        "reconciliation_mismatch:false:{}",
    ]
    assert _psql(
        "SELECT i.indisvalid AND i.indisready FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='usage' "
        "AND c.relname='idx_cost_dead_letters_created'"
    ).stdout.strip() == "t"

    _alembic("downgrade", "0065")
    assert _psql(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='notification' AND table_name='notification_configs' "
        "AND column_name='config')"
    ).stdout.strip() == "f"
    assert _psql(
        "SELECT count(*) FROM notification.notification_configs WHERE event_type IN "
        "('reconciliation_mismatch', 'dead_letter_growth', 'pricing_missing')"
    ).stdout.strip() == "0"
    assert _psql("SELECT to_regclass('usage.idx_cost_dead_letters_created') IS NULL").stdout.strip() == "t"

    _alembic("upgrade", "head")
    _alembic("upgrade", "head")  # 멱등
    assert _psql(
        "SELECT count(*) FROM notification.notification_configs WHERE event_type IN "
        "('reconciliation_mismatch', 'dead_letter_growth', 'pricing_missing')"
    ).stdout.strip() == "3"
