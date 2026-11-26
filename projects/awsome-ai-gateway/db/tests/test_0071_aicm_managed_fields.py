# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0071 — auth.external_management static/lock-safety checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_DB = Path(__file__).resolve().parents[1]
_MIG = _DB / "versions" / "0071_aicm_managed_fields.py"
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("m0071", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain_and_lock_budget():
    m = _load()
    assert m.revision == "0071" and m.down_revision == "0070"
    assert m.LOCK_TIMEOUT == "5s" and m.MAX_ATTEMPTS >= 3
    sql = "\n".join(m._TABLES)
    assert "CREATE TABLE IF NOT EXISTS auth.external_management" in sql
    assert "ALTER TABLE" not in sql.upper()
    assert "usage.usage_logs" not in sql


def test_init_sql_mirrors_migration():
    assert "CREATE TABLE IF NOT EXISTS auth.external_management" in _INIT
    for col in (
        "resource_type", "resource_id", "facet", "managed_by", "state",
        "value_state", "last_written_at", "token_id", "lineage_id", "request_id",
        "released_at", "released_by", "release_reason",
    ):
        assert col in _INIT
    assert "PRIMARY KEY (resource_type, resource_id, facet)" in _INIT
    assert "ck_external_management_state" in _INIT
