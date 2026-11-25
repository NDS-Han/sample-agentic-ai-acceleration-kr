# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0063 — usage.request_starts + usage.cost_pending_events (wave 3b).

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0062 테스트와 같은 방식).
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
_MIG_PATH = _DB / "versions" / "0063_cost_durability.py"
_MIG = _MIG_PATH.read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("m0063", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    assert re.search(r'^revision = "0063"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0062"$', _MIG, re.M)


def test_lock_safe_statements():
    m = _load()
    assert m.LOCK_TIMEOUT.endswith("s") and int(m.LOCK_TIMEOUT[:-1]) <= 5
    assert m.MAX_ATTEMPTS >= 3
    assert "autocommit_block" in _MIG
    # 새 테이블만 — 기존 테이블(특히 usage_logs)에 대한 ALTER/INDEX 가 없다.
    for stmt in m._TABLES:
        low = " ".join(stmt.lower().split())
        assert "usage_logs" not in low, stmt
        assert "alter table" not in low, stmt
        assert low.startswith("create table if not exists") or low.startswith(
            "create index if not exists"), stmt


def test_init_sql_mirrors_objects():
    for name in ("usage.request_starts", "usage.cost_pending_events",
                 "idx_request_starts_open", "idx_request_starts_closed",
                 "idx_request_starts_swept", "idx_cost_pending_events_request"):
        assert name in _MIG and name in _INIT, name
    starts = _INIT.split("CREATE TABLE IF NOT EXISTS usage.request_starts (", 1)[1].split(");", 1)[0]
    for col in ("request_id", "instance_id", "started_at", "received_at", "payload",
                "closed_at", "swept_at", "swept_charge_usd"):
        assert re.search(rf"^\s+{col}\s", starts, re.M), col
    assert "PRIMARY KEY" in starts.split("request_id", 1)[1].split("\n", 1)[0]
    pend = _INIT.split("CREATE TABLE IF NOT EXISTS usage.cost_pending_events (", 1)[1].split(");", 1)[0]
    for col in ("id", "request_id", "kind", "payload", "created_at"):
        assert re.search(rf"^\s+{col}\s", pend, re.M), col
    # 마이그레이션과 init 의 열 정의가 같다(공백 무시).
    m = _load()

    def _cols(sql: str) -> list[str]:
        body = sql.split("(", 1)[1].rsplit(")", 1)[0]
        return [" ".join(c.split()) for c in body.strip().splitlines() if c.strip()]

    mig_starts = next(s for s in m._TABLES if "usage.request_starts (" in s)
    assert _cols(mig_starts) == _cols("(" + starts + ")")
    mig_pend = next(s for s in m._TABLES if "usage.cost_pending_events (" in s)
    assert _cols(mig_pend) == _cols("(" + pend + ")")


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=check,
                          env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip():
    _alembic("upgrade", "head")
    assert _psql("SELECT to_regclass('usage.request_starts') IS NOT NULL "
                 "AND to_regclass('usage.cost_pending_events') IS NOT NULL").stdout.strip() == "t"
    # 미처리 폴백 이벤트가 있으면 다운그레이드를 거부한다(아직 기록되지 않은 비용).
    _psql("INSERT INTO usage.cost_pending_events (request_id, kind, payload) VALUES ('r', 'end', '{}')")
    res = _alembic("downgrade", "0062", check=False)
    assert res.returncode != 0 and "unprocessed cost events" in (res.stdout + res.stderr)
    assert _psql("SELECT to_regclass('usage.cost_pending_events') IS NOT NULL").stdout.strip() == "t"
    _psql("DELETE FROM usage.cost_pending_events")
    _alembic("downgrade", "0062")
    assert _psql("SELECT to_regclass('usage.request_starts') IS NULL").stdout.strip() == "t"
    _alembic("upgrade", "head")
    _alembic("upgrade", "head")  # 멱등
    assert _psql("SELECT to_regclass('usage.cost_pending_events') IS NOT NULL").stdout.strip() == "t"


def _holder(hold_seconds: float) -> None:
    # 스키마 usage 에 ACCESS EXCLUSIVE 를 잡을 방법은 없으므로, 같은 이름의 테이블을 미리 만들어
    # 잠근 채로 두고 CREATE INDEX 가 lock_timeout 으로 재시도하는지 본다.
    subprocess.run(
        ["psql", PG_URL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
         f"BEGIN; LOCK TABLE usage.request_starts IN ACCESS EXCLUSIVE MODE; "
         f"SELECT pg_sleep({hold_seconds}); COMMIT;"],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_lock_retry_real_pg():
    """다른 세션이 테이블을 잠그고 있으면 인덱스 생성은 5s 에서 포기하고 재시도한다."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0062")
    # 인덱스 없이 테이블만 먼저 둔다(이전 시도가 중간에 끊긴 상태와 같다).
    m = _load()
    _psql(next(s for s in m._TABLES if "usage.request_starts (" in s))
    t = threading.Thread(target=_holder, args=(7.0,))
    t.start()
    time.sleep(1.0)
    t0 = time.monotonic()
    out = _alembic("upgrade", "0063")
    elapsed = time.monotonic() - t0
    t.join()
    assert "lock timeout (attempt 1)" in out.stdout, out.stdout + out.stderr
    assert elapsed >= 5.0, elapsed
    assert _psql("SELECT count(*) FROM pg_indexes WHERE schemaname='usage' "
                 "AND tablename='request_starts'").stdout.strip() == "4"
