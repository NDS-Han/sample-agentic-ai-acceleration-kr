# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3b 단위 — 수명 이벤트 디코드, 스트림 디코드, budget_settle.lua 사본 동일성, 추정 행 모양."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from worker.schemas.cost_stream import (
    CostStreamEntry,
    RequestCloseEvent,
    RequestStartEvent,
    decode_event,
)

_START = {
    "kind": "start", "request_id": "r1", "instance_id": "h:1:abcd", "user_id": "u", "team_id": "t",
    "teamless": False, "dept_id": "d", "model_alias": "m", "provider": "BEDROCK",
    "client": "claude-code", "estimated_input_tokens": 1200, "estimated_cost_usd": "0.0036",
    "requested_at": "2026-11-25T10:00:00+00:00", "period": "2026-11", "date": "2026-11-25",
    "budget_reservation": [{"scope": "user", "usage_key": "k"}], "schema_version": 4,
}


def test_decode_event_kinds():
    assert isinstance(decode_event(_START), RequestStartEvent)
    assert isinstance(decode_event({"kind": "close", "request_id": "r1"}), RequestCloseEvent)
    end = {"request_id": "r1", "user_id": "u", "team_id": "t", "dept_id": "d", "model_alias": "m",
           "provider": "p", "input_tokens": 1, "output_tokens": 2, "cost_usd": "0.1",
           "latency_ms": 1, "requested_at": "2026-11-25T10:00:00+00:00",
           "completed_at": "2026-11-25T10:00:01+00:00", "period": "2026-11", "date": "2026-11-25"}
    assert isinstance(decode_event(end), CostStreamEntry)  # kind 없음 = 완료(구 게이트웨이 포함)
    with pytest.raises(Exception):
        decode_event({"kind": "start", "request_id": "x"})  # 필수 필드 없음 → dead-letter 대상


def test_start_event_clamps_bad_numbers():
    st = RequestStartEvent(**{**_START, "estimated_input_tokens": -5,
                              "estimated_cost_usd": "NaN"})
    assert st.estimated_input_tokens == 0 and st.estimated_cost_usd == Decimal("0")


def test_start_to_entry_is_estimated_error_row():
    from worker.sweeper import start_to_entry

    e = start_to_entry(RequestStartEvent(**_START))
    assert e.estimated_usage is True and e.status == "ERROR"
    assert (e.input_tokens, e.output_tokens, e.cost_usd) == (1200, 0, Decimal("0.0036"))
    assert e.web_search_cost_usd == Decimal("0") and e.period == "2026-11"
    assert e.requested_at == _START["requested_at"]


def test_stream_decode_mixed_batch():
    from unittest.mock import MagicMock

    from worker.stream_consumer import StreamConsumer

    settings = MagicMock(cost_stream_key="s", cost_stream_group="g", cost_stream_consumer="c",
                         batch_max_size=10, batch_max_interval_sec=1, xread_block_ms=10,
                         xautoclaim_interval_sec=1, xautoclaim_min_idle_ms=1,
                         stream_dead_letter_grace_sec=1)
    c = StreamConsumer(redis=MagicMock(), flusher=MagicMock(), settings=settings)
    msgs = [("s", [
        (b"1-0", {b"payload": json.dumps(_START).encode()}),
        (b"2-0", {b"payload": b'{"kind":"close","request_id":"r1"}'}),
        (b"3-0", {b"payload": b'{"kind":"start"}'}),
    ])]
    entries, ids = c._decode(msgs)
    assert ids == ["1-0", "2-0", "3-0"]
    assert [type(e).__name__ for e in entries] == ["RequestStartEvent", "RequestCloseEvent"]
    assert entries[0]._raw and entries[0]._stream_id == "1-0"
    assert len(c._undecodable) == 1  # 깨진 start 는 dead-letter 로(ACK 전에)


def test_budget_settle_lua_copy_matches_gateway():
    """워커 사본이 게이트웨이 원본과 같아야 한다 — 정산 규칙이 두 벌이면 마커 의미가 어긋난다."""
    here = Path(__file__).resolve()
    worker_copy = here.parents[2] / "src" / "worker" / "redis_scripts" / "budget_settle.lua"
    gateway = (here.parents[3] / "gateway-proxy" / "src" / "app" / "redis_scripts"
               / "budget_settle.lua")
    if not gateway.exists():
        pytest.skip("gateway-proxy 소스가 없는 체크아웃")
    assert worker_copy.read_text() == gateway.read_text()


def test_settle_args_shape():
    from worker.sweeper import _settle_args

    resv = {"usage_key": "u", "config_key": "c", "marker_key": "m", "resvsum_key": "r",
            "idx_key": "i", "idx_field": "f", "fallback": "", "reserved": "2", "marker_ttl": "60"}
    assert _settle_args(resv, Decimal("0.5")) == (5, "u", "c", "m", "r", "i", "0.5", "", "f", "2",
                                                  "60")
    old = {k: v for k, v in resv.items() if k not in ("idx_key", "idx_field")}
    assert _settle_args(old, Decimal("0.5"))[:5] == (4, "u", "c", "m", "r")
    assert _settle_args({"usage_key": "u"}, Decimal("1")) is None
