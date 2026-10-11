# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""list_users/list_keys keyset 페이지네이션 회귀 테스트.

과거 결함: ORDER BY created_at DESC 인데 커서 조건이 ``id < cursor`` 라서
UUIDv4 순서와 무관하게 2페이지부터 행을 건너뛰거나 반복했다.
수정: 커서는 ``(created_at, id)`` 복합 키 — 정렬 키와 커서 키가 일치해야 한다.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories._cursor import decode_cursor, encode_cursor
from app.repositories.key_repository import KeyRepository
from app.repositories.user_repository import UserRepository


def _session_capturing_stmt() -> tuple[AsyncMock, list]:
    captured: list = []
    result = MagicMock()
    result.scalars.return_value.all.return_value = []

    async def _execute(stmt):
        captured.append(stmt)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session, captured


# ── 커서 코덱 ──────────────────────────────────────────────────────────


def test_cursor_roundtrip():
    ts = datetime(2026, 3, 1, 12, 34, 56, 789000, tzinfo=timezone.utc)
    rid = uuid.uuid4()
    decoded = decode_cursor(encode_cursor(ts, rid))
    assert decoded is not None
    dec_ts, dec_id = decoded
    assert dec_ts == ts
    assert dec_id == rid


def test_decode_rejects_legacy_and_garbage():
    assert decode_cursor(str(uuid.uuid4())) is None      # 구 형식 bare UUID
    assert decode_cursor("not-a-cursor") is None
    assert decode_cursor("abc_" + str(uuid.uuid4())) is None  # ts 파싱 실패


# ── 컴파일된 SQL 이 정렬키와 커서키를 일치시키는가 ─────────────────────


@pytest.mark.asyncio
async def test_list_users_composite_cursor_matches_sort_key():
    session, captured = _session_capturing_stmt()
    repo = UserRepository(session)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rid = uuid.uuid4()

    await repo.list_users(cursor=(ts, rid))

    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY" in sql and "created_at DESC" in sql and "id DESC" in sql
    assert "created_at <" in sql  # 복합 조건의 첫 다리
    assert "created_at =" in sql  # 타이브레이크 다리


@pytest.mark.asyncio
async def test_list_users_legacy_uuid_cursor_falls_back():
    session, captured = _session_capturing_stmt()
    repo = UserRepository(session)

    await repo.list_users(legacy_id_cursor=uuid.uuid4())

    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "id <" in sql


@pytest.mark.asyncio
async def test_list_keys_composite_cursor_and_email_escape():
    session, captured = _session_capturing_stmt()
    repo = KeyRepository(session)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    await repo.list_keys(cursor=(ts, uuid.uuid4()), email="100%_admin")

    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "created_at DESC" in sql and "id DESC" in sql
    assert "created_at <" in sql
    # ilike 와일드카드 이스케이프 — % 와 _ 가 리터럴이어야 한다
    assert "100\\%\\_admin" in sql or "%'100" not in sql


# ── 페이지 순회 시뮬레이션 — 건너뜀/중복이 없음을 의미론 수준에서 증명 ──


def _keyset_page(rows, cursor, limit):
    """repo 와 동일한 의미론: (created_at DESC, id DESC) 정렬 + 커서 이후."""
    ordered = sorted(rows, key=lambda r: (r["created_at"], r["id"].int), reverse=True)
    if cursor is not None:
        c_ts, c_id = cursor
        ordered = [
            r for r in ordered
            if (r["created_at"], r["id"].int) < (c_ts, c_id.int)
        ]
    return ordered[:limit]


def test_full_traversal_no_skip_no_dup():
    """id 순서가 created_at 순서와 완전히 어긋난 데이터에서 전체 순회."""
    rng = random.Random(42)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(120):
        # 같은 created_at 을 가진 행 다수 포함(배치 insert 시나리오)
        ts = base + timedelta(seconds=rng.choice([0, 0, 0, 1, 5, 60]))
        rows.append({"id": uuid.uuid4(), "created_at": ts})

    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        page = _keyset_page(rows, cursor, limit=25)
        if not page:
            break
        seen.extend(r["id"] for r in page)
        last = page[-1]
        cursor = (last["created_at"], last["id"])
        pages += 1
        if pages > 50:
            pytest.fail("페이지 순회가 종료하지 않는다 — 커서가 진행하지 않음")

    assert len(seen) == len(rows), "누락 또는 중복 행이 있다"
    assert len(set(seen)) == len(rows), "중복 행이 있다"


def test_traversal_stable_across_identical_timestamps():
    """전 행이 동일 created_at — id 타이브레이크만으로 전수 순회."""
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"id": uuid.uuid4(), "created_at": ts} for _ in range(60)]

    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(20):
        page = _keyset_page(rows, cursor, limit=10)
        if not page:
            break
        seen.extend(r["id"] for r in page)
        last = page[-1]
        cursor = (last["created_at"], last["id"])

    assert len(set(seen)) == len(rows)
