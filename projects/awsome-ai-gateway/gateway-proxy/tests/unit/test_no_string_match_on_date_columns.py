# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""LIKE/ILIKE 를 DATE/TIMESTAMP 컬럼에 쓰는 드리프트 방지 가드.

배경 — `/v1/usage/team/{id}` 가 실제로 이 버그로 항상 0 을 돌려주고 있었다::

    .where(DailyAggregate.date.like(f"{period}%"))   # date 는 DATE 컬럼

asyncpg 방언은 `date LIKE $2::VARCHAR` 를 내보내고 PostgreSQL 이 거부한다
(실 PG16 확인)::

    ERROR:  operator does not exist: date ~~ unknown

호출부가 `except Exception` 으로 삼키고 있었기 때문에 500 도 나지 않고 조용히
total_tokens=0 / total_cost=0 이 되어, 단위테스트·헬스체크·에러율 어디에도 안 잡혔다.
(같은 클래스의 침묵 실패: notification-worker 의 `User.role == "ADMIN"` +
`String(20)` → `$1::VARCHAR` 드리프트.)

문자열 grep 가드는 주석·문서·문자열 리터럴까지 걸려 잡음이 크고, 반대로
줄바꿈이 끼면 놓친다. 그래서 AST 로 `<something>.<col>.like(...)` 호출을 찾고,
`<col>` 이 ORM 모델에서 Date/DateTime 으로 선언된 컬럼명일 때만 실패시킨다.
반열구간(`>= 월초`, `< 다음달초`)으로 쓰면 인덱스도 그대로 탄다.
"""
from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import Date, DateTime
from sqlalchemy import inspect as sa_inspect

SRC = Path(__file__).resolve().parents[2] / "src"


def _temporal_column_names() -> set[str]:
    """모든 ORM 모델에서 Date/DateTime 으로 선언된 컬럼명 집합.

    모델 모듈을 실제로 import 해서 매퍼에서 읽는다 — 선언 텍스트를 파싱하는 게
    아니라 SQLAlchemy 가 최종적으로 갖는 타입을 보므로, TypeDecorator 나
    `Mapped[...]` 추론으로 타입이 바뀌어도 따라간다.
    """
    import importlib

    from app.db import Base

    for module in sorted(p.stem for p in (SRC / "app" / "models").glob("*.py")):
        if module != "__init__":
            importlib.import_module(f"app.models.{module}")

    names: set[str] = set()
    for mapper in Base.registry.mappers:
        for column in sa_inspect(mapper.class_).columns:
            if isinstance(column.type, (Date, DateTime)):
                names.add(column.key)
    return names


def _string_match_calls_on(names: set[str]) -> list[str]:
    """`*.<name>.like(...)` / `.ilike(...)` 형태의 호출을 전부 수집."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 찾는 모양: <expr>.<column>.like(...)  → func.attr in {like, ilike}
            #                                        func.value.attr == 컬럼명
            if not (isinstance(func, ast.Attribute) and func.attr in {"like", "ilike"}):
                continue
            receiver = func.value
            if isinstance(receiver, ast.Attribute) and receiver.attr in names:
                offenders.append(
                    f"{path.relative_to(SRC.parent)}:{node.lineno} "
                    f"— .{receiver.attr}.{func.attr}(...)"
                )
    return offenders


def test_no_like_on_date_or_timestamp_columns() -> None:
    names = _temporal_column_names()
    # 가드 자체가 무의미해지지 않도록: 잡을 대상이 실제로 존재하는지 먼저 확인.
    # (모델 import 가 조용히 실패해 names 가 빈 집합이면 이 테스트는 항상 통과한다.)
    assert "date" in names, f"DailyAggregate.date 를 못 찾았다 — 가드가 공허하다: {sorted(names)}"

    offenders = _string_match_calls_on(names)
    assert not offenders, (
        "DATE/TIMESTAMP 컬럼에 LIKE/ILIKE 를 쓰면 PostgreSQL 이 "
        "'operator does not exist: date ~~ unknown' 으로 거부한다. "
        "반열구간 비교(>= 시작, < 끝)로 바꿔라:\n  " + "\n  ".join(offenders)
    )


def test_guard_detects_a_known_bad_pattern() -> None:
    """가드의 음성 대조군 — 나쁜 패턴을 넣으면 정말 잡는지."""
    names = _temporal_column_names()
    tree = ast.parse("stmt.where(DailyAggregate.date.like('2026-09%'))")
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"like", "ilike"}
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr in names
    ]
    assert len(found) == 1, "가드가 알려진 나쁜 패턴을 못 잡는다"
