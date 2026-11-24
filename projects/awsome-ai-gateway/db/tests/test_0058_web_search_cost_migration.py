# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""migration 0058 — usage_logs.web_search_cost_usd (nullable, 백필 없음) up/down.

정적 검사는 항상 돈다. 실 PG 왕복(upgrade → 데이터 → downgrade → upgrade)은
``MIGRATION_PG_URL`` (postgresql://… — alembic head 까지 올라간 **버려도 되는** DB)이
있을 때만 돈다. 실행 예:

    MIGRATION_PG_URL=postgresql://postgres:pw@127.0.0.1:55439/gateway \\
      uvx --from alembic==1.20.1 --with asyncpg==0.32.0 --with 'sqlalchemy[asyncio]==2.1.8' \\
          --with pytest pytest db/tests/test_0058_web_search_cost_migration.py

검증하는 계약
  - upgrade: NUMERIC(14,6) NULL, DEFAULT 없음 — 기존 행은 NULL("미추적")로 남는다(백필 없음).
  - NULL 과 0 은 구별돼 저장된다.
  - downgrade: 컬럼과 추적 시작 표지를 지우고, 관리자가 정한 단가는 남긴다.
  - 재-upgrade 가 멱등하게 수렴한다.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DB = Path(__file__).resolve().parents[1]
_MIG = (_DB / "versions" / "0058_usage_logs_web_search_cost.py").read_text()
_INIT = (_DB / "init" / "02_create_tables.sql").read_text()
PG_URL = os.environ.get("MIGRATION_PG_URL")


def test_revision_chain():
    assert re.search(r'^revision = "0058"$', _MIG, re.M)
    assert re.search(r'^down_revision = "0057"$', _MIG, re.M)


def test_upgrade_adds_nullable_column_without_default_or_backfill():
    assert "ADD COLUMN IF NOT EXISTS web_search_cost_usd NUMERIC(14,6)\"" in _MIG
    assert "DEFAULT" not in _MIG.split("def upgrade")[1].split("def downgrade")[0]
    assert "UPDATE usage.usage_logs" not in _MIG  # 백필 금지


def test_init_sql_mirrors_column_for_fresh_and_drifted_installs():
    assert re.search(r"\bweb_search_cost_usd\s+NUMERIC\(14,6\),", _INIT)
    assert "('web_search_cost_usd',         'NUMERIC(14,6)')" in _INIT


def _psql(sql: str) -> str:
    out = subprocess.run(
        ["psql", PG_URL, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _alembic(*args: str) -> None:
    env = {**os.environ, "DB_MASTER_URL": PG_URL}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_DB, env=env, check=True,
                   capture_output=True, text=True)


def _column() -> str:
    return _psql(
        "SELECT data_type||':'||numeric_precision||','||numeric_scale||':'||is_nullable||':'"
        "||coalesce(column_default,'-') FROM information_schema.columns "
        "WHERE table_schema='usage' AND table_name='usage_logs' "
        "AND column_name='web_search_cost_usd'"
    )


_FK = (
    "(SELECT id FROM auth.users LIMIT 1), (SELECT id FROM auth.teams LIMIT 1), "
    "(SELECT id FROM auth.departments LIMIT 1)"
)


def _insert(rid: str, extra_cols: str = "", extra_vals: str = "") -> None:
    _psql(
        "INSERT INTO usage.usage_logs (request_id, user_id, team_id, dept_id, model_alias, "
        "provider, input_tokens, output_tokens, web_search_count, cost_usd, latency_ms, status, "
        f"requested_at, completed_at{extra_cols}) VALUES ('{rid}', {_FK}, 'm', 'BEDROCK', 1, 1, "
        f"2, 0.01, 1, 'SUCCESS', now(), now(){extra_vals})"
    )


@pytest.mark.skipif(not PG_URL, reason="MIGRATION_PG_URL 미설정 — 실 PG 왕복은 건너뜀")
def test_up_down_roundtrip_on_real_postgres():
    assert _psql("SELECT count(*) FROM auth.users") != "0", "시드 사용자 필요(init SQL 03)"
    _psql("DELETE FROM usage.usage_logs WHERE request_id LIKE 'mig0058-%'")
    _alembic("downgrade", "0057")
    assert _column() == ""

    # 기능 이전 행 — 업그레이드 뒤에도 NULL(미추적)이어야 한다.
    _insert("mig0058-old")
    _alembic("upgrade", "0058")
    assert _column() == "numeric:14,6:YES:-"
    assert _psql(
        "SELECT web_search_cost_usd IS NULL FROM usage.usage_logs "
        "WHERE request_id='mig0058-old'"
    ) == "t"

    # NULL 과 0 이 구별돼 저장된다.
    _insert("mig0058-zero", ", web_search_cost_usd", ", 0")
    _insert("mig0058-paid", ", web_search_cost_usd", ", 0.014")
    assert _psql(
        "SELECT string_agg(request_id||'='||coalesce(web_search_cost_usd::text,'NULL'), ',' "
        "ORDER BY request_id) FROM usage.usage_logs WHERE request_id LIKE 'mig0058-%'"
    ) == "mig0058-old=NULL,mig0058-paid=0.014000,mig0058-zero=0.000000"

    _psql(
        "INSERT INTO public.system_settings(key, value) VALUES "
        "('web_search_pricing', '{\"price_per_1k_queries\": \"6.00\"}'), "
        "('web_search_cost_tracking_since', '{\"since\": \"2026-11-24T00:00:00+00:00\"}') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    _alembic("downgrade", "0057")
    assert _column() == ""
    assert _psql(
        "SELECT string_agg(key, ',' ORDER BY key) FROM public.system_settings "
        "WHERE key LIKE 'web_search%'"
    ) == "web_search_pricing"  # 표지는 지우고 단가는 남긴다

    _alembic("upgrade", "head")
    _alembic("upgrade", "head")  # 멱등
    assert _column() == "numeric:14,6:YES:-"
    # 다운그레이드로 값이 사라진 행은 다시 NULL(미추적) — 0(무료)으로 둔갑하지 않는다.
    assert _psql(
        "SELECT count(*) FROM usage.usage_logs WHERE request_id LIKE 'mig0058-%' "
        "AND web_search_cost_usd IS NOT NULL"
    ) == "0"
    _psql("DELETE FROM usage.usage_logs WHERE request_id LIKE 'mig0058-%'")
    _psql("DELETE FROM public.system_settings WHERE key LIKE 'web_search%'")
