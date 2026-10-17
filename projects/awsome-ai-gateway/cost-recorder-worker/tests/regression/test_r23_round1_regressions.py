# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R23 r1 회귀 — 스트림 소비 경계 조건.

M1  소비 정지로 MAXLEN~ 백스톱이 **미배달** entry 를 잘라낸 갭을
    ``_sample_lag`` 가 first-entry > last-delivered-id 로 감지·계측한다.
    감지가 없으면 그 구간의 청구 행이 아무 신호 없이 영구 유실된다.

S1  ``request_stop`` 은 배치 경계에서 루프를 멈춘다 — 즉시 cancel 이면
    커밋~XACK 사이에 잘린 배치가 재전달돼 전량 replay 스킵되고 임계 알림·
    당일 카운터(비멱등)가 영구 생략된다.

N8  PEL 비어 있고 오래 idle 인 죽은 consumer 이름을 그룹에서 제거한다.

S6  임계 알림 PUBLISH 가 구독자 0에 나가면 유실로 계측한다 — pub/sub 은
    재생이 없고 sweeper 도 잡을 행이 없다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _settings(**over):
    s = MagicMock()
    s.cost_stream_key = "cost:stream"
    s.cost_stream_group = "g"
    s.cost_stream_consumer = "c1"
    s.batch_max_size = 100
    s.batch_max_interval_sec = 5.0
    s.xread_block_ms = 5000
    s.xautoclaim_interval_sec = 60.0
    s.xautoclaim_min_idle_ms = 300_000
    s.stream_dead_letter_grace_sec = 120.0
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _consumer(redis, metrics=None):
    from worker.stream_consumer import StreamConsumer

    return StreamConsumer(
        redis=redis,
        flusher=MagicMock(),
        settings=_settings(),
        metrics=metrics,
    )


@pytest.mark.asyncio
async def test_stream_gap_detected_when_head_passed_last_delivered():
    """first-entry > last-delivered-id → 미배달 트림 유실로 계측."""
    redis = MagicMock()
    redis.xinfo_groups = AsyncMock(return_value=[
        {"name": "g", "lag": 3, "pending": 1, "last-delivered-id": "1000-0"}
    ])
    redis.xinfo_stream = AsyncMock(return_value={
        "first-entry": ("2000-0", {"payload": "x"})
    })
    metrics = MagicMock()
    consumer = _consumer(redis, metrics)

    await consumer._sample_lag()

    metrics.stream_gap_detected.add.assert_called_once_with(1)
    assert consumer.last_lag == 3
    assert consumer.last_pending == 1


@pytest.mark.asyncio
async def test_stream_gap_not_flagged_for_fresh_group():
    """한 번도 배달 안 한 신생 그룹(0-0)은 미배달이 정상 — 오탐 금지."""
    redis = MagicMock()
    redis.xinfo_groups = AsyncMock(return_value=[
        {"name": "g", "lag": 50, "pending": 0, "last-delivered-id": "0-0"}
    ])
    metrics = MagicMock()
    consumer = _consumer(redis, metrics)

    await consumer._sample_lag()

    metrics.stream_gap_detected.add.assert_not_called()
    redis.xinfo_stream.assert_not_called()


@pytest.mark.asyncio
async def test_request_stop_exits_consume_loop_at_batch_boundary():
    """stop 요청 뒤 현재 배치만 끝내고 루프를 빠진다."""
    import json as _json

    redis = MagicMock()
    payload = _json.dumps({
        "request_id": "r1", "user_id": "u1", "team_id": "t1",
        "dept_id": "d1", "model_alias": "m", "provider": "bedrock",
        "input_tokens": 1, "output_tokens": 1, "cost_usd": "0.1",
        "latency_ms": 1, "status": "SUCCESS",
        "requested_at": "2026-10-17T00:00:00+00:00",
        "completed_at": "2026-10-17T00:00:01+00:00",
        "period": "2026-10", "date": "2026-10-17", "schema_version": 3,
    })
    calls = 0

    async def _xreadgroup(**_kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [("cost:stream", [("1-0", {"payload": payload})])]
        return None  # 이후는 타임아웃 — stop 없으면 외부 루프가 계속 돈다

    redis.xreadgroup = AsyncMock(side_effect=_xreadgroup)
    redis.xack = AsyncMock()
    flusher = MagicMock()

    from worker.stream_consumer import StreamConsumer

    consumer = StreamConsumer(
        redis=redis, flusher=flusher, settings=_settings(), metrics=None
    )
    consumer._trim_consumed = AsyncMock()
    # flush 가 끝나는 시점이 "배치 경계" — 여기서 종료를 요청한다.
    flusher.flush = AsyncMock(side_effect=lambda _e: consumer.request_stop())

    import asyncio

    await asyncio.wait_for(consumer._consume_live(), timeout=5)
    flusher.flush.assert_awaited_once()
    redis.xack.assert_awaited_once()  # 끊기기 전에 ACK 까지 완료됐다


@pytest.mark.asyncio
async def test_drain_backlog_exits_on_empty_pel_page():
    """XREADGROUP '0' 은 빈 PEL 에서도 [[stream, []]] (truthy) 를 돌려준다.

    ids 도 빈 페이지를 continue 하면 기동이 이 루프에서 영원히 멈춰
    _consume_live 에 도달하지 못한다 — 실 Redis 7 에서 재현 확인된
    r1 수정 회귀.
    """
    redis = MagicMock()
    reads = 0

    async def _xreadgroup(**_kw):
        nonlocal reads
        reads += 1
        if reads > 20:
            raise AssertionError("drain_backlog가 빈 PEL에서 종료하지 않음")
        return [("cost:stream", [])]  # 실 Redis 의 빈 PEL 응답 형태

    redis.xreadgroup = AsyncMock(side_effect=_xreadgroup)
    redis.xinfo_groups = AsyncMock(return_value=[])
    consumer = _consumer(redis)

    import asyncio

    await asyncio.wait_for(consumer._drain_backlog(), timeout=5)
    assert reads == 1  # 첫 빈 페이지에서 즉시 종료


@pytest.mark.asyncio
async def test_stop_all_waits_for_cooperative_exit_before_cancelling():
    """cooperative_window 안에 끝난 태스크는 cancel 되지 않는다(R23 r2 S-1).

    구형 stop_all 은 대기 전에 cancel 부터 해서 request_stop 플래그를
    코루틴이 볼 기회가 없었다 — 커밋~XACK 사이의 배치가 잘렸다.
    """
    import asyncio

    from worker.worker import TaskSupervisor

    supervisor = TaskSupervisor()
    finished: list[str] = []

    async def slow_ok():
        await asyncio.sleep(0.05)
        finished.append("ok")

    supervisor._tasks["t"] = asyncio.ensure_future(slow_ok())
    await supervisor.stop_all(grace_period=1.0, cooperative_window=0.5)
    assert finished == ["ok"]  # cancel 되지 않고 자연 완료됐다

    stuck = asyncio.ensure_future(asyncio.sleep(30))
    supervisor._tasks["s"] = stuck
    await supervisor.stop_all(grace_period=0.6, cooperative_window=0.1)
    assert stuck.cancelled()  # 창을 넘긴 태스크만 cancel 된다


def test_aggregation_sql_has_only_declared_bind_params():
    """주석 속 `:word` 가 바인드 파라미터로 파싱되면 안 된다(R23 r3 실측 발견).

    주석에 쓴 `:requests` 를 SQLAlchemy 가 파라미터로 파싱해 실 PG 실행이
    "A value is required for bind parameter" 로 실패했다.
    """
    from sqlalchemy import text

    from worker import daily_aggregator as da

    expected = {
        "AGG": {"tz", "start", "end"},
        "BACKFILL": {"tz", "today_local"},
        "MISSING": {"tz", "lookback_start", "lookback_start_date", "today_start"},
    }
    for name, sql in [
        ("AGG", da._AGG_SQL),
        ("BACKFILL", da._BACKFILL_SQL),
        ("MISSING", da._MISSING_DATES_SQL),
    ]:
        assert set(text(sql)._bindparams.keys()) == expected[name]


@pytest.mark.asyncio
async def test_dead_consumer_swept_when_idle_and_empty_pel():
    """idle>1h + pending=0 의 타 consumer 이름만 제거한다."""
    redis = MagicMock()
    redis.xinfo_consumers = AsyncMock(return_value=[
        {"name": "c1", "pending": 0, "idle": 100},            # 자기 자신 — 제외
        {"name": "old-pod", "pending": 0, "idle": 7_200_000},  # 제거 대상
        {"name": "busy", "pending": 3, "idle": 7_200_000},     # PEL 잔여 — 유지
        {"name": "fresh", "pending": 0, "idle": 60_000},       # 아직 젊다 — 유지
    ])
    redis.xgroup_delconsumer = AsyncMock()
    consumer = _consumer(redis)

    await consumer._sweep_dead_consumers()

    redis.xgroup_delconsumer.assert_awaited_once_with(
        "cost:stream", "g", "old-pod"
    )


@pytest.mark.asyncio
async def test_threshold_publish_zero_subscribers_is_metered():
    """구독자 0 에 발행된 임계 알림은 기록 없이 유실 — 반드시 계측한다."""
    from decimal import Decimal
    from worker.batch_flusher import BatchFlusher
    from worker.schemas.cost_stream import CostStreamEntry

    redis = MagicMock()
    redis.publish = AsyncMock(return_value=0)
    metrics = MagicMock()
    flusher = BatchFlusher(
        session_factory=MagicMock(), redis=redis, metrics=metrics
    )

    entry = CostStreamEntry(
        request_id="r1", user_id="u1", team_id="t1", dept_id="d1",
        model_alias="m", provider="bedrock", input_tokens=10,
        output_tokens=5, cache_creation_tokens=0, cache_read_tokens=0,
        reasoning_tokens=0, web_search_count=0, cost_usd=Decimal("1"),
        latency_ms=1, ttft_ms=1, status="SUCCESS",
        requested_at="2026-10-17T00:00:00+00:00",
        completed_at="2026-10-17T00:00:01+00:00",
        is_streaming=True, estimated_usage=False,
        threshold_triggered=80, period="2026-10", date="2026-10-17",
        schema_version=3,
    )
    # 누적 조회는 실패해도 발행 경로는 그대로 간다 — session_factory 목.
    flusher._session_factory = MagicMock()
    session = MagicMock()
    session.execute = AsyncMock(side_effect=Exception("db down"))
    flusher._session_factory.return_value.__aenter__ = AsyncMock(
        return_value=session
    )
    flusher._session_factory.return_value.__aexit__ = AsyncMock()

    await flusher._publish_thresholds([entry])

    metrics.threshold_publish_lost.add.assert_called_once_with(1)
