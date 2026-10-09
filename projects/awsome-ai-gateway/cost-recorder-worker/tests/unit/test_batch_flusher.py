# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""BatchFlusher 단위 테스트.

배치 flush 로직의 핵심 경로 검증:
- 빈 entries → no-op
- user+team 스코프별 period 집계 합산
- threshold_triggered 있는 entry → notifications:budget publish
- daily counter pipeline 호출
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.batch_flusher import BatchFlusher
from worker.schemas.cost_stream import CostStreamEntry


def _make_entry(
    request_id: str = "req-1",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    team_id: str = "00000000-0000-0000-0000-000000000002",
    cost: str = "0.01",
    threshold: int | None = None,
    threshold_scope: str | None = None,
) -> CostStreamEntry:
    return CostStreamEntry(
        request_id=request_id,
        user_id=user_id,
        team_id=team_id,
        dept_id="00000000-0000-0000-0000-000000000003",
        model_alias="claude-sonnet-4-6",
        provider="BEDROCK",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=200,
        is_streaming=False,
        estimated_usage=False,
        requested_at="2026-04-21T10:00:00+00:00",
        completed_at="2026-04-21T10:00:01+00:00",
        period="2026-04",
        date="2026-04-21",
        threshold_triggered=threshold,
        threshold_scope=threshold_scope,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_batch_is_noop():
    session_factory = MagicMock()
    redis = MagicMock()
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([])
    session_factory.assert_not_called()
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flush_inserts_usage_and_upserts_budgets():
    """entries 3개, 같은 user/team/period 이면 budget_usages UPSERT 는 집계 1건씩."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id=f"req-{i}", cost="0.10") for i in range(3)
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # 4 session.execute 호출: 재처리 필터 SELECT + INSERT usage_logs
    # + UPSERT user budget + UPSERT team budget
    #
    # ⚠️ 첫 호출이 재처리 필터여야 한다. budget_usages 는 **가산** UPSERT 라, 재처리된
    #    entry 를 걸러내지 않으면 사용자의 기록 사용액이 영구히 두 배가 된다
    #    (batch_flusher._filter_replays 주석 참조). 개수만 세면 그 SELECT 가 뒤로 밀려
    #    무의미해진 것을 잡지 못하므로 순서를 함께 못 박는다.
    assert session.execute.await_count == 4
    assert session.commit.await_count == 1
    replay_sql = str(session.execute.await_args_list[0].args[0]).lower()
    assert "from usage.usage_logs" in replay_sql and "request_id" in replay_sql, (
        f"첫 호출이 재처리 필터 SELECT 가 아니다: {replay_sql[:120]}"
    )

    # 3번째 호출 = user UPSERT — 합산된 cost (0.30)
    user_call = session.execute.await_args_list[2]
    user_params = user_call.args[1]
    assert len(user_params) == 1  # 단일 (user, period) 그룹
    assert user_params[0]["scope"] == "USER"
    assert user_params[0]["cost"] == "0.30"

    # 4번째 호출 = team UPSERT
    team_call = session.execute.await_args_list[3]
    team_params = team_call.args[1]
    assert team_params[0]["scope"] == "TEAM"
    assert team_params[0]["cost"] == "0.30"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_triggered_publishes_notification():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id="req-normal"),  # no threshold
        _make_entry(request_id="req-80pct", threshold=80),
        _make_entry(request_id="req-100pct", threshold=100),
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # publish는 threshold != None 인 2건만.
    assert redis.publish.await_count == 2
    channels = [call.args[0] for call in redis.publish.await_args_list]
    assert all(c == "notifications:budget" for c in channels)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_daily_counter_pipeline_uses_hash_tag():
    """Redis Cluster hash tag {<user_id>} 가 daily counter 키에 포함."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(user_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    # incrbyfloat 첫 호출의 key 확인 — hash tag 포함.
    keys_seen = [call.args[0] for call in pipe.incrbyfloat.call_args_list]
    assert any("{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}" in k for k in keys_seen)


def _mock_session_with_cumulative(
    cumulative: object, scope_captures: list | None = None
) -> MagicMock:
    """flush 호출 순서에 맞춘 session.execute side_effect.

    호출 순서: replay SELECT → INSERT usage_logs → UPSERT user → UPSERT team
    → (threshold 있으면) cumulative SELECT. 마지막만 scalar_one_or_none 설정.
    """
    replay_result = MagicMock()
    replay_result.__iter__ = MagicMock(return_value=iter([]))
    writes = MagicMock()
    cumulative_result = MagicMock()
    cumulative_result.scalar_one_or_none = MagicMock(return_value=cumulative)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[replay_result, writes, writes, writes, cumulative_result]
    )
    return MagicMock(return_value=session)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_publishes_cumulative_usage_not_request_cost():
    """current_usage_usd 는 요청 단건 비용이 아니라 budget_usages 기간 누적."""
    session_factory = _mock_session_with_cumulative(Decimal("3.11"))

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(request_id="req-100pct", cost="0.178762", threshold=100)
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    assert redis.publish.await_count == 1
    import json

    event = json.loads(redis.publish.await_args_list[0].args[1])
    assert event["payload"]["current_usage_usd"] == "3.11"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_cumulative_fallback_to_request_cost():
    """누적 행이 없으면 요청 비용으로 폴백(이메일은 나간다)."""
    session_factory = _mock_session_with_cumulative(None)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(request_id="req-80pct", cost="0.50", threshold=80)
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    import json

    event = json.loads(redis.publish.await_args_list[0].args[1])
    assert event["payload"]["current_usage_usd"] == "0.50"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_team_scope_looks_up_team_id():
    """threshold_scope=team 이면 TEAM 스코프+team_id로 누적을 조회."""
    session_factory = _mock_session_with_cumulative(Decimal("9.99"))

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(
        request_id="req-team", cost="0.10", threshold=100, threshold_scope="team"
    )
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    session = session_factory.return_value
    cumulative_call = session.execute.await_args_list[4]
    params = cumulative_call.args[1]
    assert params["scope"] == "TEAM"
    assert params["scope_id"] == entry.team_id

    import json

    event = json.loads(redis.publish.await_args_list[0].args[1])
    assert event["payload"]["current_usage_usd"] == "9.99"


def _mock_session_with_replays(existing_ids: set[str]) -> MagicMock:
    """_filter_replays 가 existing_ids 를 돌려주는 session factory.

    호출 순서: replay SELECT → INSERT → UPSERT user → UPSERT team (+ 앱별 있으면
    1건 더). execute 가 MagicMock iterator 를 돌려주도록 세팅한다.
    """
    replay_result = MagicMock()
    replay_result.__iter__ = MagicMock(
        return_value=iter([(rid,) for rid in existing_ids])
    )
    writes = MagicMock()

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[replay_result, writes, writes, writes, writes]
    )
    return MagicMock(return_value=session)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_partial_replay_does_not_double_count_or_republish():
    """부분 replay: DB는 replay-filter로 보호되지만 카운터/알림도 같은 기준이어야 한다.

    spool 재발행/XAUTOCLAIM 겹침으로 이미 기록된 request_id 가 새 배치에 섞여
    들어오면, 예전엔 _bump_daily_counters/_publish_thresholds 가 원본 entries 로
    돌아 일별 카운터 이중 계상 + 임계 메일 재발송이 됐다. 수정 후 두 단계 모두
    실제 기록된(fresh) entries 만 반영해야 한다.
    """
    session_factory = _mock_session_with_replays({"req-replayed"})

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    replayed_user = "11111111-1111-1111-1111-111111111111"
    fresh_user = "22222222-2222-2222-2222-222222222222"
    entries = [
        _make_entry(
            request_id="req-replayed", user_id=replayed_user,
            cost="9.99", threshold=80,
        ),
        _make_entry(
            request_id="req-fresh", user_id=fresh_user,
            cost="0.10", threshold=90,
        ),
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # 일별 카운터: replayed 유저의 키는 절대 터치되면 안 된다.
    bumped_keys = [c.args[0] for c in pipe.incrbyfloat.call_args_list]
    bumped_keys += [c.args[0] for c in pipe.incrby.call_args_list]
    bumped_keys += [c.args[0] for c in pipe.sadd.call_args_list]
    assert bumped_keys, "fresh 항목의 카운터가 bump 되지 않았다"
    assert all(f"{{{replayed_user}}}" not in k for k in bumped_keys), (
        f"replay entry가 일별 카운터를 또 계상했다: {bumped_keys}"
    )
    assert any(f"{{{fresh_user}}}" in k for k in bumped_keys)

    # threshold 알림도 fresh 항목(threshold=90) 1건만 — replayed 의 80% 재발행 금지.
    assert redis.publish.await_count == 1
    import json

    event = json.loads(redis.publish.await_args_list[0].args[1])
    assert event["payload"]["threshold_pct"] == 90
    assert event["event_id"] == "req-fresh"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_all_replays_skips_counters_and_publish():
    """배치 전체가 replay 면 조기 반환 — 카운터/알림 모두 건드리지 않는다."""
    session_factory = _mock_session_with_replays({"req-a", "req-b"})

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id="req-a", threshold=80),
        _make_entry(request_id="req-b", threshold=100),
    ]
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    redis.pipeline.assert_not_called()
    redis.publish.assert_not_called()
