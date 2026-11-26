# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from collections.abc import Callable

from opentelemetry.metrics import Counter, Histogram, ObservableGauge

from worker.observability._setup import get_meter


def _fanout(callbacks: list[Callable]) -> list[Callable]:
    """관측 게이지에 넘길 콜백 — 등록 목록을 **수집 시점에** 읽는다.

    OTel SDK 는 ``create_observable_gauge(callbacks=…)`` 에서 목록을 복사한다. 그래서 생성 뒤
    ``register_*_callback`` 으로 붙인 콜백은 한 번도 불리지 않았고, 이 워커의 관측 게이지
    (db_buffer_size·uptime_seconds)는 Prometheus 에 시리즈 자체가 없었다 — cost-recorder-worker
    wave 3c 와 같은 버그다. 목록을 감싼 콜백 하나를 넘겨 늦은 등록도 보이게 한다.
    """
    def _observe(options):
        out = []
        for cb in list(callbacks):
            out.extend(cb(options))
        return out

    return [_observe]


class WorkerMetrics:
    """Notification Worker 커스텀 메트릭 (OBS-02).

    8개 지표:
    - events_received_total      Counter   (channel, event_type)
    - events_processed_total     Counter   (event_type, status: success/failed/skipped)
    - event_processing_duration  Histogram (event_type)
    - emails_sent_total          Counter   (event_type, status: sent/failed)
    - retry_total                Counter   (event_type)
    - errors_total               Counter   (error_type: parse/db/email/redis)
    - db_buffer_size             ObservableGauge
    - uptime_seconds             ObservableGauge
    """

    def __init__(self) -> None:
        meter = get_meter("notification-worker")

        self.events_received_total: Counter = meter.create_counter(
            "worker_events_received_total",
            description="Total Pub/Sub events received (labels: channel, event_type)",
        )
        self.events_processed_total: Counter = meter.create_counter(
            "worker_events_processed_total",
            description="Total events fully processed (labels: event_type, status)",
        )
        self.event_processing_duration: Histogram = meter.create_histogram(
            "worker_event_processing_duration_seconds",
            description="End-to-end event processing duration (labels: event_type)",
            unit="s",
        )
        self.emails_sent_total: Counter = meter.create_counter(
            "worker_emails_sent_total",
            description="Total email send attempts (labels: event_type, status)",
        )
        self.retry_total: Counter = meter.create_counter(
            "worker_retry_total",
            description="Total email send retries (labels: event_type)",
        )
        self.errors_total: Counter = meter.create_counter(
            "worker_errors_total",
            description="Total errors by category (labels: error_type)",
        )
        self.business_alerts_checked_total: Counter = meter.create_counter(
            "worker_business_alerts_checked_total",
            description="Business alert checks by outcome (labels: check, result)",
        )
        self.business_alert_deliveries_total: Counter = meter.create_counter(
            "worker_business_alert_deliveries_total",
            description="Business Slack alert deliveries (labels: check, status)",
        )

        # Observable gauges use callbacks registered after construction
        self._db_buffer_size_callbacks: list[Callable] = []
        self._uptime_callbacks: list[Callable] = []

        self.db_buffer_size: ObservableGauge = meter.create_observable_gauge(
            "worker_db_buffer_size",
            callbacks=_fanout(self._db_buffer_size_callbacks),
            description="Current size of the in-memory DB-failure event buffer",
        )
        self.uptime_seconds: ObservableGauge = meter.create_observable_gauge(
            "worker_uptime_seconds",
            callbacks=_fanout(self._uptime_callbacks),
            description="Worker process uptime in seconds",
            unit="s",
        )

    def register_db_buffer_callback(self, callback: Callable) -> None:
        self._db_buffer_size_callbacks.append(callback)

    def register_uptime_callback(self, callback: Callable) -> None:
        self._uptime_callbacks.append(callback)
