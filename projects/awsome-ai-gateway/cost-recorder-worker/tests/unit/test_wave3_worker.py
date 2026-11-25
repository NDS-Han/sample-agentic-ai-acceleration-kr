# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3 — 워커: dept 를 팀에서 해석(B5), 영구 오류/디코드 불가 행은 dead-letter 표로(B5),
CANCELLED 상태와 pricing_missing 표시를 그대로 기록(B1/B7)."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from worker.batch_flusher import DEFAULT_DEPT_ID, BatchFlusher
from worker.schemas.cost_stream import CostStreamEntry

pytestmark = pytest.mark.unit

TEAM = "00000000-0000-0000-0000-0000000000aa"
DEPT = "00000000-0000-0000-0000-0000000000bb"


def _entry(rid: str, **kw) -> CostStreamEntry:
    base = dict(
        request_id=rid, user_id="00000000-0000-0000-0000-000000000001", team_id=TEAM,
        dept_id=DEFAULT_DEPT_ID, model_alias="m", provider="BEDROCK", input_tokens=10,
        output_tokens=5, cost_usd=Decimal("0.01"), latency_ms=1, is_streaming=True,
        estimated_usage=False, requested_at="2026-04-21T10:00:00+00:00",
        completed_at="2026-04-21T10:00:01+00:00", period="2026-04", date="2026-04-21",
    )
    base.update(kw)
    return CostStreamEntry(**base)


class _Db:
    def __init__(self, fail_insert_for: set[str] | None = None):
        self.inserts: list[dict] = []
        self.dead: list[dict] = []
        self.team_lookups = 0
        self.fail_insert_for = fail_insert_for or set()

    def factory(self):
        db = self

        async def execute(stmt, params=None):
            sql = str(stmt)
            r = MagicMock()
            if "FROM auth.teams" in sql:
                db.team_lookups += 1
                r.all.return_value = [(TEAM, DEPT)] if TEAM in params["ids"] else []
            elif "INSERT INTO usage.usage_logs" in sql:
                if params["request_id"] in db.fail_insert_for:
                    raise IntegrityError("INSERT", params, Exception("fk"))
                db.inserts.append(dict(params))
                r.scalar_one_or_none.return_value = params["request_id"]
            elif "cost_dead_letters" in sql:
                db.dead.extend(params if isinstance(params, list) else [params])
            else:
                r.__iter__ = lambda _s: iter([])
                r.scalar_one_or_none.return_value = None
            return r

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.execute = AsyncMock(side_effect=execute)
        return MagicMock(return_value=session)


def _redis():
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    r = MagicMock()
    r.pipeline = MagicMock(return_value=pipe)
    r.publish = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_dept_is_resolved_from_team_and_cached():
    db = _Db()
    f = BatchFlusher(session_factory=db.factory(), redis=_redis())
    await f.flush([_entry("a"), _entry("b", dept_id="")])
    assert {p["dept_id"] for p in db.inserts} == {DEPT}
    await f.flush([_entry("c")])
    assert db.team_lookups == 1  # TTL 캐시


@pytest.mark.asyncio
async def test_real_dept_is_kept():
    db = _Db()
    f = BatchFlusher(session_factory=db.factory(), redis=_redis())
    real = "00000000-0000-0000-0000-0000000000cc"
    await f.flush([_entry("a", dept_id=real)])
    assert db.inserts[0]["dept_id"] == real and db.team_lookups == 0


@pytest.mark.asyncio
async def test_permanent_row_error_goes_to_dead_letter_with_raw_payload():
    db = _Db(fail_insert_for={"bad"})
    f = BatchFlusher(session_factory=db.factory(), redis=_redis())
    bad = _entry("bad")
    bad._raw = '{"request_id":"bad","original":true}'
    bad._stream_id = "1700000000000-0"
    await f.flush([_entry("ok"), bad])
    # (가짜 세션은 롤백을 모델링하지 않는다 — 배치 시도 + 행 단위 재시도가 둘 다 보인다)
    assert {p["request_id"] for p in db.inserts} == {"ok"}
    assert len(db.dead) == 1
    d = db.dead[0]
    assert d["request_id"] == "bad" and d["stream_id"] == "1700000000000-0"
    assert d["payload"] == bad._raw and d["reason"] == "permanent_row_error"


@pytest.mark.asyncio
async def test_dead_letter_write_failure_propagates_no_ack_and_drop():
    """dead-letter 쓰기가 실패하면 예외 — 호출자는 XACK 하지 않는다(버리지 않는다)."""
    db = _Db(fail_insert_for={"bad"})
    factory = db.factory()
    session = factory.return_value
    orig = session.execute.side_effect

    async def exec2(stmt, params=None):
        if "cost_dead_letters" in str(stmt):
            raise ConnectionError("db down")
        return await orig(stmt, params)

    session.execute.side_effect = exec2
    f = BatchFlusher(session_factory=factory, redis=_redis())
    with pytest.raises(ConnectionError):
        await f.flush([_entry("bad")])


@pytest.mark.asyncio
async def test_cancelled_status_and_pricing_missing_recorded():
    db = _Db()
    f = BatchFlusher(session_factory=db.factory(), redis=_redis())
    await f.flush([_entry("c", status="CANCELLED", pricing_missing=True, estimated_usage=True)])
    p = db.inserts[0]
    assert (p["status"], p["pricing_missing"], p["estimated_usage"]) == ("CANCELLED", True, True)


@pytest.mark.asyncio
async def test_consumer_dead_letters_undecodable_before_ack():
    from worker.stream_consumer import StreamConsumer

    db = _Db()
    flusher = BatchFlusher(session_factory=db.factory(), redis=_redis())
    settings = MagicMock(cost_stream_key="s", cost_stream_group="g", cost_stream_consumer="c",
                         batch_max_size=10, batch_max_interval_sec=1, xread_block_ms=1,
                         xautoclaim_interval_sec=60, xautoclaim_min_idle_ms=1,
                         stream_dead_letter_grace_sec=120)
    redis = MagicMock()
    redis.xack = AsyncMock()
    c = StreamConsumer(redis=redis, flusher=flusher, settings=settings)
    entries, ids = c._decode([("s", [("1-0", {b"payload": b"{not json"}),
                                     ("2-0", {b"payload": json.dumps({"x": 1}).encode()})])])
    assert entries == [] and ids == ["1-0", "2-0"]
    await c._ack(ids)
    assert [d["stream_id"] for d in db.dead] == ["1-0", "2-0"]
    assert db.dead[0]["payload"] == "{not json" and db.dead[0]["reason"] == "parse_failed"
    redis.xack.assert_awaited_once()
