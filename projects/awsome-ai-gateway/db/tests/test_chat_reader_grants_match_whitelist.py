# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""schema_whitelist.yaml 의 노출 컬럼 ⊆ GRANT 부여 컬럼 — 드리프트 가드.

kiro R17-5 S2 실사고: whitelist 가 ``usage_logs.client``/``reasoning_tokens``
를 노출하는데 ``08_create_chat_reader.sql`` 의 GRANT 에는 없어, 앱별 질의가
sqlglot 통과 후 ``permission denied`` 로 죽었다.

주의 — 08_*.sql 은 alembic head **이전**에 실행되므로 마이그레이션이 만드는
컬럼(0007 `client`, 0019 `reasoning_tokens`)은 거기에 넣을 수 없다. 그래서
GRANT 소스는 ``init/*.sql`` + ``versions/*.py`` 양쪽을 합쳐 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # db/
INIT_DIR = ROOT / "init"
VERSIONS_DIR = ROOT / "versions"
WHITELIST = ROOT.parent / "admin-chat-agent" / "config" / "schema_whitelist.yaml"


def _whitelist_columns() -> dict[tuple[str, str], set[str]]:
    """yaml 을 최소 파싱 — PyYAML 미설치 환경(db 테스트)에서도 돌도록."""
    grants: dict[tuple[str, str], set[str]] = {}
    schema = table = None
    in_columns = False
    for line in WHITELIST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s+-?\s*schema:\s*(\w+)", line)
        if m:
            schema = m.group(1)
            in_columns = False
            continue
        m = re.match(r"^\s+-?\s*table:\s*(\w+)", line)
        if m:
            table = m.group(1)
            in_columns = False
            continue
        if re.match(r"^\s+columns:\s*$", line):
            in_columns = True
            continue
        m = re.match(r"^\s+-?\s*name:\s*(\w+)", line)
        if m and in_columns and schema and table:
            grants.setdefault((schema, table), set()).add(m.group(1))
    return grants


def _granted_columns() -> dict[tuple[str, str], set[str]]:
    """init/*.sql + versions/*.py 의 `GRANT SELECT (cols) ON schema.table` 집계."""
    granted: dict[tuple[str, str], set[str]] = {}
    pat = re.compile(
        r"GRANT\s+SELECT\s*\(([^)]*)\)\s*ON\s*(\w+)\.(\w+)", re.IGNORECASE
    )
    files = list(INIT_DIR.glob("*.sql")) + list(VERSIONS_DIR.glob("*.py"))
    for f in files:
        for cols, schema, table in pat.findall(f.read_text(encoding="utf-8")):
            granted.setdefault((schema, table), set()).update(
                c.strip() for c in cols.split(",") if c.strip()
            )
    return granted


def test_whitelisted_columns_are_all_granted():
    missing = []
    granted = _granted_columns()
    for (schema, table), cols in _whitelist_columns().items():
        for col in cols - granted.get((schema, table), set()):
            missing.append(f"{schema}.{table}.{col}")
    assert not missing, (
        "whitelist 노출 컬럼인데 어느 GRANT 에도 없음 — 쿼리 시 permission denied.\n"
        "  마이그레이션 생성 컬럼이면 versions/*.py 에 GRANT 를 추가할 것\n"
        "  (08_*.sql 은 alembic 이전에 실행돼 신규 컬럼에 GRANT 불가): "
        + ", ".join(missing)
    )
