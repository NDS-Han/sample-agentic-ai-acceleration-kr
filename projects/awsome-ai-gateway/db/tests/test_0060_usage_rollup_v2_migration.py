# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0060 — daily_aggregates v2 입도 + 일자 레지스트리 + 월 누적 계층.

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0058/0059 테스트와 같은 방식).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_MIG = (_DB / "versions" / "0060_usage_rollup_v2.py").read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")

_U = "00000000-0000-4000-a000-000000000010"
_T = "00000000-0000-4000-a000-000000000003"
_D = "00000000-0000-4000-a000-000000000002"


def test_revision_chain():
    assert re.search(r'^revision = "0060"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0059"$', _MIG, re.M)


def test_init_sql_mirrors_tables_and_indexes():
    for name in ("usage.daily_aggregate_days", "usage.monthly_aggregates",
                 "usage.monthly_aggregate_months", "uq_daily_aggregates_grain",
                 "uq_monthly_aggregates_grain", "idx_monthly_aggregates_user_period",
                 "idx_daily_aggregate_days_dirty"):
        assert name in _MIG and name in _INIT, name
    grain = "(date, user_id, team_id, dept_id, model_alias, client, status)"
    assert grain in _MIG and grain in _INIT
    # 새 설치본에 옛 유니크가 다시 생기면 v2 입도 INSERT 가 깨진다.
    daily = _INIT.split("CREATE TABLE IF NOT EXISTS usage.daily_aggregates (", 1)[1].split(");", 1)[0]
    assert "UNIQUE (date, user_id, model_alias)" not in daily
    for col in ("client", "status", "call_count", "latency_ms_sum"):
        assert re.search(rf"^\s+{col}\s", daily, re.M), col


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=True,
                   env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


def _uniques() -> str:
    return _psql(
        "SELECT string_agg(indexdef, ' | ' ORDER BY indexname) FROM pg_indexes "
        "WHERE schemaname='usage' AND tablename='daily_aggregates' AND indexdef LIKE 'CREATE UNIQUE%'"
    ).stdout


def _legacy_row(day: str, model: str = "m-legacy") -> str:
    return (
        "INSERT INTO usage.daily_aggregates (date,user_id,team_id,dept_id,model_alias,"
        "input_tokens,output_tokens,total_tokens,total_cost_usd,request_count) "
        f"VALUES ('{day}','{_U}','{_T}','{_D}','{model}',10,5,15,0.5,1)"
    )


def _v2_row(day: str, status: str, client: str = "claude-code") -> str:
    return (
        "INSERT INTO usage.daily_aggregates (date,user_id,team_id,dept_id,model_alias,client,status,"
        "input_tokens,output_tokens,total_tokens,total_cost_usd,request_count,call_count) "
        f"VALUES ('{day}','{_U}','{_T}','{_D}','m1','{client}','{status}',1,1,2,0.1,1,1)"
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip_keeps_legacy_rows_and_new_grain():
    _alembic("upgrade", "head")
    _psql("DELETE FROM usage.daily_aggregates")
    _alembic("downgrade", "0059")
    assert "(date, user_id, model_alias)" in _uniques()
    assert _psql("SELECT to_regclass('usage.daily_aggregate_days') IS NULL").stdout.strip() == "t"

    # 0060 이전 입도의 행은 업그레이드 후에도 남는다(gateway /usage 합계 불변).
    _psql(_legacy_row("2026-10-01"))
    _alembic("upgrade", "0060")
    u = _uniques()
    assert "uq_daily_aggregates_grain" in u
    assert "(date, user_id, model_alias)" not in u, u
    assert _psql("SELECT count(*) FROM usage.daily_aggregates WHERE client IS NULL "
                 "AND status IS NULL AND call_count = 0").stdout.strip() == "1"

    # 새 입도 — 같은 (date,user,model) 에 status/client 별 행이 공존한다.
    _psql(_v2_row("2026-10-02", "SUCCESS"))
    _psql(_v2_row("2026-10-02", "ERROR"))
    _psql(_v2_row("2026-10-02", "SUCCESS", client="cowork"))
    assert _psql(_v2_row("2026-10-02", "SUCCESS"), check=False).returncode != 0  # 입도 중복
    # 레거시 행과 v2 행이 같은 날짜에 섞여도 유니크 충돌 없음(NULL 은 서로 다르다).
    _psql(_legacy_row("2026-10-02"))

    for t in ("daily_aggregate_days", "monthly_aggregates", "monthly_aggregate_months"):
        assert _psql(f"SELECT to_regclass('usage.{t}') IS NOT NULL").stdout.strip() == "t", t

    # 다운그레이드: v2 행은 옛 유니크를 위반하므로 지워지고 레거시 행만 남는다.
    _alembic("downgrade", "0059")
    assert "(date, user_id, model_alias)" in _uniques()
    assert _psql("SELECT count(*) FROM usage.daily_aggregates").stdout.strip() == "2"
    _alembic("upgrade", "head")
    _alembic("upgrade", "head")  # 멱등
    assert "uq_daily_aggregates_grain" in _uniques()
    _psql("DELETE FROM usage.daily_aggregates")
