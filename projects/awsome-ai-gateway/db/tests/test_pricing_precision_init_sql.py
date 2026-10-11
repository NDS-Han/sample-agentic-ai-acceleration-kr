"""단가 열 정밀도 NUMERIC(12,8) — init SQL 정적 가드 (US-19, 2026-10-10).

Haiku 5.5 의 US(Regional) 5분 캐시 쓰기 단가는 0.0001375 / 1K 로 소수 7자리다. 6자리 열에서는
관리 API 가 422 로 거부하고, SQL 로 넣으면 Postgres 가 0.000138 로 조용히 반올림한다.
정본(phase-2 975219a, 마이그레이션 0043)과 같이 단가 열을 (12,8) 로 넓힌다.

fork 는 alembic 파일을 넣지 않는다(마이그레이션 번호가 upstream 과 엇갈림). 대신 정본이 함께 둔
init SQL 미러를 쓴다: migration Job 은 배포마다 init/*.sql 을 먼저 돌리므로, 멱등 DO 블록이
기존 DB 도 넓힌다. 금액 열 usage_logs.cost_usd 는 6자리 그대로다(단가가 아니라 금액).

English: price columns are NUMERIC(12,8); an idempotent DO block in init SQL widens existing
databases (the migration Job runs init SQL on every deploy); cost_usd stays NUMERIC(10,6).
"""

from __future__ import annotations

import re
from pathlib import Path

SQL = (
    Path(__file__).resolve().parents[1] / "init" / "02_create_tables.sql"
).read_text()

RATE_COLS = (
    "input_price_per_1k_tokens",
    "output_price_per_1k_tokens",
    "cache_creation_5m_price_per_1k_tokens",
    "cache_creation_1h_price_per_1k_tokens",
    "cache_read_price_per_1k_tokens",
    "long_context_input_price_per_1k_tokens",
    "long_context_output_price_per_1k_tokens",
    "long_context_cache_creation_5m_price_per_1k_tokens",
    "long_context_cache_creation_1h_price_per_1k_tokens",
    "long_context_cache_read_price_per_1k_tokens",
)


def test_every_rate_column_is_declared_numeric_12_8():
    for col in RATE_COLS:
        types = re.findall(rf"\b{col}\s+NUMERIC\((\d+),\s*(\d+)\)", SQL)
        assert types, f"{col} not declared in init SQL"
        assert set(types) == {("12", "8")}, f"{col}: {types}"


def test_existing_databases_are_widened_by_an_idempotent_block():
    blocks = re.findall(r"DO \$\$.*?END \$\$;", SQL, re.DOTALL)
    widen = [b for b in blocks if "model_pricings" in b and "numeric_scale" in b]
    assert len(widen) == 1, "one DO block widens model.model_pricings"
    block = widen[0]
    assert re.search(r"numeric_scale\s*<\s*8", block), "only columns not yet at scale 8"
    assert (
        "ALTER TABLE model.model_pricings ALTER COLUMN %I TYPE NUMERIC(12,8)" in block
    )


def test_cost_usd_stays_a_six_place_money_amount():
    assert re.search(r"\bcost_usd\s+NUMERIC\(10,6\)", SQL)


# ── 새로 설치하는 경로: init SQL 뒤에 alembic 이 표를 다시 만든다 ─────────────────
# 새 DB 에서는 init SQL((12,8)) → 0003(표를 DROP 후 재생성) → 0042(long_context 열 추가) 순서라,
# 이 두 파일이 (10,6) 이면 첫 배포는 (10,6) 으로 끝나고 8자리 단가가 조용히 반올림된다
# (2026-10-10 pgvector:pg16 실측). 기존 DB 는 두 revision 을 다시 돌지 않으므로 DO 블록이 넓힌다.
# English: on a fresh install alembic 0003 recreates the table and 0038 adds the long-context
# columns after init SQL ran, so both must declare NUMERIC(12,8) too.

VERSIONS = Path(__file__).resolve().parents[1] / "versions"


def test_fresh_install_0003_recreates_rate_columns_at_12_8():
    src = (VERSIONS / "0003_rename_cache_5m.py").read_text()
    upgrade = src.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    for col in RATE_COLS[:5]:
        types = re.findall(rf"\b{col}\s+NUMERIC\((\d+),\s*(\d+)\)", upgrade)
        assert types == [("12", "8")], f"0003 upgrade {col}: {types}"


def test_fresh_install_0042_adds_long_context_columns_at_12_8():
    # upstream 의 0038 — 우리 체인에서는 0042 로 리번호됐다(0041 alert_thresholds 충돌 해소).
    src = (VERSIONS / "0042_add_long_context_pricing.py").read_text()
    upgrade = src.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "ADD COLUMN IF NOT EXISTS {col} NUMERIC(12,8)" in upgrade
    assert "NUMERIC(10,6)" not in upgrade
