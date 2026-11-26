# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""관측 게이지 늦은 등록 회귀 — notification-worker 도 3c 와 같은 OTel 버그가 있었다."""
from __future__ import annotations

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
                out[m.name] = [
                    (dict(p.attributes), getattr(p, "value", None))
                    for p in m.data.data_points
                ]
    return out


def test_callbacks_registered_after_creation_are_observed(reader):
    """OTel SDK 는 생성 시 콜백 목록을 복사하므로, 나중 등록도 fan-out 이 보게 해야 한다."""
    from worker.observability.metrics import WorkerMetrics

    m = WorkerMetrics()
    m.register_db_buffer_callback(lambda _o: [Observation(7)])
    m.register_uptime_callback(lambda _o: [Observation(42.5)])

    points = _points(reader)
    assert points["worker_db_buffer_size"] == [({}, 7)]
    assert points["worker_uptime_seconds"] == [({}, 42.5)]
