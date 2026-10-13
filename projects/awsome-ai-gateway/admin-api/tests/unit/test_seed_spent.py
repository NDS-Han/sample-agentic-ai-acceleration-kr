# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""seed_spent 의 트랜잭션 안전성 회귀.

수정 전 결함 두 가지:

1. **savepoint 부재** — 한 item 의 DB 실패가 외부 트랜잭션을 aborted 상태로
   만들어 이후 모든 item 과 최종 commit 이 연쇄 실패했다.
2. **Redis SET 이 commit 전** — 롤백되면 DB 에 없는 지출액이 게이트웨이에
   TTL 동안 광고됐다(§6-6 위반).

수정 후 계약: item 별 begin_nested savepoint, 실패 item 은 error 기록 후 계속,
Redis 쓰기는 ``defer_redis_write_until_commit`` 으로 outer commit 까지 지연.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.budgets import SeedSpentItem
from app.services.budget_service import BudgetService


def _nested_session():
    """begin_nested 가 실제 async CM, execute 는 MagicMock 세션."""
    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    session.begin_nested = MagicMock(return_value=cm)
    row = MagicMock()
    row.scalar_one_or_none = MagicMock(return_value=None)
    row.scalar = MagicMock(return_value=0)
    session.execute = AsyncMock(return_value=row)
    return session


def _svc(redis=None):
    cache = MagicMock()
    cache._redis = redis or AsyncMock()
    return BudgetService(cache)


def _actor():
    return MagicMock(user_id=uuid.uuid4(), role=MagicMock(value="ADMIN"))


def _item(uid: uuid.UUID, spent: str = "1.5") -> SeedSpentItem:
    return SeedSpentItem(
        scope="USER", scope_id=str(uid), period="2026-01", spent_usd=Decimal(spent)
    )


@pytest.mark.asyncio
async def test_failed_item_does_not_poison_following_items():
    """item#1 의 DB 실패가 savepoint 에 격리돼 item#2 는 정상 처리된다."""
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    session = _nested_session()
    # item#1: get_user 가 예외 → item#2: 정상
    repo = MagicMock()
    repo.get_user = AsyncMock(side_effect=[Exception("db down"), MagicMock()])

    redis = AsyncMock()
    svc = _svc(redis)

    with patch("app.services.budget_service.UserRepository", return_value=repo), \
         patch("app.services.budget_service.audit_logger") as audit:
        audit.log = AsyncMock()
        resp = await svc.seed_spent(
            session, items=[_item(uid1), _item(uid2)], actor=_actor()
        )

    assert resp.succeeded == 1 and resp.failed == 1
    assert resp.results[0].status == "error"
    assert resp.results[1].status == "ok"
    # 두 item 모두 begin_nested savepoint 안에서 실행됐다
    assert session.begin_nested.call_count == 2


@pytest.mark.asyncio
async def test_failed_item_does_not_schedule_redis_write():
    """실패한 item 의 Redis SET 은 commit 후에도 실행되지 않는다."""
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    session = _nested_session()
    repo = MagicMock()
    repo.get_user = AsyncMock(side_effect=[Exception("db down"), MagicMock()])

    redis = AsyncMock()
    svc = _svc(redis)

    with patch("app.services.budget_service.UserRepository", return_value=repo), \
         patch("app.services.budget_service.audit_logger") as audit:
        audit.log = AsyncMock()
        await svc.seed_spent(
            session, items=[_item(uid1), _item(uid2)], actor=_actor()
        )

    # AsyncMock 세션은 defer 가 즉시 실행 — 성공한 item#2 의 키만 쓰인다
    set_keys = [c.args[0] for c in redis.set.call_args_list]
    assert len(set_keys) == 1
    assert str(uid2) in set_keys[0]
    assert str(uid1) not in set_keys[0]


@pytest.mark.asyncio
async def test_per_app_seed_also_defers_total_key():
    """client 지정 item 은 앱 키 + 총합 키 두 개를 commit 후에 쓴다."""
    uid = uuid.uuid4()
    session = _nested_session()
    repo = MagicMock()
    repo.get_user = AsyncMock(return_value=MagicMock())

    redis = AsyncMock()
    svc = _svc(redis)

    with patch("app.services.budget_service.UserRepository", return_value=repo), \
         patch("app.services.budget_service.audit_logger") as audit:
        audit.log = AsyncMock()
        item = SeedSpentItem(
            scope="USER", scope_id=str(uid), client="claude-code",
            period="2026-01", spent_usd=Decimal("2.5"),
        )
        resp = await svc.seed_spent(session, items=[item], actor=_actor())

    assert resp.succeeded == 1
    set_keys = [c.args[0] for c in redis.set.call_args_list]
    assert len(set_keys) == 2  # 앱 키 + 총합(client=None) 키


@pytest.mark.asyncio
async def test_defer_registered_once_for_all_items():
    """Redis 쓰기는 건수와 무관하게 단일 deferred 콜백으로 묶인다."""
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    session = _nested_session()
    repo = MagicMock()
    repo.get_user = AsyncMock(return_value=MagicMock())

    svc = _svc()

    with patch("app.services.budget_service.UserRepository", return_value=repo), \
         patch("app.services.budget_service.audit_logger") as audit, \
         patch(
             "app.services.budget_service.defer_redis_write_until_commit",
             new=AsyncMock(),
         ) as defer:
        audit.log = AsyncMock()
        await svc.seed_spent(
            session, items=[_item(uid1), _item(uid2)], actor=_actor()
        )

    defer.assert_awaited_once()
