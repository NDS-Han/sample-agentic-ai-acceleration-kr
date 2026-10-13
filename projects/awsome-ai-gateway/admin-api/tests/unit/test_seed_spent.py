# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""seed_spent 의 트랜잭션 안전성 회귀.

수정 전 결함 두 가지:

1. **savepoint 부재** — 한 item 의 DB 실패가 외부 트랜잭션을 aborted 상태로
   만들어 이후 모든 item 과 최종 commit 이 연쇄 실패했다.
2. **Redis SET 이 commit 전** — 롤백되면 DB 에 없는 지출액이 게이트웨이에
   TTL 동안 광고됐다(§6-6 위반).
3. **절대값 덮어쓰기 경합** — FOR UPDATE 없이 읽은 값을 Redis SET 으로
   덮어쓰면 카운터에 섞인 in-flight 예약치가 지워져 settle 이 (실비 − 예약치)
   적용 시 예약치만큼 음수로 샜다. 델타(INCRBYFLOAT + 행잠금 델타 UPDATE)로 교체.

수정 후 계약: item 별 begin_nested savepoint, 실패 item 은 error 기록 후 계속,
Redis 쓰기는 ``defer_redis_write_until_commit`` 으로 outer commit 까지 지연,
DB·Redis 모두 델타 적용으로 동시 settle/예약 보존.
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
    """한 item 의 DB 실패가 savepoint 에 격리돼 나머지 item 은 정상 처리된다.

    ⚠️ items 는 데드락 감소를 위해 (scope, scope_id, ...) 로 정렬돼 처리된다 —
       get_user 실패가 어느 item 에 걸리는지는 정렬 순서에 따른다.
    """
    uid1, uid2 = sorted([uuid.uuid4(), uuid.uuid4()])
    session = _nested_session()
    # 정렬상 첫 item: get_user 가 예외 → 둘째: 정상
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
    assert resp.results[0].status == "error"   # 정렬상 첫 item
    assert resp.results[0].scope_id == str(uid1)
    assert resp.results[1].status == "ok"
    # 두 item 모두 begin_nested savepoint 안에서 실행됐다
    assert session.begin_nested.call_count == 2


@pytest.mark.asyncio
async def test_failed_item_does_not_schedule_redis_write():
    """실패한 item 의 Redis 쓰기는 commit 후에도 실행되지 않는다."""
    uid1, uid2 = sorted([uuid.uuid4(), uuid.uuid4()])
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

    # AsyncMock 세션은 defer 가 즉시 실행 — 성공한 item(정렬상 둘째)의 키만 쓰인다
    keys = [c.args[2] for c in redis.eval.call_args_list]
    assert len(keys) == 1
    assert str(uid2) in keys[0]
    assert str(uid1) not in keys[0]


@pytest.mark.asyncio
async def test_per_app_seed_does_not_touch_total_key():
    """client 지정 item 은 앱 키만 쓰고 총합(client=None) 키는 건드리지 않는다.

    upsert 는 client 행만 바꾸고 총합 행은 그대로다 — 앱 델타를 총합 카운터에
    더하면 "총합 시드 + 앱 시드" 일괄 마이그레이션에서 이중계상된다.
    """
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
    keys = [c.args[2] for c in redis.eval.call_args_list]
    assert len(keys) == 1  # 앱 키만 — 총합 키는 건드리지 않는다
    assert "claude-code" in keys[0]


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


@pytest.mark.asyncio
async def test_seed_applies_delta_not_overwrite():
    """기존 100 사용 중 150 시드 → DB/Redis 모두 +50 델타로 적용한다.

    절대값 덮어쓰기는 행을 읽은 뒤 쓰기까지의 worker 증분과 Redis 카운터의
    in-flight 예약치를 유실시킨다 — 델타 시맨틱이어야 둘 다 보존된다.
    """
    uid = uuid.uuid4()
    session = _nested_session()
    # FOR UPDATE SELECT 가 기존 사용량 100 을 돌려준다
    row = MagicMock()
    row.scalar_one_or_none = MagicMock(return_value=Decimal("100"))
    session.execute = AsyncMock(return_value=row)
    repo = MagicMock()
    repo.get_user = AsyncMock(return_value=MagicMock())

    redis = AsyncMock()
    svc = _svc(redis)

    with patch("app.services.budget_service.UserRepository", return_value=repo), \
         patch("app.services.budget_service.audit_logger") as audit:
        audit.log = AsyncMock()
        resp = await svc.seed_spent(
            session, items=[_item(uid, spent="150")], actor=_actor()
        )

    assert resp.succeeded == 1
    # upsert 에 delta=50 이 전달됐다
    upsert_calls = [
        c for c in session.execute.call_args_list
        if isinstance(c.args[1], dict) and "delta" in c.args[1]
    ]
    assert len(upsert_calls) == 1
    assert upsert_calls[0].args[1]["delta"] == "50"
    # Redis 도 절대값 SET 이 아니라 조건부 델타 INCRBYFLOAT Lua
    redis.set.assert_not_called()
    vals = [c.args for c in redis.eval.call_args_list]
    assert len(vals) == 1 and vals[0][3] == "50"
    assert "EXISTS" in vals[0][0] and "INCRBYFLOAT" in vals[0][0]
