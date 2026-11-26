# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0070 — budget.user_app_group_budgets static/lock-safety checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_DB = Path(__file__).resolve().parents[1]
_MIG = _DB / "versions" / "0070_user_app_group_budgets.py"
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("m0070", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain_and_lock_budget():
    m = _load()
    assert m.revision == "0070" and m.down_revision == "0069"
    assert m.LOCK_TIMEOUT == "5s" and m.MAX_ATTEMPTS >= 3
    assert "CREATE TABLE IF NOT EXISTS budget.user_app_group_budgets" in m._CREATE
    assert "ALTER TABLE" not in m._CREATE.upper()
    assert "usage.usage_logs" not in m._CREATE


def test_init_sql_mirrors_migration():
    assert "CREATE TABLE IF NOT EXISTS budget.user_app_group_budgets" in _INIT
    for col in ("user_id", "app_group", "max_budget_usd", "policy", "alert_thresholds", "allocated_by"):
        assert col in _INIT
    assert "ck_user_app_group_budget_name" in _INIT
