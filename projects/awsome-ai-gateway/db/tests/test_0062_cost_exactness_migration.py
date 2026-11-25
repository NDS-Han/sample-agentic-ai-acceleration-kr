# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0062 — CANCELLED, pricing_missing, cost_dead_letters, budget_usages NUMERIC(16,6),
reconciliation_runs.

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0061 테스트와 같은 방식).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_MIG_PATH = _DB / "versions" / "0062_cost_exactness.py"
_MIG = _MIG_PATH.read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("m0062", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    assert re.search(r'^revision = "0062"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0061"$', _MIG, re.M)


def test_lock_safe_statements():
    m = _load()
    assert m.LOCK_TIMEOUT.endswith("s") and int(m.LOCK_TIMEOUT[:-1]) <= 5
    assert m.MAX_ATTEMPTS >= 3
    # enum 값 추가는 autocommit(트랜잭션 밖) 경로로만 실행된다.
    assert "ADD VALUE IF NOT EXISTS 'CANCELLED'" in m._ENUM_STMT
    assert "autocommit_block" in _MIG
    # usage_logs: 상수 DEFAULT 만(휘발성 없음), 형 변경 없음, 인덱스는 CONCURRENTLY.
    assert "DEFAULT false" in m._PRICING_MISSING_DDL
    for stmt in (m._PRICING_MISSING_DDL, m._PRICING_MISSING_INDEX_DDL, *m._TABLES):
        low = stmt.lower()
        if "usage_logs" in low:
            assert "alter column" not in low, stmt
            assert "now()" not in low and "random" not in low, stmt
    assert "CONCURRENTLY" in m._PRICING_MISSING_INDEX_DDL
    # 형 변경은 작은 budget_usages 에만.
    assert "budget.budget_usages" in m._BUDGET_PRECISION
    assert "NUMERIC(16,6)" in m._BUDGET_PRECISION


def test_init_sql_mirrors_objects():
    assert "'TIMEOUT', 'CANCELLED')" in _INIT
    usage_logs = _INIT.split("CREATE TABLE IF NOT EXISTS usage.usage_logs (", 1)[1].split(");", 1)[0]
    assert re.search(r"^\s+pricing_missing\s+BOOLEAN\s+NOT NULL DEFAULT false", usage_logs, re.M)
    # 기존 설치본 수렴 블록에도 있다.
    assert "('pricing_missing',             'BOOLEAN NOT NULL DEFAULT false')" in _INIT
    budget = _INIT.split("CREATE TABLE IF NOT EXISTS budget.budget_usages (", 1)[1].split(");", 1)[0]
    assert re.search(r"used_usd\s+NUMERIC\(16,6\)", budget)
    for name in ("usage.cost_dead_letters", "usage.reconciliation_runs",
                 "uq_cost_dead_letters_request", "idx_cost_dead_letters_open",
                 "idx_reconciliation_runs_started", "idx_usage_logs_pricing_missing"):
        assert name in _MIG and name in _INIT, name
    # init 의 부분 인덱스는 빈 테이블에서만 — 행 있는 설치본은 alembic 의 CONCURRENTLY 몫.
    block = _INIT.split("idx_usage_logs_pricing_missing", 1)[0].rsplit("DO $$", 1)[1]
    assert "NOT EXISTS (SELECT 1 FROM usage.usage_logs LIMIT 1)" in block


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=True,
                          env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip():
    _alembic("upgrade", "head")
    labels = _psql("SELECT string_agg(enumlabel, ',' ORDER BY enumsortorder) FROM pg_enum e "
                   "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'usage_status'").stdout
    assert "CANCELLED" in labels
    assert _psql("SELECT numeric_scale FROM information_schema.columns WHERE table_schema='budget' "
                 "AND table_name='budget_usages' AND column_name='used_usd'").stdout.strip() == "6"
    assert _psql("SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                 "WHERE c.relname='idx_usage_logs_pricing_missing'").stdout.strip() == "t"
    _alembic("downgrade", "0061")
    assert _psql("SELECT to_regclass('usage.reconciliation_runs') IS NULL").stdout.strip() == "t"
    _alembic("upgrade", "head")  # 멱등
    assert _psql("SELECT to_regclass('usage.cost_dead_letters') IS NOT NULL").stdout.strip() == "t"


def _holder(hold_seconds: float) -> None:
    subprocess.run(
        ["psql", PG_URL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
         f"BEGIN; SELECT count(*) FROM budget.budget_usages; "
         f"SELECT pg_sleep({hold_seconds}); COMMIT;"],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_budget_precision_lock_retry_real_pg():
    """다른 세션이 budget_usages 를 읽고 있으면 형 변경은 5s 에서 포기하고 재시도한다."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0061")
    _psql("ALTER TABLE budget.budget_usages ALTER COLUMN used_usd TYPE NUMERIC(12,4)")
    t = threading.Thread(target=_holder, args=(7.0,))
    t.start()
    time.sleep(1.0)
    t0 = time.monotonic()
    out = _alembic("upgrade", "0062")
    elapsed = time.monotonic() - t0
    t.join()
    assert "lock timeout (attempt 1)" in out.stdout, out.stdout + out.stderr
    assert elapsed >= 5.0, elapsed
    assert _psql("SELECT numeric_scale FROM information_schema.columns WHERE table_schema='budget' "
                 "AND table_name='budget_usages' AND column_name='used_usd'").stdout.strip() == "6"
