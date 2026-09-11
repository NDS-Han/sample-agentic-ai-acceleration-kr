# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Every Python mirror of a Postgres enum must list EVERY label the DB can hold.

## The bug class this pins

``model.provider`` is a native Postgres enum. Four Python files mirror it:

    gateway-proxy/src/app/models/model.py   _provider_enum   (SQLAlchemy, on READ)
    gateway-proxy/src/app/schemas/domain.py ProviderType     (dispatch)
    admin-api/src/app/models/model.py       Provider         (SQLAlchemy, on READ)
    admin-api/src/app/schemas/common.py     ProviderEnum     (request validation)

The SQLAlchemy ones are the dangerous pair, because ``Enum`` validates on **read**: a row
whose label is not in the Python list raises ``LookupError`` while the result set is being
fetched. So one unlisted label does not break "that model" — it breaks every query that
touches ``model_aliases``, i.e. the whole model catalogue and every request that resolves
a model. Adding an ``ALTER TYPE ... ADD VALUE`` migration and forgetting a mirror is
therefore a full outage triggered by a single INSERT, and it is invisible until a row
actually uses the new label.

Measured against real Postgres 16 at revision 0033 (not reasoned from the docs): with the
0031 label removed from the SQLAlchemy mirror, a targeted read of one PRE-EXISTING row
still returns fine, while selecting the catalogue raises

    LookupError: 'BEDROCK_RUNTIME_OPENAI' is not among the defined enum values.

and with the label restored the same query returns all 20 rows. That asymmetry is what
makes the drift so dangerous: smoke tests that fetch one known-good model keep passing
while every listing and every unfiltered resolve is already broken.

This exact drift shipped twice before in this repo (notification-worker ``user_role`` as
``String(20)`` vs the real ``auth.user_role`` enum, and the 0016 Mantle rollout), which is
why it is pinned here rather than left to review.

## Why the expected set is DERIVED, not written down

Hardcoding the label list in the test would make the test another mirror that can drift —
it would pass while being wrong. Instead the DB's own definition is reconstructed from the
two places that actually create labels:

    db/init/02_create_tables.sql    CREATE TYPE ... AS ENUM (...)
    db/versions/*.py                ALTER TYPE ... ADD VALUE ... 'LABEL'

so adding a migration is enough to make this test demand the mirrors be updated.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = REPO / "db" / "init" / "02_create_tables.sql"
VERSIONS = REPO / "db" / "versions"

_CREATE_RE = re.compile(
    r"CREATE\s+TYPE\s+(?P<schema>\w+)\.(?P<name>\w+)\s+AS\s+ENUM\s*\((?P<body>[^)]*)\)",
    re.IGNORECASE,
)
_ADD_RE = re.compile(
    r"ALTER\s+TYPE\s+(?P<schema>\w+)\.(?P<name>\w+)\s+ADD\s+VALUE"
    r"(?:\s+IF\s+NOT\s+EXISTS)?\s+'(?P<label>[^']+)'",
    re.IGNORECASE,
)


def _db_enum_labels(schema: str, name: str) -> set[str]:
    """Reconstruct a Postgres enum's label set from init SQL + every migration."""
    labels: set[str] = set()
    sql = INIT_SQL.read_text(encoding="utf-8")
    for m in _CREATE_RE.finditer(sql):
        if m.group("schema").lower() == schema and m.group("name").lower() == name:
            labels |= {v.strip().strip("'") for v in m.group("body").split(",") if v.strip()}
    # Migrations only ever ADD; this repo never drops a label (Postgres cannot without
    # recreating the type, which 0008/0016/0031 all document as a deliberate no-op).
    for path in sorted(VERSIONS.glob("*.py")):
        for m in _ADD_RE.finditer(path.read_text(encoding="utf-8")):
            if m.group("schema").lower() == schema and m.group("name").lower() == name:
                labels.add(m.group("label"))
    return labels


def _string_list_arg(path: Path, var: str) -> set[str]:
    """String literals passed positionally to a call assigned to ``var``.

    AST, not grep: a regex over ``"BEDROCK...",`` would also match the label inside a
    docstring or a comment and report a mirror as up to date when it is not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if var in targets:
            return {
                a.value
                for a in node.value.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            }
    raise AssertionError(f"{var} not found as a call assignment in {path}")


def _enum_class_values(path: Path, class_name: str) -> set[str]:
    """``str, Enum`` member values, read from the AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            out = set()
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    out.add(stmt.value.value)
            return out
    raise AssertionError(f"class {class_name} not found in {path}")


# (label, path, extractor) — every Python mirror of model.provider in the repo.
PROVIDER_MIRRORS = [
    (
        "gateway-proxy models.model._provider_enum (SQLAlchemy, validates on READ)",
        REPO / "gateway-proxy/src/app/models/model.py",
        lambda p: _string_list_arg(p, "_provider_enum"),
    ),
    (
        "gateway-proxy schemas.domain.ProviderType (dispatch)",
        REPO / "gateway-proxy/src/app/schemas/domain.py",
        lambda p: _enum_class_values(p, "ProviderType"),
    ),
    (
        "admin-api models.model.Provider (SQLAlchemy, validates on READ)",
        REPO / "admin-api/src/app/models/model.py",
        lambda p: _enum_class_values(p, "Provider"),
    ),
    (
        "admin-api schemas.common.ProviderEnum (request validation)",
        REPO / "admin-api/src/app/schemas/common.py",
        lambda p: _enum_class_values(p, "ProviderEnum"),
    ),
]

API_FORMAT_MIRRORS = [
    (
        "gateway-proxy models.model._api_format_enum",
        REPO / "gateway-proxy/src/app/models/model.py",
        lambda p: _string_list_arg(p, "_api_format_enum"),
    ),
    (
        "admin-api models.model.ApiFormat",
        REPO / "admin-api/src/app/models/model.py",
        lambda p: _enum_class_values(p, "ApiFormat"),
    ),
    (
        "admin-api schemas.common.ApiFormatEnum",
        REPO / "admin-api/src/app/schemas/common.py",
        lambda p: _enum_class_values(p, "ApiFormatEnum"),
    ),
]


def test_db_provider_enum_labels_are_discoverable():
    """Guard the guard: if the SQL parsing breaks, every assertion below passes vacuously."""
    labels = _db_enum_labels("model", "provider")
    assert "BEDROCK" in labels, "init SQL parse failed — CREATE TYPE not found"
    assert "BEDROCK_MANTLE" in labels, "migration parse failed — 0008 ADD VALUE not found"
    assert "BEDROCK_RUNTIME_OPENAI" in labels, "migration parse failed — 0031 ADD VALUE not found"
    assert len(labels) >= 5


@pytest.mark.parametrize("name,path,extract", PROVIDER_MIRRORS, ids=[m[0] for m in PROVIDER_MIRRORS])
def test_provider_mirror_covers_every_db_label(name, path, extract):
    db = _db_enum_labels("model", "provider")
    mirror = extract(path)
    missing = db - mirror
    assert not missing, (
        f"{name} is missing {sorted(missing)}.\n"
        f"model.provider can hold {sorted(db)}.\n"
        "A SQLAlchemy mirror raises LookupError while FETCHING a row with an unlisted "
        "label, so this breaks the entire model catalogue, not just the new row."
    )


@pytest.mark.parametrize("name,path,extract", PROVIDER_MIRRORS, ids=[m[0] for m in PROVIDER_MIRRORS])
def test_provider_mirror_invents_no_label(name, path, extract):
    """The reverse drift: a Python label the DB cannot store is an INSERT that 500s."""
    db = _db_enum_labels("model", "provider")
    extra = extract(path) - db
    assert not extra, (
        f"{name} lists {sorted(extra)}, which model.provider cannot store. "
        "Add an ALTER TYPE ... ADD VALUE migration first (see 0031)."
    )


@pytest.mark.parametrize(
    "name,path,extract", API_FORMAT_MIRRORS, ids=[m[0] for m in API_FORMAT_MIRRORS]
)
def test_api_format_mirror_covers_every_db_label(name, path, extract):
    db = _db_enum_labels("model", "api_format")
    assert "BEDROCK_NATIVE" in db, "api_format SQL parse failed"
    missing = db - extract(path)
    assert not missing, f"{name} is missing {sorted(missing)} from model.api_format"


def test_runtime_openai_provider_is_actually_importable():
    """The AST checks prove the text; this proves the module really exposes the member.

    Catches the case where the label sits inside an ``if TYPE_CHECKING`` block or a class
    that fails to import — the AST would find it and the process would still crash.
    """
    sys.path.insert(0, str(REPO / "gateway-proxy" / "src"))
    from app.schemas.domain import ProviderType

    assert ProviderType("BEDROCK_RUNTIME_OPENAI") is ProviderType.BEDROCK_RUNTIME_OPENAI
    assert ProviderType.BEDROCK_RUNTIME_OPENAI.value == "BEDROCK_RUNTIME_OPENAI"
