# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0064 — business Slack alert notification configs + dead-letter created_at index."""
from __future__ import annotations

import re
from pathlib import Path

_DB = Path(__file__).resolve().parents[1]
_MIG_PATH = _DB / "versions" / "0064_business_alert_configs.py"
_MIG = _MIG_PATH.read_text()
_SEED = (_DB / "init" / "06_seed_notification_configs.sql").read_text()
_INIT_TABLES = (_DB / "init" / "02_create_tables.sql").read_text()
_GRANTS = (_DB / "init" / "07_grant_notification_privileges.sql").read_text()


def test_revision_chain():
    assert re.search(r'^revision = "0064"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0063"$', _MIG, re.M)


def test_business_alert_rows_are_seeded_disabled_in_migration_and_init():
    for event_type in ("reconciliation_mismatch", "dead_letter_growth", "pricing_missing"):
        assert event_type in _MIG
        assert event_type in _SEED
    for text in (_MIG, _SEED):
        for event_type in ("reconciliation_mismatch", "dead_letter_growth", "pricing_missing"):
            assert re.search(rf"\('{event_type}'[^\n]*false", text, re.I)


def test_lock_safe_and_concurrent_index():
    assert "LOCK_TIMEOUT" in _MIG and "'5s'" in _MIG
    assert "autocommit_block" in _MIG
    assert "CREATE INDEX CONCURRENTLY" in _MIG
    assert "idx_cost_dead_letters_created" in _MIG
    assert "idx_cost_dead_letters_created" in _INIT_TABLES


def test_notification_worker_usage_read_grants_are_in_migration_and_init():
    for text in (_MIG, _GRANTS):
        assert "GRANT USAGE ON SCHEMA usage TO notification_worker_user" in text
        assert "usage.reconciliation_runs" in text
        assert "usage.cost_dead_letters" in text
        assert "usage.usage_logs" in text
