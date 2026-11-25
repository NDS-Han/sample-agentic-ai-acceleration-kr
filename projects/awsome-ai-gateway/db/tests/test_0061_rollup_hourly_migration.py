# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0061 — 웹 검색 롤업 열 + 시간별 롤업 + dirty 표시 + 실패 기록.

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0060 테스트와 같은 방식).
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
_MIG_PATH = _DB / "versions" / "0061_rollup_hourly_ws_dirty_marks.py"
_MIG = _MIG_PATH.read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("m0061", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    assert re.search(r'^revision = "0061"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0060"$', _MIG, re.M)


def test_lock_safe_statements():
    m = _load()
    # lock_timeout ≤ 5s, 재시도 있음, usage_logs 는 건드리지 않는다.
    assert m.LOCK_TIMEOUT.endswith("s") and int(m.LOCK_TIMEOUT[:-1]) <= 5
    assert m.MAX_ATTEMPTS >= 3
    for stmt in m._UPGRADE:
        assert "usage_logs" not in stmt, stmt
        # 재작성 유발 형태 금지: 휘발성 DEFAULT / 형 변경
        assert "ALTER COLUMN" not in stmt.upper(), stmt
        # 기존 테이블에 휘발성 DEFAULT 를 붙이지 않는다(새 테이블 정의 안의 now() 는 무관).
        if stmt.lstrip().upper().startswith("ALTER TABLE"):
            assert "now()" not in stmt.lower() and "random" not in stmt.lower(), stmt
    assert "autocommit_block" in _MIG


def test_init_sql_mirrors_objects():
    for name in ("usage.hourly_aggregates", "usage.hourly_aggregate_hours",
                 "usage.rollup_dirty_marks", "uq_hourly_aggregates_grain",
                 "idx_hourly_aggregates_date", "idx_hourly_aggregate_hours_date",
                 "idx_rollup_dirty_marks_date", "idx_rollup_dirty_marks_hour"):
        assert name in _MIG and name in _INIT, name
    for table in ("daily_aggregates", "monthly_aggregates"):
        body = _INIT.split(f"CREATE TABLE IF NOT EXISTS usage.{table} (", 1)[1].split(");", 1)[0]
        for col in ("web_search_count", "web_search_cost_usd", "web_search_untracked"):
            assert re.search(rf"^\s+{col}\s", body, re.M), (table, col)
    days = _INIT.split("CREATE TABLE IF NOT EXISTS usage.daily_aggregate_days (", 1)[1].split(");", 1)[0]
    for col in ("failed_at", "last_error"):
        assert re.search(rf"^\s+{col}\s", days, re.M), col
    months = _INIT.split("CREATE TABLE IF NOT EXISTS usage.monthly_aggregate_months (", 1)[1].split(");", 1)[0]
    assert re.search(r"^\s+grain_version\s", months, re.M)
    # 기존 설치본 수렴 블록(ADD COLUMN) 이 init 에 있다.
    for col in ("web_search_untracked", "failed_at", "last_error", "grain_version"):
        assert f"'{col}'" in _INIT, col


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=True,
                          env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


def _cols(table: str) -> set[str]:
    out = _psql("SELECT string_agg(column_name, ',') FROM information_schema.columns "
                f"WHERE table_schema='usage' AND table_name='{table}'").stdout.strip()
    return set(out.split(",")) if out else set()


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip():
    _alembic("upgrade", "head")
    _alembic("downgrade", "0060")
    assert "web_search_count" not in _cols("daily_aggregates")
    assert _psql("SELECT to_regclass('usage.hourly_aggregates') IS NULL").stdout.strip() == "t"
    _alembic("upgrade", "0061")
    for t in ("daily_aggregates", "monthly_aggregates"):
        assert {"web_search_count", "web_search_cost_usd", "web_search_untracked"} <= _cols(t)
    assert {"failed_at", "last_error"} <= _cols("daily_aggregate_days")
    assert "grain_version" in _cols("monthly_aggregate_months")
    for t in ("hourly_aggregates", "hourly_aggregate_hours", "rollup_dirty_marks"):
        assert _psql(f"SELECT to_regclass('usage.{t}') IS NOT NULL").stdout.strip() == "t", t
    _alembic("upgrade", "head")  # 멱등


def _holder(hold_seconds: float) -> None:
    # psql 세션 하나가 트랜잭션 안에서 테이블을 읽고(ACCESS SHARE) hold_seconds 동안 유지.
    subprocess.run(
        ["psql", PG_URL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
         f"BEGIN; SELECT count(*) FROM usage.daily_aggregates; "
         f"SELECT pg_sleep({hold_seconds}); COMMIT;"],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_lock_retry_real_pg():
    """다른 세션이 daily_aggregates 를 잡고 있으면 5s 에서 포기하고 재시도 → 잠금이 풀리면 성공."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0060")
    t = threading.Thread(target=_holder, args=(7.0,))
    t.start()
    time.sleep(1.0)  # holder 가 잠금을 잡을 시간
    t0 = time.monotonic()
    out = _alembic("upgrade", "0061")
    elapsed = time.monotonic() - t0
    t.join()
    assert "lock timeout (attempt 1)" in out.stdout, out.stdout + out.stderr
    assert elapsed >= 5.0, elapsed  # 첫 시도가 5s 에서 포기했고
    assert "web_search_count" in _cols("daily_aggregates")  # 재시도로 성공했다
