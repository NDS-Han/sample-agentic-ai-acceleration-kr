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
        # 그 외 기록 없이 버려진 entry 수 — per-row 영구 오류 스킵, 디코드 불가,
        # 본문 트림 ((id,None)/deleted_ids) 등 dead-letter 바깥의 유실 경로 전부.
        # reason 라벨로 경로를 구분한다. 0 이어야 정상.
        self.entries_dropped = self._meter.create_counter(
            "cost_recorder_entries_dropped_total",
            description="Cost entries dropped outside dead-letter path (labels: reason)",
            unit="1",
        )
        # 소비 정지로 게이트웨이 MAXLEN~ 백스톱이 미배달 entry 를 잘라낸 흔적 —
        # XINFO first-entry 가 그룹 last-delivered-id 를 앞설 때 발화. 이 카운터가
        # 오르면 그 구간의 usage_logs/budget_usages 는 영구 유실(과소청구)이다.
        self.stream_gap_detected = self._meter.create_counter(
            "cost_recorder_stream_gap_total",
            description="Times the stream head was found trimmed ahead of last-delivered-id",
            unit="1",
        )
        # 임계 알림 PUBLISH 가 구독자 0에 발행돼 유실된 횟수 — pub/sub 은 재생이
        # 없으므로 이 카운터가 오른 알림은 어디에도 기록되지 않는다.
        self.threshold_publish_lost = self._meter.create_counter(
            "cost_recorder_threshold_publish_lost_total",
            description="Threshold alert publishes that reached zero subscribers",
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
        # PEL(unacked·배달됐지만 미처리) 크기 — lag 은 미배달+pending 합산이라
        # 미배달 백로그와 회수 대기를 구분하려면 별도 게이지가 필요하다.
        self._stream_pending_callbacks: list[Callable] = []
        self.stream_pending = self._meter.create_observable_gauge(
            "cost_recorder_stream_pending",
            callbacks=self._stream_pending_callbacks,
            description="Pending (delivered but unacked) entries in the consumer group",
            unit="1",
        )

    def register_stream_lag_callback(self, callback: Callable) -> None:
        self._stream_lag_callbacks.append(callback)

    def register_stream_pending_callback(self, callback: Callable) -> None:
        self._stream_pending_callbacks.append(callback)
