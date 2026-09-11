# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""InvocationLogService 검증 — 가짜 CloudWatch Logs client(실제 AWS 호출 없음).

여기서 지키려는 계약은 세 가지이고, 셋 다 **틀리면 감사 리포트가 거짓말을 한다**:

1. `bedrock_request_id IS NULL` 을 절대 `missing` 으로 세지 않는다. Mantle plane 은
   invocation log 를 아예 남기지 않으므로(live 실측) NULL 이 정상이다. 이걸 놓치면 codex
   호출마다 영원히 늑대를 부른다.
2. 잘린 결과를 전수처럼 보고하지 않는다(Logs Insights 10 000 상한, 표본 상한, row_limit).
3. 본문(프롬프트 원문)은 명시적으로 요청하지 않으면 응답에 실리지 않는다.

실 AWS 대상 증명은 `gateway-proxy/tests/integration/test_invocation_logging_live.py` 담당.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.invocation_log_service import (
    REASON_LEGACY,
    REASON_MANTLE_PLANE,
    REASON_NOT_BEDROCK,
    REASON_WEB_SEARCH,
    InvocationLogNotConfigured,
    InvocationLogQueryError,
    InvocationLogService,
    LogWindow,
    UsageRowRef,
    classify_null_request_id,
    padded_window,
    reconcile,
    redact_bodies,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(hours=1)


class _FakeLogsClient:
    """boto3 logs client 흉내 — start_query/get_query_results/stop_query 만.

    `statuses` 로 폴링 시나리오를, `results` 로 반환 행을 지정한다.
    """

    def __init__(
        self,
        results: list[list[dict[str, str]]] | None = None,
        *,
        statuses: list[str] | None = None,
        statistics: dict | None = None,
        start_query_error: Exception | None = None,
    ) -> None:
        self._results = results or []
        self._statuses = statuses or ["Complete"]
        self._statistics = statistics or {}
        self._start_query_error = start_query_error
        self.start_query_calls: list[dict] = []
        self.stop_query_calls: list[str] = []
        self.get_calls = 0

    def start_query(self, **kwargs):
        self.start_query_calls.append(kwargs)
        if self._start_query_error is not None:
            raise self._start_query_error
        return {"queryId": "q-1"}

    def get_query_results(self, **kwargs):
        self.get_calls += 1
        status = self._statuses[min(self.get_calls - 1, len(self._statuses) - 1)]
        payload = {"status": status, "statistics": self._statistics}
        if status == "Complete":
            payload["results"] = self._results
        return payload

    def stop_query(self, **kwargs):
        self.stop_query_calls.append(kwargs.get("queryId", ""))
        return {"success": True}


def _cells(**fields: str) -> list[dict[str, str]]:
    return [{"field": k, "value": v} for k, v in fields.items()]


def _row(
    request_id: str,
    bedrock_request_id: str | None,
    *,
    alias: str = "gpt-5.6-terra",
    provider: str = "BEDROCK_RUNTIME_OPENAI",
    web_search_count: int = 0,
    minutes_ago: int = 30,
) -> UsageRowRef:
    return UsageRowRef(
        request_id=request_id,
        bedrock_request_id=bedrock_request_id,
        model_alias=alias,
        provider=provider,
        requested_at=NOW - timedelta(minutes=minutes_ago),
        web_search_count=web_search_count,
    )


def _window(*ids: str, matched: int | None = None) -> LogWindow:
    """core 창 안쪽 timestamp 를 가진 로그 창."""
    ts = int((NOW - timedelta(minutes=30)).timestamp() * 1000)
    return LogWindow(
        request_ids=set(ids),
        timestamp_by_id={i: ts for i in ids},
        records_matched=matched if matched is not None else len(ids),
        records_returned=len(ids),
    )


# ── NULL 이유 분류 ────────────────────────────────────────────────────────────


class TestClassifyNullRequestId:
    def test_mantle_plane_providers_are_not_a_gap(self):
        for provider in ("BEDROCK_MANTLE", "BEDROCK_MANTLE_OPENAI"):
            assert classify_null_request_id(
                provider=provider, model_alias="codex-gpt-5.6-terra", web_search_count=0
            ) == REASON_MANTLE_PLANE

    def test_mantle_wins_over_web_search(self):
        """Mantle 은 웹서치를 했든 안 했든 로그 자체가 없다 — plane 판정이 먼저다."""
        assert classify_null_request_id(
            provider="BEDROCK_MANTLE_OPENAI", model_alias="codex-gpt-5.6-terra", web_search_count=3
        ) == REASON_MANTLE_PLANE

    def test_mantle_alias_caught_when_provider_is_blank(self):
        """provider 가 비어 있는 legacy 행에서도 alias 로 Mantle 을 잡는다."""
        assert classify_null_request_id(
            provider="", model_alias="codex-gpt-5.6-luna", web_search_count=0
        ) == REASON_MANTLE_PLANE

    def test_non_bedrock_provider(self):
        assert classify_null_request_id(
            provider="OPENMODEL", model_alias="qwen-32b", web_search_count=0
        ) == REASON_NOT_BEDROCK

    def test_web_search_multi_turn(self):
        """웹서치 행은 1 usage 행 = N invocation 이라 단일 id 로는 1:N 을 오보한다."""
        assert classify_null_request_id(
            provider="BEDROCK_RUNTIME_OPENAI", model_alias="gpt-5.6-terra", web_search_count=4
        ) == REASON_WEB_SEARCH

    def test_legacy_is_the_only_leftover(self):
        assert classify_null_request_id(
            provider="BEDROCK", model_alias="claude-sonnet-4-6", web_search_count=0
        ) == REASON_LEGACY

    def test_provider_case_and_whitespace_are_normalized(self):
        assert classify_null_request_id(
            provider="  bedrock_mantle_openai ", model_alias="x", web_search_count=0
        ) == REASON_MANTLE_PLANE


# ── reconcile (순수 함수) ─────────────────────────────────────────────────────


class TestReconcile:
    def _report(self, rows, window, **kw):
        return reconcile(
            rows,
            window,
            window_start=WINDOW_START,
            window_end=NOW,
            log_group="/aws/bedrock/modelinvocations",
            region="us-east-2",
            core_start=WINDOW_START,
            core_end=NOW,
            **kw,
        )

    def test_matched_rows_are_not_reported(self):
        report = self._report([_row("r1", "aws-1")], _window("aws-1"))
        assert report.matched == 1
        assert report.missing_count == 0
        assert report.orphan_count == 0

    def test_billed_row_without_a_log_record_is_the_alarm(self):
        report = self._report([_row("r1", "aws-1")], _window())
        assert report.missing_count == 1
        assert report.missing[0]["bedrock_request_id"] == "aws-1"
        assert report.missing[0]["model_alias"] == "gpt-5.6-terra"

    def test_null_ids_never_land_in_missing(self):
        """이 테스트가 이 파일의 존재 이유다 — Mantle NULL 을 missing 으로 세면 안 된다."""
        rows = [
            _row("r1", None, alias="codex-gpt-5.6-terra", provider="BEDROCK_MANTLE_OPENAI"),
            _row("r2", None, provider="BEDROCK_RUNTIME_OPENAI", web_search_count=2),
            _row("r3", None, provider="BEDROCK", alias="claude-sonnet-4-6"),
        ]
        report = self._report(rows, _window())
        assert report.missing_count == 0
        assert report.matched == 0
        assert report.skipped_null == {
            REASON_MANTLE_PLANE: 1,
            REASON_WEB_SEARCH: 1,
            REASON_LEGACY: 1,
        }
        assert report.as_dict()["skipped_null_total"] == 3

    def test_orphan_inside_core_window_is_reported_with_a_caveat(self):
        report = self._report([], _window("aws-orphan"))
        assert report.orphan_count == 1
        assert report.orphan_log_ids == ["aws-orphan"]
        assert any("계정 전체" in n for n in report.notes)

    def test_orphan_in_the_padding_is_not_counted(self):
        """패딩 구간 레코드는 창 밖 usage 행의 것일 수 있다 — orphan 으로 세면 거짓양성."""
        outside = int((NOW + timedelta(minutes=5)).timestamp() * 1000)
        window = LogWindow(
            request_ids={"aws-late"},
            timestamp_by_id={"aws-late": outside},
            records_matched=1,
            records_returned=1,
        )
        report = self._report([], window)
        assert report.orphan_count == 0

    def test_missing_sample_is_capped_but_count_is_exact(self):
        rows = [_row(f"r{i}", f"aws-{i}") for i in range(7)]
        report = self._report(rows, _window(), max_samples=3)
        assert report.missing_count == 7
        assert len(report.missing) == 3
        payload = report.as_dict()
        assert payload["missing_count"] == 7
        assert payload["missing_sample_truncated"] is True
        assert any("잘렸습니다" in n for n in report.notes)

    def test_log_truncation_is_surfaced(self):
        window = _window("aws-1", matched=99_999)
        report = self._report([_row("r1", "aws-1")], window)
        assert report.log_truncated is True
        assert any("신뢰하지 말고" in n for n in report.notes)

    def test_duplicate_bedrock_ids_do_not_create_a_phantom_orphan(self):
        """같은 invocation 에 붙은 usage 행이 둘이어도 그 로그 id 는 orphan 이 아니다."""
        rows = [_row("r1", "aws-1"), _row("r2", "aws-1")]
        report = self._report(rows, _window("aws-1"))
        assert report.matched == 2
        assert report.orphan_count == 0


# ── AWS I/O (가짜 client) ─────────────────────────────────────────────────────


class TestFetchRequestIds:
    async def test_collects_ids_models_and_timestamps(self):
        client = _FakeLogsClient(
            [
                _cells(**{
                    "@timestamp": "2026-09-03 11:30:00.000",
                    "requestId": "aws-1",
                    "modelId": "us.openai.gpt-5.6-terra",
                }),
                _cells(**{
                    "@timestamp": "2026-09-03 11:31:00.000",
                    "requestId": "aws-2",
                    "modelId": "us.openai.gpt-5.6-sol",
                }),
            ],
            statistics={"recordsMatched": 2},
        )
        svc = InvocationLogService(client, log_group="/g", region="us-east-2")
        window = await svc.fetch_request_ids(start=WINDOW_START, end=NOW)
        assert window.request_ids == {"aws-1", "aws-2"}
        assert window.model_by_id["aws-1"] == "us.openai.gpt-5.6-terra"
        assert window.timestamp_by_id["aws-1"] == int(
            datetime(2026, 9, 3, 11, 30, tzinfo=timezone.utc).timestamp() * 1000
        )
        assert window.truncated is False

    async def test_truncation_flag_compares_returned_rows_not_unique_ids(self):
        """같은 id 가 여러 레코드로 나올 수 있으니 set 크기로 비교하면 거짓 truncation."""
        client = _FakeLogsClient(
            [_cells(requestId="aws-1"), _cells(requestId="aws-1")],
            statistics={"recordsMatched": 2},
        )
        svc = InvocationLogService(client, log_group="/g")
        window = await svc.fetch_request_ids(start=WINDOW_START, end=NOW)
        assert window.request_ids == {"aws-1"}
        assert window.records_returned == 2
        assert window.truncated is False

    async def test_truncation_detected_when_matched_exceeds_returned(self):
        client = _FakeLogsClient([_cells(requestId="aws-1")], statistics={"recordsMatched": 50_000})
        svc = InvocationLogService(client, log_group="/g")
        window = await svc.fetch_request_ids(start=WINDOW_START, end=NOW)
        assert window.truncated is True

    async def test_limit_is_clamped_to_the_insights_ceiling(self):
        """10 000 초과를 그대로 보내면 API 가 거부한다 — 우리가 먼저 깎는다."""
        client = _FakeLogsClient([], statistics={"recordsMatched": 0})
        svc = InvocationLogService(client, log_group="/g", max_records=999_999)
        await svc.fetch_request_ids(start=WINDOW_START, end=NOW)
        assert client.start_query_calls[0]["limit"] == 10_000

    async def test_empty_log_group_is_not_configured_not_a_query_error(self):
        svc = InvocationLogService(_FakeLogsClient(), log_group="")
        with pytest.raises(InvocationLogNotConfigured):
            await svc.fetch_request_ids(start=WINDOW_START, end=NOW)

    async def test_missing_log_group_maps_to_not_configured(self):
        err = Exception("nope")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}
        svc = InvocationLogService(_FakeLogsClient(start_query_error=err), log_group="/g")
        with pytest.raises(InvocationLogNotConfigured):
            await svc.fetch_request_ids(start=WINDOW_START, end=NOW)

    async def test_failed_query_raises_instead_of_returning_partial(self):
        client = _FakeLogsClient([], statuses=["Running", "Failed"])
        svc = InvocationLogService(client, log_group="/g", poll_interval_s=0)
        with pytest.raises(InvocationLogQueryError):
            await svc.fetch_request_ids(start=WINDOW_START, end=NOW)

    async def test_timeout_cancels_the_query_and_raises(self):
        """잘린 결과로 missing 을 판정하면 거짓양성 — 부분결과를 성공으로 위장하지 않는다."""
        client = _FakeLogsClient([], statuses=["Running"])
        svc = InvocationLogService(client, log_group="/g", query_timeout_s=0, poll_interval_s=0)
        with pytest.raises(InvocationLogQueryError):
            await svc.fetch_request_ids(start=WINDOW_START, end=NOW)
        assert client.stop_query_calls == ["q-1"]


class TestFetchRecord:
    _RECORD = {
        "schemaType": "ModelInvocationLog",
        "requestId": "aws-1",
        "modelId": "us.openai.gpt-5.6-terra",
        "identity": {"arn": "arn:aws:sts::123456789012:assumed-role/gw/pod"},
        "input": {"inputTokenCount": 12, "inputBodyJson": {"messages": [{"content": "비밀"}]}},
        "output": {"outputTokenCount": 3, "outputBodyJson": {"output": "답"}},
    }

    def _client(self):
        return _FakeLogsClient(
            [_cells(**{"@timestamp": "2026-09-03 11:30:00.000", "@message": json.dumps(self._RECORD)})]
        )

    async def test_bodies_are_redacted_by_default(self):
        svc = InvocationLogService(self._client(), log_group="/g")
        got = await svc.fetch_record("aws-1", start=WINDOW_START, end=NOW)
        assert got["bodies_included"] is False
        record = got["records"][0]
        assert "inputBodyJson" not in record["input"]
        assert record["input"]["_body_redacted"] is True
        assert record["input"]["inputTokenCount"] == 12  # 메타데이터는 남는다
        assert "비밀" not in json.dumps(got, ensure_ascii=False)

    async def test_bodies_included_when_explicitly_asked(self):
        svc = InvocationLogService(self._client(), log_group="/g")
        got = await svc.fetch_record("aws-1", start=WINDOW_START, end=NOW, include_bodies=True)
        assert got["bodies_included"] is True
        assert got["records"][0]["input"]["inputBodyJson"]["messages"][0]["content"] == "비밀"

    async def test_absent_record_is_none_not_an_error(self):
        svc = InvocationLogService(_FakeLogsClient([]), log_group="/g")
        assert await svc.fetch_record("aws-x", start=WINDOW_START, end=NOW) is None

    @pytest.mark.parametrize("bad", ["a' | fields @message", "aws 1", "", "x" * 129])
    async def test_request_id_is_validated_before_query_interpolation(self, bad):
        """id 는 쿼리 문자열에 그대로 들어간다 — 형식 검증이 주입 방어선이다."""
        client = _FakeLogsClient([])
        svc = InvocationLogService(client, log_group="/g")
        with pytest.raises(ValueError):
            await svc.fetch_record(bad, start=WINDOW_START, end=NOW)
        assert client.start_query_calls == []

    async def test_s3_body_pointer_is_surfaced(self):
        """큰 본문은 sidecar S3 로 간다 — 포인터를 안 보여주면 '본문 없음' 으로 오해한다."""
        record = {
            "requestId": "aws-1",
            "input": {"inputBodyS3Path": "s3://bkt/2026/09/03/aws-1-in.json"},
            "output": {"outputBodyS3Path": "s3://bkt/2026/09/03/aws-1-out.json"},
        }
        client = _FakeLogsClient([_cells(**{"@message": json.dumps(record)})])
        svc = InvocationLogService(client, log_group="/g")
        got = await svc.fetch_record("aws-1", start=WINDOW_START, end=NOW)
        assert got["s3_body_pointers"] == [
            "s3://bkt/2026/09/03/aws-1-in.json",
            "s3://bkt/2026/09/03/aws-1-out.json",
        ]
        # 포인터는 본문이 아니라 좌표 — 마스킹 대상이 아니다.
        assert got["records"][0]["input"]["inputBodyS3Path"].startswith("s3://")

    async def test_no_s3_pointer_yields_an_empty_list(self):
        svc = InvocationLogService(self._client(), log_group="/g")
        got = await svc.fetch_record("aws-1", start=WINDOW_START, end=NOW)
        assert got["s3_body_pointers"] == []

    async def test_unparsable_message_is_surfaced_not_swallowed(self):
        """파싱 실패를 조용히 버리면 '레코드 없음' 과 구분이 안 된다."""
        client = _FakeLogsClient([_cells(**{"@message": "{not json"})])
        svc = InvocationLogService(client, log_group="/g")
        got = await svc.fetch_record("aws-1", start=WINDOW_START, end=NOW)
        assert got["record_count"] == 1
        assert got["records"][0]["_unparsed"].startswith("{not json")


# ── 보조 함수 ────────────────────────────────────────────────────────────────


def test_redact_bodies_does_not_mutate_the_input():
    original = {"input": {"inputBodyJson": {"a": 1}, "inputTokenCount": 5}}
    redact_bodies(original)
    assert original["input"]["inputBodyJson"] == {"a": 1}


def test_redact_bodies_marks_absence_of_bodies_too():
    """본문 키가 없던 레코드에 `_body_redacted=True` 를 붙이면 거짓 신호가 된다."""
    out = redact_bodies({"input": {"inputTokenCount": 5}})
    assert out["input"]["_body_redacted"] is False


def test_padded_window_widens_both_ends():
    """usage.requested_at 과 로그 timestamp 는 같지 않다 — 패딩 없으면 경계가 missing 이 된다."""
    start, end = padded_window(WINDOW_START, NOW, pad_minutes=10)
    assert start == WINDOW_START - timedelta(minutes=10)
    assert end == NOW + timedelta(minutes=10)
