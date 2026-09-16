# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: 서비스 토큰 합성 유저의 id 계약.

`admin-api/src/app/core/auth.py` 는 `Bearer svc-...` 요청마다 **하드코딩된 UUID** 로
CurrentUser 를 합성한다. `auth.service_tokens.created_by` 가 그 id 를 FK 로 참조하므로,
`db/init/03_seed_data.sql` 의 시드 행과 코드의 상수가 어긋나면 서비스 토큰 발급이
FK 위반으로 실패한다 — 그런데 그 실패는 **런타임에만** 드러난다.

두 곳에 흩어진 상수를 테스트로 묶어, 한쪽만 바꾸면 즉시 실패하게 한다.

⚠️ 이 시드를 백필하는 alembic 마이그레이션은 만들지 않는다.
`db/run_migration.sh` 가 매 배포마다 init SQL 전체를 재적용하고 시드가
`ON CONFLICT (id) DO NOTHING` 이라 신규·기존 DB 모두에서 행이 보장된다.
참조 트리에는 그 마이그레이션이 있지만 여기서는 중복이고, ON CONFLICT DO NOTHING 이라
이미 존재하는 행의 컬럼 드리프트를 고치지도 못한다(고친 것처럼 보이기만 한다).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

SEED = Path(__file__).resolve().parents[2].parent / "db" / "init" / "03_seed_data.sql"
AUTH = Path(__file__).resolve().parents[2] / "src" / "app" / "core" / "auth.py"


def _seed_service_token_uuid() -> str:
    """시드 SQL 에서 서비스 토큰 합성 유저의 id 를 뽑는다."""
    sql = SEED.read_text(encoding="utf-8")
    marker = "Service-Token Synthetic User"
    assert marker in sql, f"{SEED.name} 에 서비스 토큰 시드 블록이 없다"
    block = sql[sql.index(marker) :]
    m = re.search(r"INSERT INTO auth\.users[^;]*?'([0-9a-f-]{36})'", block, re.S)
    assert m, f"시드 블록에서 UUID 를 찾지 못했다:\n{block[:400]}"
    return m.group(1)


def test_the_extraction_actually_finds_both_constants():
    """공허성 대조군 — 한쪽이라도 못 찾으면 아래 단정이 무의미하다."""
    seeded = _seed_service_token_uuid()
    uuid.UUID(seeded)  # 형식 확인
    code = AUTH.read_text(encoding="utf-8")
    assert "is_service_token=True" in code, "auth.py 에 서비스 토큰 합성 경로가 없다"


def test_code_and_seed_agree_on_the_synthetic_user_id():
    """⚠️ 두 상수가 일치해야 한다 — 어긋나면 서비스 토큰 발급이 FK 위반으로 죽는다."""
    seeded = _seed_service_token_uuid()
    code = AUTH.read_text(encoding="utf-8")
    assert seeded in code, (
        f"시드의 합성 유저 id {seeded} 를 core/auth.py 가 쓰지 않는다 — "
        f"service_tokens.created_by FK 대상이 어긋나 토큰 발급이 런타임에 실패한다"
    )


def test_no_migration_backfills_the_seed():
    """이 시드를 백필하는 마이그레이션이 생기지 않았는지 — 중복이고 드리프트도 못 고친다."""
    versions = SEED.parent.parent / "versions"
    assert versions.is_dir(), f"db/versions 미발견: {versions}"
    seeded = _seed_service_token_uuid()
    offenders = [
        p.name for p in sorted(versions.glob("*.py"))
        if seeded in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "합성 유저를 삽입하는 마이그레이션이 있다 — db/run_migration.sh 가 매 배포마다 "
        "init SQL 을 재적용하므로 중복이다. 게다가 ON CONFLICT DO NOTHING 이라 이미 "
        "존재하는 행의 컬럼 드리프트를 고치지도 못한다:\n  " + "\n  ".join(offenders)
    )


def _strip_sql_comments(sql: str) -> str:
    """`--` 주석을 제거한다.

    ⚠️ 이게 없으면 검사가 **자기충족**이 된다. 같은 파일의 주석이 이 계약을 설명하며
    `ON CONFLICT` 라는 문자열을 담고 있어서, 실제 SQL 에서 그 절을 지워도 주석 때문에
    검사가 통과했다(음성대조군에서 드러났다). 테스트는 문서가 아니라 **코드**를 봐야 한다.
    """
    return "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )


def test_seed_is_idempotent():
    """기존 DB 에서도 안전해야 한다 — 매 배포마다 재적용되기 때문.

    ⚠️ `;` 앞까지만 잘라 검사하는 것도 안 된다. `ON CONFLICT` 절을 지우면 그 앞
    `VALUES (...)` 끝에서 이미 `;` 를 만나므로, 잘라낸 조각에 그 절이 없는 게 당연해져
    검사가 통과해 버린다. UUID 를 담은 **INSERT 문 전체**를, **주석을 제거한 뒤** 본다.
    """
    sql = _strip_sql_comments(SEED.read_text(encoding="utf-8"))
    seeded = _seed_service_token_uuid()
    stmts = [
        st for st in sql.split(";")
        if seeded in st and "INSERT INTO auth.users" in st
    ]
    assert len(stmts) == 1, (
        f"합성 유저를 삽입하는 INSERT 문이 {len(stmts)}개다 — 1개여야 한다"
    )
    assert "ON CONFLICT" in stmts[0], (
        f"시드가 멱등이 아니다 — run_migration.sh 가 매 배포마다 재적용하므로 두 번째 "
        f"배포가 중복키로 죽는다:\n{stmts[0].strip()[:300]}"
    )


def test_the_comment_stripper_is_not_a_no_op():
    """대조군 — 주석 제거가 실제로 동작해야 위 테스트가 의미를 갖는다."""
    stripped = _strip_sql_comments("SELECT 1; -- ON CONFLICT in a comment\nSELECT 2;")
    assert "ON CONFLICT" not in stripped, stripped
    assert "SELECT 1" in stripped and "SELECT 2" in stripped, stripped
