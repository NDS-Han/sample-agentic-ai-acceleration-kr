# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0064/0065 — auth.integration_tokens + auth.integration_idempotency_keys (AICM /aicm/v1).

정적 검사는 항상, 실 PG 왕복은 ``MIGRATION_PG_URL`` 이 있을 때만(0063 테스트와 같은 방식).
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")
_MIGS = {
    "0064": _DB / "versions" / "0064_integration_tokens.py",
    "0065": _DB / "versions" / "0065_integration_idempotency.py",
}


def _load(rev: str):
    spec = importlib.util.spec_from_file_location(f"m{rev}", _MIGS[rev])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    t64, t65 = _MIGS["0064"].read_text(), _MIGS["0065"].read_text()
    assert re.search(r'^revision = "0064"$', t64, re.M) and re.search(r'^down_revision = "0063"$', t64, re.M)
    assert re.search(r'^revision = "0065"$', t65, re.M) and re.search(r'^down_revision = "0064"$', t65, re.M)


@pytest.mark.parametrize("rev", ["0064", "0065"])
def test_lock_safe_statements(rev):
    m = _load(rev)
    assert m.LOCK_TIMEOUT.endswith("s") and int(m.LOCK_TIMEOUT[:-1]) <= 5
    assert m.MAX_ATTEMPTS >= 3
    assert "autocommit_block" in _MIGS[rev].read_text()
    for stmt in m._TABLES:
        low = " ".join(stmt.lower().split())
        assert "usage_logs" not in low, stmt
        assert "alter table" not in low, stmt  # auth.service_tokens 도 ALTER 하지 않는다
        assert low.startswith("create table if not exists") or low.startswith("create index if not exists"), stmt


def _cols(sql: str) -> list[str]:
    body = sql.split("(", 1)[1].rsplit(")", 1)[0]
    return [" ".join(c.split()) for c in body.strip().splitlines() if c.strip()]


@pytest.mark.parametrize("rev,table,index", [
    ("0064", "auth.integration_tokens", "idx_integration_tokens_lineage"),
    ("0065", "auth.integration_idempotency_keys", "idx_integration_idempotency_created"),
])
def test_init_sql_mirrors_migration(rev, table, index):
    assert table in _INIT and index in _INIT
    init_body = _INIT.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(");", 1)[0]
    mig = next(s for s in _load(rev)._TABLES if f"{table} (" in s)
    assert _cols(mig) == _cols("(" + init_body + ")")


def _psql(sql: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          check=check, capture_output=True, text=True)


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, check=check,
                          env={**os.environ, "DB_MASTER_URL": PG_URL}, capture_output=True, text=True)


def _relfilenode(table: str) -> str:
    return _psql(f"SELECT relfilenode FROM pg_class WHERE oid = '{table}'::regclass").stdout.strip()


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip():
    _alembic("upgrade", "head")
    before = _relfilenode("auth.service_tokens")
    _alembic("downgrade", "0063")
    assert _psql("SELECT to_regclass('auth.integration_tokens') IS NULL "
                 "AND to_regclass('auth.integration_idempotency_keys') IS NULL").stdout.strip() == "t"
    _alembic("upgrade", "head")
    _alembic("upgrade", "head")  # 멱등
    assert _psql("SELECT to_regclass('auth.integration_tokens') IS NOT NULL "
                 "AND to_regclass('auth.integration_idempotency_keys') IS NOT NULL").stdout.strip() == "t"
    # 기존 hot 테이블은 재작성되지 않았다(ALTER 없음)
    assert _relfilenode("auth.service_tokens") == before
    # FK: 토큰 행 없이 권한 행을 만들 수 없다
    res = _psql("INSERT INTO auth.integration_tokens (token_id, integration, scopes, lineage_id) "
                "VALUES (gen_random_uuid(), 'aicm', '{users:read}', gen_random_uuid())", check=False)
    assert res.returncode != 0 and "foreign key" in res.stderr.lower()
    # 멱등 키 PK
    _psql("DELETE FROM auth.integration_idempotency_keys WHERE integration = 'mig-test'")
    _psql("INSERT INTO auth.integration_idempotency_keys (integration, idem_key, method, path, request_sha256) "
          "VALUES ('mig-test', 'k', 'PUT', '/x', 'h')")
    res = _psql("INSERT INTO auth.integration_idempotency_keys (integration, idem_key, method, path, request_sha256) "
                "VALUES ('mig-test', 'k', 'PUT', '/x', 'h')", check=False)
    assert res.returncode != 0
    _psql("DELETE FROM auth.integration_idempotency_keys WHERE integration = 'mig-test'")


def _hold_service_tokens(hold_seconds: float) -> None:
    subprocess.run(
        ["psql", PG_URL, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
         f"BEGIN; LOCK TABLE auth.service_tokens IN ACCESS EXCLUSIVE MODE; "
         f"SELECT pg_sleep({hold_seconds}); COMMIT;"],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_fk_creation_waits_at_most_5s_and_retries():
    """다른 세션이 auth.service_tokens 를 잠그고 있으면 FK 생성은 5s 에서 포기하고 재시도한다
    (svc- 토큰 인증 쿼리를 오래 막지 않는다)."""
    _alembic("upgrade", "head")
    _alembic("downgrade", "0063")
    t = threading.Thread(target=_hold_service_tokens, args=(8.0,))
    t.start()
    # 잠금이 실제로 잡힐 때까지 기다린다(고정 sleep 은 부하가 높을 때 잠금 전에 마이그레이션이 돈다).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        held = _psql("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                     "WHERE c.relname = 'service_tokens' AND l.mode = 'AccessExclusiveLock' "
                     "AND l.granted").stdout.strip()
        if held != "0":
            break
        time.sleep(0.1)
    t0 = time.monotonic()
    out = _alembic("upgrade", "0064")
    elapsed = time.monotonic() - t0
    t.join()
    assert "[0064] lock timeout (attempt 1)" in out.stdout, out.stdout + out.stderr
    assert elapsed >= 5.0, elapsed
    assert _psql("SELECT to_regclass('auth.integration_tokens') IS NOT NULL").stdout.strip() == "t"
    _alembic("upgrade", "head")
