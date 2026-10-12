# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OTel 메트릭 정의 — cost-recorder-worker.

주요 메트릭:
- entries_flushed: 배치 flush로 DB에 기록된 usage entry 총 수 (Counter)
- flush_errors: flush 실패 횟수 (Counter)
- batch_size: 단일 flush 배치 크기 (Histogram)
- stream_lag: cost:stream consumer group 의 미처리 entry 수
  (XINFO GROUPS lag — 미배달+pending 합산. 값이 없는 구버전이면 XPENDING 합계)
"""
from __future__ import annotations

from typing import Callable

from opentelemetry import metrics


class WorkerMetrics:
    def __init__(self, meter_name: str = "cost-recorder-worker") -> None:
        self._meter = metrics.get_meter(meter_name)

        self.entries_flushed = self._meter.create_counter(
            "cost_recorder_entries_flushed_total",
            description="Total number of cost entries flushed to DB",
            unit="1",
        )
        self.flush_errors = self._meter.create_counter(
            "cost_recorder_flush_errors_total",
            description="Number of batch flush failures",
            unit="1",
        )
        self.batch_size = self._meter.create_histogram(
            "cost_recorder_batch_size",
            description="Size of each flushed batch (entries)",
            unit="1",
        )
        # 미래 schema_version entry 수 — 게이트웨이가 워커보다 새 버전으로
        # 올라갔을 때 의미 롤링 변경을 놓치지 않기 위한 배포 검증 신호.
        self.future_schema_entries = self._meter.create_counter(
            "cost_recorder_future_schema_entries_total",
            description="Entries with schema_version newer than the worker supports",
            unit="1",
        )
        # dead-letter 로 소화돼 스트림에서는 진행됐지만 기록되지 않은 행 수 —
        # 과소청구 유실의 직접 계측. 0 이어야 정상.
        self.dead_letter_dropped = self._meter.create_counter(
            "cost_recorder_dead_letter_dropped_total",
            description="Entries dropped by dead-letter handling after grace",
            unit="1",
        )

        # Observable gauge — 콜백은 StreamConsumer 가 등록한다(주기 조회 결과를 들고 있음).
        self._stream_lag_callbacks: list[Callable] = []
        self.stream_lag = self._meter.create_observable_gauge(
            "cost_recorder_stream_lag",
            callbacks=self._stream_lag_callbacks,
            description="Unprocessed entries in the cost stream consumer group (lag)",
            unit="1",
        )

    def register_stream_lag_callback(self, callback: Callable) -> None:
        self._stream_lag_callbacks.append(callback)
