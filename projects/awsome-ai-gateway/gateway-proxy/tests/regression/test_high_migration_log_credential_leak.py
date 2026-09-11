# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: db/env.py 가 DB 비밀번호를 migration Job stdout 에 찍어서는 안 된다.

Bug: 디버그 출력이 `db_url[:40]` 이었고 주석에는 "mask password" 라고 적혀 있었지만
마스킹이 아니었다. `postgresql+asyncpg://<user>:` 접두사가 30~35자
(`postgres`=30, prod master 인 `postgres_admin`=35)이므로 40자 컷은 비밀번호 앞
5~10자를 그대로 노출한다. 실측(로컬 PG16, 2026-09-09):

    [ENV.PY DEBUG] Using URL starting with: postgresql+asyncpg://postgres:NOT-A-REAL-PW...

migration Job 은 매 helm install/upgrade 마다 dev·prod 양쪽에서 실행되고 stdout 은
CloudWatch Logs 에 보존되므로 master 비번 앞부분이 로그에 누적된다.

Fix: userinfo 구간(`://user:pass@`) 을 정규식으로 제거하는 `_redact_credentials`.

이 테스트는 문자열 grep 이 아니라 **함수 본문을 AST 로 떼어내 실제로 실행**해서
리댁션 동작을 검증하고, 추가로 `print` 안에서 db_url 이 리댁션을 거치는지도 AST 로
확인한다(주석/문자열에 우연히 들어간 패턴에 속지 않기 위해).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # regression -> tests -> gateway-proxy -> root
ENV_PY = PROJECT_ROOT / "db" / "env.py"


def _load_redactor():
    """db/env.py 에서 _redact_credentials 만 떼어내 실행 가능한 함수로 만든다.

    env.py 를 그대로 import 하면 alembic context 가 필요하므로 불가능하다.
    """
    source = ENV_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_redact_credentials":
            module = ast.Module(body=[node], type_ignores=[])
            ns: dict = {"re": re}
            exec(compile(module, str(ENV_PY), "exec"), ns)  # noqa: S102 - 테스트 전용
            return ns["_redact_credentials"]
    pytest.fail("db/env.py 에 _redact_credentials 가 없다 — 디버그 출력이 리댁션을 거치는지 확인")


# ⚠️ 픽스처 비밀번호는 전부 한눈에 가짜여야 한다. 이 파일은 커밋되므로 실제 값을 쓰면
#    이 테스트가 막으려는 바로 그 유출을 스스로 저지른다. 리댁션 검증에 필요한 건 값의
#    진짜 여부가 아니라 `://<user>:<pw>@` 라는 모양뿐이다(hex 처럼 보이는 문자열은
#    실제 비번으로 오독될 수 있어 피한다).
@pytest.mark.parametrize(
    ("url", "secret"),
    [
        (
            "postgresql+asyncpg://postgres:NOT-A-REAL-PASSWORD@127.0.0.1:5432/gateway",
            "NOT-A-REAL-PASSWORD",
        ),
        # prod 실제 형태: master 유저명이 길어서 40자 컷이 특히 위험했던 케이스
        ("postgresql+asyncpg://postgres_admin:S3cr3tPw@aurora.internal:5432/gateway", "S3cr3tPw"),
        # 비밀번호에 인코딩되지 않은 @ 가 섞여도 마지막 @ 까지 지워야 한다
        ("postgresql://postgres:pw@with@host:5432/gateway", "pw@with"),
        # sslmode 쿼리스트링이 붙은 형태 (Helm 이 넘기는 모양)
        ("postgresql://postgres:abcdef123@host:5432/gateway?sslmode=require", "abcdef123"),
    ],
)
def test_redaction_removes_the_password(url: str, secret: str):
    redact = _load_redactor()
    out = redact(url)
    assert secret not in out, f"비밀번호가 남았다: {out}"
    assert "<redacted>" in out, f"리댁션 표시가 없다: {out}"


def test_redaction_keeps_host_and_database_for_diagnostics():
    """진단 가치는 유지 — 어느 호스트/DB 에 붙었는지는 보여야 한다."""
    redact = _load_redactor()
    out = redact("postgresql+asyncpg://postgres:secret@aurora.internal:5432/gateway")
    assert "aurora.internal:5432/gateway" in out
    assert out.startswith("postgresql+asyncpg://")


def test_redaction_is_noop_without_credentials():
    redact = _load_redactor()
    url = "postgresql+asyncpg://localhost:5432/gateway"
    assert redact(url) == url


def test_no_print_exposes_db_url_unredacted():
    """모듈 레벨 print 가 db_url 을 찍을 때는 반드시 리댁션을 거쳐야 한다."""
    source = ENV_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "print":
            continue
        segment = ast.get_source_segment(source, node) or ""
        # bool(db_master) / bool(db_app) 같은 존재 여부 출력은 비밀이 아니다.
        if "db_url" in segment and "_redact_credentials" not in segment:
            offenders.append(segment)

    assert not offenders, (
        "db_url 을 리댁션 없이 출력하는 print 가 있다 — migration Job stdout 은 "
        f"CloudWatch 에 남는다: {offenders}"
    )
