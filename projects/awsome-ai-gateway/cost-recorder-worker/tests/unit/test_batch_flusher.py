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


def _insert_returning_exec(stmt, params=None):
    """session.execute side_effect — INSERT ... RETURNING 에 실제 삽입된
    request_id 를 돌려준다. 그 외(replay SELECT/UPSERT)는 기본 MagicMock."""
    result = MagicMock()
    if "RETURNING" in str(stmt):
        result.scalar_one_or_none.return_value = (
            params["request_id"] if isinstance(params, dict) else None
        )
    return result


def _make_entry(
    request_id: str = "req-1",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    team_id: str = "00000000-0000-0000-0000-000000000002",
    cost: str = "0.01",
    threshold: int | None = None,
    threshold_scope: str | None = None,
    teamless: bool = False,
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
        teamless=teamless,
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

    async def _exec(stmt, params=None):
        result = MagicMock()
        if "RETURNING" in str(stmt):
            # 행 단위 INSERT ... RETURNING — 실제 삽입된 것처럼 request_id 반환
            result.scalar_one_or_none.return_value = (
                params["request_id"] if isinstance(params, dict) else None
            )
        return result

    session.execute = AsyncMock(side_effect=_exec)
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

    # 6 session.execute 호출: 재처리 필터 SELECT + INSERT × 3(행 단위, RETURNING)
    # + UPSERT user budget + UPSERT team budget
    #
    # ⚠️ 첫 호출이 재처리 필터여야 한다. budget_usages 는 **가산** UPSERT 라, 재처리된
    #    entry 를 걸러내지 않으면 사용자의 기록 사용액이 영구히 두 배가 된다
    #    (batch_flusher._filter_replays 주석 참조). 개수만 세면 그 SELECT 가 뒤로 밀려
    #    무의미해진 것을 잡지 못하므로 순서를 함께 못 박는다.
    assert session.execute.await_count == 6
    assert session.commit.await_count == 1
    replay_sql = str(session.execute.await_args_list[0].args[0]).lower()
    assert "from usage.usage_logs" in replay_sql and "request_id" in replay_sql, (
        f"첫 호출이 재처리 필터 SELECT 가 아니다: {replay_sql[:120]}"
    )

    # 마지막 두 호출 = user UPSERT, team UPSERT — 합산된 cost (0.30)
    user_call = session.execute.await_args_list[4]
    user_params = user_call.args[1]
    assert len(user_params) == 1  # 단일 (user, period) 그룹
    assert user_params[0]["scope"] == "USER"
    assert user_params[0]["cost"] == "0.30"

    team_call = session.execute.await_args_list[5]
    team_params = team_call.args[1]
    assert team_params[0]["scope"] == "TEAM"
    assert team_params[0]["cost"] == "0.30"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_triggered_publishes_notification():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=_insert_returning_exec)
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
    session.execute = AsyncMock(side_effect=_insert_returning_exec)
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
    """SQL 내용에 맞춘 session.execute side_effect.

    INSERT ... RETURNING 은 request_id 를, 누적 SELECT 는 cumulative 를,
    replay SELECT 는 빈 결과를 돌려준다.
    """

    async def _exec(stmt, params=None):
        sql = str(stmt)
        result = MagicMock()
        if "RETURNING" in sql:
            result.scalar_one_or_none.return_value = (
                params["request_id"] if isinstance(params, dict) else None
            )
        elif "used_usd" in sql:
            result.scalar_one_or_none.return_value = cumulative
        return result

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(side_effect=_exec)
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
    async def _exec(stmt, params=None):
        sql = str(stmt)
        result = MagicMock()
        if "RETURNING" in sql:
            result.scalar_one_or_none.return_value = (
                params["request_id"] if isinstance(params, dict) else None
            )
        elif "request_id" in sql:
            result.__iter__ = MagicMock(
                return_value=iter([(rid,) for rid in existing_ids])
            )
        return result

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(side_effect=_exec)
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


# ─────────────────────────────────────────────────────────────────────────────
# E2E S5 — usage_logs.status 전파 (워커 SUCCESS 하드코딩 회귀)
# ─────────────────────────────────────────────────────────────────────────────
def _insert_param_capturing_exec(captured: list):
    """INSERT 파라미터를 captured 에 기록하고 RETURNING 은 삽입 성공을 반환."""

    async def _exec(stmt, params=None):
        result = MagicMock()
        if "RETURNING" in str(stmt):
            if isinstance(params, dict):
                captured.append(dict(params))
            result.scalar_one_or_none.return_value = (
                params["request_id"] if isinstance(params, dict) else None
            )
        return result

    return _exec


def _session_factory_for(exec_fn) -> MagicMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(side_effect=exec_fn)
    return MagicMock(return_value=session)


def _counter_redis() -> tuple[MagicMock, MagicMock]:
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()
    return redis, pipe


@pytest.mark.asyncio
@pytest.mark.unit
async def test_entry_status_written_to_usage_logs():
    """스트림 엔트리의 status 가 INSERT 파라미터까지 간다 — SUCCESS 하드코딩 회귀."""
    captured: list = []
    session_factory = _session_factory_for(_insert_param_capturing_exec(captured))
    redis, _pipe = _counter_redis()

    entries = [
        _make_entry(request_id="req-ok"),
        _make_entry(request_id="req-err"),
        _make_entry(request_id="req-timeout"),
    ]
    entries[1].status = "ERROR"
    entries[2].status = "TIMEOUT"

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    statuses = {p["request_id"]: p["status"] for p in captured}
    assert statuses == {
        "req-ok": "SUCCESS",
        "req-err": "ERROR",
        "req-timeout": "TIMEOUT",
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_invalid_status_coerced_to_error_not_success():
    """enum 밖 값은 ERROR 로 — SUCCESS 삼킴(실패 오보고)도 배치 깨짐도 아니다."""
    from worker.batch_flusher import _usage_status

    e = _make_entry()
    e.status = "PARTIAL"
    assert _usage_status(e) == "ERROR"
    e.status = "bogus"
    assert _usage_status(e) == "ERROR"
    e.status = None
    assert _usage_status(e) == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_zero_usage_error_marker_skips_budget_and_counters():
    """0 토큰·0 비용 실패 마커 행은 INSERT 되지만 예산/카운터에는 올리지 않는다.

    게이트웨이는 실패 종결의 0 토큰 행만 발행한다 — monitoring 의 ERROR/TIMEOUT
    집계가 이 행을 읽는다. 예산 스냅샷과 일별 카운터는 이 행으로 아무것도
    만들면 안 된다.
    """
    captured: list = []
    session_factory = _session_factory_for(_insert_param_capturing_exec(captured))
    redis, pipe = _counter_redis()

    e = _make_entry(request_id="req-fail-zero", cost="0")
    e.status = "ERROR"
    e.input_tokens = 0
    e.output_tokens = 0
    e.cache_creation_tokens = 0
    e.cache_read_tokens = 0

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([e])

    # usage_logs INSERT 는 일어난다(status=ERROR)
    assert len(captured) == 1
    assert captured[0]["status"] == "ERROR"
    # budget UPSERT 는 호출되지 않는다(replay SELECT + INSERT 만 실행)
    session = session_factory.return_value
    sqls = [str(c.args[0]) for c in session.execute.await_args_list]
    assert len(sqls) == 2, f"예산 UPSERT 가 추가 호출됐다: {sqls}"
    assert not any("INTO budget.budget_usages" in s for s in sqls)
    # daily 카운터도 건너뛴다
    pipe.incrbyfloat.assert_not_called()
    pipe.incrby.assert_not_called()
    pipe.sadd.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_poison_value_error_row_skipped_others_recorded():
    """깨진 requested_at(ValueError — fromisoformat 실패)은 영구 오류다.

    페이로드가 스키마(str)를 통과하고 INSERT params 빌드에서 죽는 경로.
    ValueError 가 전파되면 배치가 PEL 에 남아 flush 가 영원히 실패해
    스트림 전체를 봉쇄한다 — poison 행만 스킵하고 나머지는 기록돼야 한다.
    """
    captured: list = []
    session_factory = _session_factory_for(_insert_param_capturing_exec(captured))
    redis, pipe = _counter_redis()

    good = _make_entry(request_id="req-good")
    poison = _make_entry(request_id="req-poison")
    poison.requested_at = "not-a-timestamp"

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([good, poison])  # 예외 없이 반환돼 호출자가 XACK 까지 진행

    assert {p["request_id"] for p in captured} == {"req-good"}
    assert not any(p["request_id"] == "req-poison" for p in captured)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_poison_data_error_row_skipped_others_recorded():
    """CAST(:user_id AS uuid) 실패(DataError)도 영구 오류 — poison 행만 스킵."""
    from sqlalchemy.exc import DataError

    async def _exec(stmt, params=None):
        result = MagicMock()
        if "RETURNING" in str(stmt):
            if isinstance(params, dict) and params["request_id"] == "req-bad-uuid":
                raise DataError("INSERT", params, Exception("invalid input for uuid"))
            result.scalar_one_or_none.return_value = (
                params["request_id"] if isinstance(params, dict) else None
            )
        return result

    session_factory = _session_factory_for(_exec)
    redis, pipe = _counter_redis()

    good = _make_entry(request_id="req-good")
    bad = _make_entry(request_id="req-bad-uuid", user_id="not-a-uuid")

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([good, bad])

    # 정상 행은 배치 경로에서 기록됐고, per-row 재시도에서도 poison 만 걸린다
    # (per-row 에서 req-good 이 재시도돼도 replay 필터가 걸러낸다 — 핵심은
    # flush 가 예외 없이 끝나 XACK 되는 것과 배치가 봉쇄되지 않는 것).
    session = session_factory.return_value
    assert session.commit.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transient_operational_error_still_propagates():
    """일시 오류(OperationalError)는 전파 — 삼키면 미청구 유실(at-least-once 위반)."""
    from sqlalchemy.exc import OperationalError

    async def _exec(stmt, params=None):
        raise OperationalError("SELECT", {}, Exception("connection reset"))

    session_factory = _session_factory_for(_exec)
    redis, _pipe = _counter_redis()

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    with pytest.raises(OperationalError):
        await flusher.flush([_make_entry()])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flush_dead_letter_reraises_transient_errors():
    """일시 계열 오류(연결·타임아웃)는 dead-letter 경로에서도 재전파한다.

    grace(120s) 만료만으로는 결정적/일시를 구별할 수 없다 — RDS 페일오버처럼
    120s 넘는 DB 장애에서 무작위로 삼키면 모든 행이 written=0 으로 **영구
    과소청구 유실**. 일시 오류는 재전파해 배치를 PEL 에 남겨야 한다.
    """
    from sqlalchemy.exc import OperationalError

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(
        side_effect=OperationalError("SELECT 1", {}, Exception("db down"))
    )

    session_factory = MagicMock(return_value=session)
    flusher = BatchFlusher(session_factory=session_factory, redis=MagicMock())

    entries = [_make_entry(request_id=f"req-{i}") for i in range(2)]
    with pytest.raises(OperationalError):
        await flusher.flush_dead_letter(entries)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flush_dead_letter_swallows_permanent_errors_per_row():
    """영구 오류 행만 드롭하고 나머지 행은 기록한다 — 결정적 오류의 목적."""
    from sqlalchemy.exc import DataError

    calls = {"n": 0}

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    async def _exec(stmt, params=None):
        result = MagicMock()
        sql = str(stmt)
        if "RETURNING" in sql:
            calls["n"] += 1
            if calls["n"] == 1:
                raise DataError("INSERT", {}, Exception("bad uuid"))
            result.scalar_one_or_none.return_value = params["request_id"]
        return result

    session.execute = AsyncMock(side_effect=_exec)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    flusher = BatchFlusher(session_factory=session_factory, redis=MagicMock())
    entries = [_make_entry(request_id=f"req-{i}") for i in range(2)]
    written = await flusher.flush_dead_letter(entries)
    assert written == 1  # 첫 행만 dead-letter, 둘째는 기록


@pytest.mark.asyncio
@pytest.mark.unit
async def test_teamless_entry_skips_team_budget_upsert():
    """팀 없는 사용자의 usage 는 TEAM 스코프 budget_usages 에 합산하지 않는다.

    gateway 는 usage_logs NOT NULL 을 위해 default team 센티널을 채우는데,
    그 행을 팀 집계에 넣으면 팀 없는 사용자의 지출이 default team 한도를
    소진해 엉뚱한 팀이 hard_block 된다. teamless 플래그로 걸러낸다.
    """
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=_insert_returning_exec)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [_make_entry(request_id="req-t", teamless=True)]
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    upsert_scopes = [
        c.args[1][0]["scope"]
        for c in session.execute.await_args_list
        if isinstance(c.args[1], list)
    ]
    assert "USER" in upsert_scopes
    assert "TEAM" not in upsert_scopes
