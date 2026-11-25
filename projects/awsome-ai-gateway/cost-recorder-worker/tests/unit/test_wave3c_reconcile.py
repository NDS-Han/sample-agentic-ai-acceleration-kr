# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3c — 관측 게이지 늦은 등록, 대조 지표 이름, 보정 CLI 인자 검증(PG 없이)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


@pytest.fixture
def reader(monkeypatch):
    from opentelemetry import metrics as otel_metrics

    r = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[r])
    monkeypatch.setattr(otel_metrics, "get_meter", mp.get_meter)
    yield r
    mp.shutdown()


def _points(reader) -> dict[str, list]:
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = [(dict(p.attributes), getattr(p, "value", None))
                               for p in m.data.data_points]
    return out


def test_callbacks_registered_after_creation_are_observed(reader):
    """OTel SDK 는 생성 시 콜백 목록을 복사한다 — wave 3 의 게이지들은 한 번도 보고되지 않았다."""
    from worker.observability.metrics import WorkerMetrics

    m = WorkerMetrics()
    m.register_stream_lag_callback(lambda _o: [Observation(7)])
    m.register_stream_pending_callback(lambda _o: [Observation(2)])
    m.register_reconciliation_callback(lambda _o: [Observation(3, {"check": "budget_vs_raw"})])
    m.register_reconciliation_success_callback(lambda _o: [Observation(1_795_600_000.0)])
    m.register_rollup_success_callback(lambda _o: [Observation(5.0, {"job": "hourly"})])
    p = _points(reader)
    assert p["cost_recorder_stream_lag"] == [({}, 7)]
    assert p["cost_recorder_stream_pending"] == [({}, 2)]
    assert p["cost_recorder_reconciliation_mismatches"] == [({"check": "budget_vs_raw"}, 3)]
    assert p["reconciliation_last_success_timestamp"] == [({}, 1_795_600_000.0)]
    assert p["cost_recorder_rollup_last_success_timestamp_seconds"] == [({"job": "hourly"}, 5.0)]


def test_mismatch_counter_series_exist_from_start_and_add(reader):
    from worker.observability.metrics import RECONCILIATION_CHECKS, WorkerMetrics

    m = WorkerMetrics()
    p = _points(reader)
    assert {a["check"] for a, _ in p["reconciliation_mismatch_total"]} == set(RECONCILIATION_CHECKS)
    assert all(v == 0 for _, v in p["reconciliation_mismatch_total"])
    m.reconciliation_mismatch_total.add(4, {"check": "budget_vs_raw"})
    by = {a["check"]: v for a, v in _points(reader)["reconciliation_mismatch_total"]}
    assert by["budget_vs_raw"] == 4 and by["redis_vs_db"] == 0


def test_gauge_units_do_not_get_ratio_suffix(reader):
    """collector 변환은 unit "1" 게이지에 _ratio 를 붙인다(알림 규칙 이름이 어긋난다)."""
    from worker.observability.metrics import WorkerMetrics

    WorkerMetrics()
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for met in sm.metrics:
                if met.name in ("cost_recorder_stream_lag", "cost_recorder_stream_pending",
                                "cost_recorder_reconciliation_mismatches",
                                "reconciliation_last_success_timestamp"):
                    assert met.unit == ""


async def test_backfill_cli_validates_arguments(monkeypatch):
    from unittest.mock import MagicMock

    from worker import backfill_budget_legacy as bb

    monkeypatch.setattr(bb, "get_settings", lambda: MagicMock(reporting_timezone="Asia/Kolkata"))
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert await bb.run(["--cutoff", "2026-11-25T11:07:47"]) == 1  # 오프셋 없음
    assert await bb.run(["--cutoff", future]) == 1
    assert await bb.run(["--cutoff", "2026-11-25T11:07:47Z", "--kinds", "everything"]) == 1


def test_fix_keys_match_gateway_counters():
    from worker.backfill_budget_legacy import Fix

    f = Fix("web_search", "USER", "u1", None, "2026-11", Decimal("0.007"))
    assert f.redis_key() == "budget:user:{u1}:2026-11"
    assert f.rid == "web_search:USER:u1::2026-11"
    assert Fix("web_search", "TEAM", "t1", None, "2026-11", Decimal(0)).redis_key() == (
        "budget:team:{t1}:2026-11")
    assert Fix("rounding", "USER", "u1", "claude-code", "2026-11", Decimal(0)).redis_key() == (
        "budget:user:{u1}:claude-code:2026-11")
