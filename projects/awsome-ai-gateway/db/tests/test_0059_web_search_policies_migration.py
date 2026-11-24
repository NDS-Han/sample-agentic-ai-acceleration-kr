# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0059 — budget.web_search_policies up/down + 제약.

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0058 테스트와 같은 방식).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_MIG = (_DB / "versions" / "0059_web_search_policies.py").read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def test_revision_chain():
    assert re.search(r'^revision = "0059"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0058"$', _MIG, re.M)


def test_init_sql_mirrors_table_and_constraints():
    assert "CREATE TABLE IF NOT EXISTS budget.web_search_policies" in _INIT
    for c in ("ck_wsp_scope", "ck_wsp_scope_id", "ck_wsp_limit", "ck_wsp_cap",
              "uq_web_search_policies_scope"):
        assert c in _MIG and c in _INIT, c
    assert "per_request_cap BETWEEN 1 AND 50" in _MIG and "per_request_cap BETWEEN 1 AND 50" in _INIT


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=True,
                   env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


def _exists() -> bool:
    return _psql("SELECT to_regclass('budget.web_search_policies') IS NOT NULL").stdout.strip() == "t"


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip_and_constraints():
    _alembic("downgrade", "0058")
    assert not _exists()
    _alembic("upgrade", "0059")
    assert _exists()
    team = "00000000-0000-4000-a000-000000000003"
    _psql("DELETE FROM budget.web_search_policies")
    _psql("INSERT INTO budget.web_search_policies(scope, monthly_query_limit) VALUES ('ORG', 100)")
    _psql(f"INSERT INTO budget.web_search_policies(scope, scope_id, monthly_query_limit, "
          f"per_request_cap) VALUES ('TEAM', '{team}', 0, 3)")
    bad = [
        "INSERT INTO budget.web_search_policies(scope) VALUES ('ORG')",            # ORG 두 번째
        f"INSERT INTO budget.web_search_policies(scope, scope_id) VALUES ('TEAM', '{team}')",
        f"INSERT INTO budget.web_search_policies(scope, scope_id) VALUES ('ORG', '{team}')",
        "INSERT INTO budget.web_search_policies(scope) VALUES ('TEAM')",           # scope_id 필수
        "INSERT INTO budget.web_search_policies(scope) VALUES ('DEPT')",
        f"INSERT INTO budget.web_search_policies(scope, scope_id, per_request_cap) "
        f"VALUES ('USER', '{team}', 0)",
        f"INSERT INTO budget.web_search_policies(scope, scope_id, monthly_query_limit) "
        f"VALUES ('USER', '{team}', -1)",
    ]
    for sql in bad:
        assert _psql(sql, check=False).returncode != 0, sql
    _alembic("downgrade", "0058")
    assert not _exists()
    _alembic("upgrade", "head")
    _alembic("upgrade", "head")
    assert _exists()
