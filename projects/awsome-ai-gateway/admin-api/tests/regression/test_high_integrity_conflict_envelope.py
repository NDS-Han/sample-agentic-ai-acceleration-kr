# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""마이그레이션 0034 의 유니크 인덱스가 런타임에서 무엇으로 보이는지 고정한다.

배경 — 0034 는 다섯 개의 (부분) 유니크 인덱스를 넣는다:

  * ``idx_model_pricings_active_unique``   — alias 당 열린 단가 1개
  * ``idx_virtual_keys_user_active_unique``— 사용자당 ACTIVE VK 1개
  * ``idx_model_aliases_lower_unique``     — ``lower(alias)`` 유일
  * ``idx_productivity_events_idempotency``— idempotency_key 유일
  * ``idx_users_team_id``                  — 성능용(유니크 아님)

앞의 넷은 **동시 요청에서 정상적으로 발동한다**(경합). 실측으로 admin-api 전체에
``IntegrityError`` 처리가 **하나도 없었고**, 그래서 최후의 그물이 잡아 500 +
"내부 오류" 로 나갔다. 인덱스를 넣으면서 이 매핑을 같이 넣지 않으면, 조용한
데이터 오염을 **원인을 알 수 없는 500** 으로 바꾸는 거래가 된다.

⚠️ 이 파일이 고정하는 두 번째 성질이 더 중요하다: **무조건 409 가 아니어야 한다.**
   ``IntegrityError`` 는 FK 위반·NOT NULL 위반 같은 코드 버그로도 발생하고, 그걸
   409 로 감추면 500 으로 드러나야 할 결함이 "정상적인 경합" 처럼 보이며
   ``logger.exception`` 도 남지 않는다. 알려진 제약만 409, 나머지는 재-raise.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.main import register_exception_handlers

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "versions" / "0034_add_missing_indexes_and_uniqueness.py"
MAIN_PY = REPO_ROOT / "admin-api" / "src" / "app" / "main.py"


class _FakeUniqueViolation(Exception):
    """asyncpg.exceptions.UniqueViolationError 의 형상만 흉내낸다.

    실제 드라이버 예외를 import 하지 않는 이유: 핸들러가 보는 건 ``orig`` 의
    ``constraint_name`` 속성과 문자열뿐이고, 이 테스트는 그 계약을 고정하는 것이다.
    """

    def __init__(self, message: str, constraint_name: str | None = None) -> None:
        super().__init__(message)
        if constraint_name is not None:
            self.constraint_name = constraint_name


def _app_raising(exc: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():  # pragma: no cover - 라우트 본문은 즉시 raise
        raise exc

    # raise_server_exceptions=False: 최후의 그물이 만든 500 응답을 보고 싶다.
    # True 면 TestClient 가 예외를 되던져서 응답을 검사할 수 없다.
    return TestClient(app, raise_server_exceptions=False)


def _integrity(message: str, constraint_name: str | None = None) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _FakeUniqueViolation(message, constraint_name))


# ─────────────────────────────────────────────────────────────────────────────
# 1. 알려진 유니크 제약 → 409
# ─────────────────────────────────────────────────────────────────────────────

CONFLICT_CONSTRAINTS = [
    "idx_model_pricings_active_unique",
    "idx_virtual_keys_user_active_unique",
    "idx_model_aliases_lower_unique",
    "idx_productivity_events_idempotency",
]


@pytest.mark.parametrize("constraint", CONFLICT_CONSTRAINTS)
def test_known_unique_constraint_maps_to_409(constraint):
    client = _app_raising(
        _integrity(f'duplicate key value violates unique constraint "{constraint}"', constraint)
    )
    r = client.get("/boom")
    assert r.status_code == 409, f"{constraint} 가 409 가 아니다: {r.status_code} {r.text}"
    body = r.json()
    assert body["error"]["type"] == "conflict"
    assert body["error"]["code"] == "CONCURRENT_MODIFICATION"


@pytest.mark.parametrize("constraint", CONFLICT_CONSTRAINTS)
def test_constraint_name_recovered_from_message_when_driver_omits_it(constraint):
    """psycopg 등 constraint_name 을 안 주는 드라이버 폴백.

    이 폴백이 없으면 드라이버를 바꾸는 순간 조용히 500 으로 되돌아간다.
    """
    client = _app_raising(
        _integrity(f'duplicate key value violates unique constraint "{constraint}"')
    )
    assert client.get("/boom").status_code == 409


def test_409_body_does_not_leak_constraint_or_sql():
    """제약 이름·SQL·테이블명은 내부 구조다 — 본문에 나가면 안 된다."""
    client = _app_raising(
        _integrity(
            'duplicate key value violates unique constraint "idx_virtual_keys_user_active_unique"\n'
            "DETAIL:  Key (user_id)=(11111111-1111-1111-1111-111111111111) already exists.",
            "idx_virtual_keys_user_active_unique",
        )
    )
    text = client.get("/boom").text
    assert "idx_virtual_keys" not in text
    assert "INSERT" not in text
    assert "user_id" not in text
    assert "11111111" not in text


# ─────────────────────────────────────────────────────────────────────────────
# 2. 알 수 없는 무결성 위반 → 감추지 않는다(500)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "constraint"),
    [
        # FK 위반 — 코드가 없는 부모를 참조한 버그
        (
            'insert or update on table "virtual_keys" violates foreign key constraint '
            '"virtual_keys_user_id_fkey"',
            "virtual_keys_user_id_fkey",
        ),
        # NOT NULL 위반 — 필수 필드를 안 채운 버그
        ('null value in column "created_by" violates not-null constraint', None),
        # 우리가 넣지 않은 다른 유니크 제약 — 의미를 모르므로 409 로 단정하면 안 된다
        (
            'duplicate key value violates unique constraint "users_email_key"',
            "users_email_key",
        ),
        # CHECK 위반
        ('new row violates check constraint "budget_configs_amount_positive"', None),
    ],
)
def test_unknown_integrity_error_is_not_disguised_as_conflict(message, constraint):
    client = _app_raising(_integrity(message, constraint))
    r = client.get("/boom")
    assert r.status_code == 500, (
        f"알 수 없는 무결성 위반이 {r.status_code} 로 나갔다 — 버그를 경합으로 감추면 안 된다"
    )
    assert r.json()["error"]["type"] != "conflict"


def test_handler_is_not_vacuous_control():
    """대조군 — 핸들러가 없으면 알려진 제약도 500 이었다.

    이 테스트가 없으면 위의 409 단정들이 "원래도 409 였다" 인지 구분할 수 없다.
    """
    app = FastAPI()  # register_exception_handlers 를 부르지 않는다

    @app.get("/boom")
    async def boom():  # pragma: no cover
        raise _integrity(
            'duplicate key value violates unique constraint "idx_model_pricings_active_unique"',
            "idx_model_pricings_active_unique",
        )

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500


# ─────────────────────────────────────────────────────────────────────────────
# 3. 핸들러의 제약 목록이 마이그레이션과 어긋나지 않는지
# ─────────────────────────────────────────────────────────────────────────────


def _migration_source() -> str:
    assert MIGRATION.is_file(), f"마이그레이션이 없다: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


def _handler_constraints() -> set[str]:
    """main.py 의 _CONFLICT_CONSTRAINTS 리터럴을 AST 로 읽는다.

    문자열 grep 이 아니라 AST 인 이유: 주석에 제약 이름을 적어두면 grep 은
    통과하지만 실제 frozenset 은 비어 있을 수 있다(자기 문서를 증거로 읽는 함정).
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_CONFLICT_CONSTRAINTS" not in targets:
            continue
        # frozenset({...}) 형태
        call = node.value
        assert isinstance(call, ast.Call), "_CONFLICT_CONSTRAINTS 가 frozenset(...) 이 아니다"
        assert call.args, "frozenset() 이 비어 있다"
        literal = ast.literal_eval(call.args[0])
        return set(literal)
    raise AssertionError("main.py 에서 _CONFLICT_CONSTRAINTS 를 찾지 못했다")


def test_every_handler_constraint_is_created_by_the_migration():
    """핸들러가 아는 제약이 실제로 생성되는가 — 오타면 영원히 500 이다."""
    src = _migration_source()
    for name in sorted(_handler_constraints()):
        assert name in src, (
            f"핸들러는 {name} 를 409 로 매핑하는데 0034 는 그 이름을 만들지 않는다 "
            "(오타면 그 경로는 계속 500 이다)"
        )


def test_every_unique_index_in_the_migration_is_mapped_to_409():
    """반대 방향 — 0034 가 만든 유니크 인덱스에 매핑이 빠지면 그 경로만 500 이 된다.

    성능용 비-유니크 인덱스(idx_users_team_id)는 위반을 낼 수 없으므로 제외한다.
    """
    src = _migration_source()
    created_unique = set()
    for line in src.splitlines():
        stripped = line.strip()
        # 주석 줄은 증거가 아니다.
        if stripped.startswith("#"):
            continue
        if "CREATE UNIQUE INDEX" not in stripped.upper():
            continue
        # `... INDEX IF NOT EXISTS name ` 에서 이름만 뽑는다.
        tokens = stripped.replace('"', " ").replace("'", " ").split()
        upper = [t.upper() for t in tokens]
        try:
            i = upper.index("INDEX")
        except ValueError:  # pragma: no cover
            continue
        rest = tokens[i + 1 :]
        upper_rest = [t.upper() for t in rest]
        if upper_rest[:3] == ["IF", "NOT", "EXISTS"]:
            rest = rest[3:]
        if rest:
            created_unique.add(rest[0].split("(")[0])

    assert created_unique, (
        "0034 에서 CREATE UNIQUE INDEX 를 하나도 못 찾았다 — 파싱이 깨졌거나 "
        "마이그레이션이 바뀌었다(이 테스트가 공허해진다)"
    )

    mapped = _handler_constraints()
    missing = created_unique - mapped
    assert not missing, (
        f"0034 가 만든 유니크 인덱스 {sorted(missing)} 가 409 매핑에 없다 — "
        "그 경합은 원인 불명 500 으로 나간다"
    )
