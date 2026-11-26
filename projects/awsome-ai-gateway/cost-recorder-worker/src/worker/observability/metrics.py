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

from collections.abc import Callable

from opentelemetry import metrics

#: 비용 대조 검사 이름 — 카운터 시리즈를 기동 시 0 으로 만들어 둔다(increase() 가 첫 증가를 본다).
RECONCILIATION_CHECKS = ("raw_vs_daily", "monthly_vs_daily", "budget_vs_raw", "redis_vs_db",
                         "pricing", "dirty_stale", "dead_letters_open", "retro_not_charged",
                         "web_search_cost_gap")


def _fanout(callbacks: list[Callable]) -> list[Callable]:
    """관측 게이지에 넘길 콜백 — 등록 목록을 **수집 시점에** 읽는다.

    OTel SDK 는 ``create_observable_gauge(callbacks=…)`` 에서 목록을 복사한다. 그래서 생성 뒤
    ``register_*_callback`` 으로 붙인 콜백은 한 번도 불리지 않았고, 이 워커의 관측 게이지
    (stream_lag·stream_pending·reconciliation_mismatches·rollup_last_success)는 dev Prometheus 에
    시리즈 자체가 없었다(wave 3c 실측). 목록을 감싼 콜백 하나를 넘겨 늦은 등록도 보이게 한다.
    """
    def _observe(options):
        out = []
        for cb in list(callbacks):
            out.extend(cb(options))
        return out

    return [_observe]


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
        # 커밋/XACK 경합 재배달로 걸러진 entry 수 — replay 스톰(dup XADD·이중
        # 배달 버그)은 로그만 남고 대시보드에선 보이지 않았다. 0 이어야 정상.
        self.replays_filtered = self._meter.create_counter(
            "cost_recorder_replays_filtered_total",
            description="Entries filtered as already-recorded replays",
            unit="1",
        )
        # 임계 알림 PUBLISH 가 구독자 0에 발행돼 유실된 횟수 — pub/sub 은 재생이
        # 없으므로 이 카운터가 오른 알림은 어디에도 기록되지 않는다.
        self.threshold_publish_lost = self._meter.create_counter(
            "cost_recorder_threshold_publish_lost_total",
            description="Threshold alert publishes that reached zero subscribers",
            unit="1",
        )

        # wave 3 — 영구 오류로 usage_logs 에 못 쓴 행을 dead-letter 표(usage.cost_dead_letters)에
        # 원본 그대로 남긴 수(B5). 0 이어야 정상 — 0 이 아니면 표를 보고 고쳐 다시 넣는다.
        self.dead_lettered = self._meter.create_counter(
            "cost_recorder_dead_letter_rows_total",
            description="Cost rows stored in usage.cost_dead_letters (labels: reason)",
            unit="1",
        )
        # dept_id 를 팀에서 해석한 행 수(B5) — VK 인증은 dept 를 모른다.
        self.dept_resolved = self._meter.create_counter(
            "cost_recorder_dept_resolved_total",
            description="Rows whose dept_id was resolved from the team (labels: outcome)",
            unit="1",
        )
        # 재가격(B7) — 단가가 생겨 비용을 채운 행 / 아직 단가가 없는 행.
        self.repriced = self._meter.create_counter(
            "cost_recorder_repriced_rows_total",
            description="pricing_missing rows repriced (labels: outcome)",
            unit="1",
        )
        # 요청 시작 원장(wave 3b, B2). outcome: swept(추정 행 기록) | raced(실제 행이 먼저 있었다) |
        # replaced(늦은 실제 완료가 추정 행을 교체) | undone(행 없이 끝난 요청의 추정 행 삭제) |
        # settle_failed(Redis 예약 정산 실패). swept 가 꾸준히 0 이 아니면 파드가 정상 종료하지
        # 못한다.
        self.request_sweeps = self._meter.create_counter(
            "cost_recorder_request_sweeps_total",
            description="Request-start sweeper outcomes (labels: outcome)",
            unit="1",
        )
        # 게이트웨이 DB 폴백(usage.cost_pending_events)에서 소화한 이벤트(B3). outcome: processed |
        # undecodable.
        self.pending_events = self._meter.create_counter(
            "cost_recorder_pending_events_total",
            description="Cost events drained from the DB fallback table (labels: outcome)",
            unit="1",
        )
        # 비용 대조(B8) — 마지막 실행의 검사별 불일치 수 + 실행 결과.
        self.reconciliation_runs = self._meter.create_counter(
            "cost_recorder_reconciliation_runs_total",
            description="Cost reconciliation runs (labels: status)",
            unit="1",
        )
        self._reconciliation_callbacks: list[Callable] = []
        self.reconciliation_mismatches = self._meter.create_observable_gauge(
            "cost_recorder_reconciliation_mismatches",
            callbacks=_fanout(self._reconciliation_callbacks),
            description="Mismatches found by the last reconciliation run (labels: check)",
            # 게이지 unit "1" 은 Prometheus 이름에 _ratio 가 붙는다(collector 변환) — 단위 없음.
            unit="",
        )

        # wave 3c — 알림용 신호. 검사별 불일치 누적(실행마다 그 실행의 불일치 수를 더한다)과
        # 마지막 성공 실행 시각(오류 없이 끝나 결과를 남긴 실행 — 불일치가 있어도 성공이다).
        # Prometheus 이름 그대로: reconciliation_mismatch_total{check},
        # reconciliation_last_success_timestamp (unix 초, 단위 접미사 없음).
        self.reconciliation_mismatch_total = self._meter.create_counter(
            "reconciliation_mismatch_total",
            description="Mismatches found by cost reconciliation runs, summed per check "
                        "(labels: check)",
            unit="1",
        )
        for check in RECONCILIATION_CHECKS:
            self.reconciliation_mismatch_total.add(0, {"check": check})
        self._reconciliation_success_callbacks: list[Callable] = []
        self.reconciliation_last_success = self._meter.create_observable_gauge(
            "reconciliation_last_success_timestamp",
            callbacks=_fanout(self._reconciliation_success_callbacks),
            description="Unix time of the last reconciliation run that finished without error",
            unit="",
        )

        # 롤업 잡(시간/야간/기동) — 실행 시간·단위(날짜/시간/월) 실패·성공 수·마지막 성공 시각.
        # 마지막 성공 시각이 2시간 넘게 멈추면 admin-api 는 그 시간들을 raw 로 읽는다(느려짐).
        self.rollup_run_duration = self._meter.create_histogram(
            "cost_recorder_rollup_run_duration_seconds",
            description="Duration of each rollup job run (labels: job, outcome)",
            unit="s",
        )
        self.rollup_failures = self._meter.create_counter(
            "cost_recorder_rollup_failures_total",
            description="Rollup units (day/hour/month) that failed and were skipped",
            unit="1",
        )
        self.rollup_units = self._meter.create_counter(
            "cost_recorder_rollup_units_total",
            description="Rollup units (day/hour) aggregated successfully",
            unit="1",
        )
        self._rollup_success_callbacks: list[Callable] = []
        self.rollup_last_success = self._meter.create_observable_gauge(
            "cost_recorder_rollup_last_success_timestamp_seconds",
            callbacks=_fanout(self._rollup_success_callbacks),
            description="Unix time of the last successful rollup job run (label: job)",
            unit="s",
        )

        # Observable gauge — 콜백은 StreamConsumer 가 등록한다(주기 조회 결과를 들고 있음).
        self._stream_lag_callbacks: list[Callable] = []
        self.stream_lag = self._meter.create_observable_gauge(
            "cost_recorder_stream_lag",
            callbacks=_fanout(self._stream_lag_callbacks),
            description="Unprocessed entries in the cost stream consumer group (lag)",
            unit="",
        )
        # PEL(unacked·배달됐지만 미처리) 크기 — lag 은 미배달+pending 합산이라
        # 미배달 백로그와 회수 대기를 구분하려면 별도 게이지가 필요하다.
        self._stream_pending_callbacks: list[Callable] = []
        self.stream_pending = self._meter.create_observable_gauge(
            "cost_recorder_stream_pending",
            callbacks=_fanout(self._stream_pending_callbacks),
            description="Pending (delivered but unacked) entries in the consumer group",
            unit="",
        )

    def register_stream_lag_callback(self, callback: Callable) -> None:
        self._stream_lag_callbacks.append(callback)

    def register_stream_pending_callback(self, callback: Callable) -> None:
        self._stream_pending_callbacks.append(callback)

    def register_reconciliation_callback(self, callback: Callable) -> None:
        self._reconciliation_callbacks.append(callback)

    def register_reconciliation_success_callback(self, callback: Callable) -> None:
        self._reconciliation_success_callbacks.append(callback)

    def register_rollup_success_callback(self, callback: Callable) -> None:
        self._rollup_success_callbacks.append(callback)
