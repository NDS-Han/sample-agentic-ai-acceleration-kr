# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""BodyLogStore — S3 lookup 단위 테스트 (boto3 mock, 네트워크 없음)."""

from __future__ import annotations

import gzip
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services.body_log_store import (
    BodyLogFetchResult,
    BodyLogLookupError,
    BodyLogStore,
    _key_timestamp,
    _partition_prefix,
)

REQ = "023c673b-0686-4794-b8ac-8d1317118e7a"
REQ_AT = datetime(2026, 11, 5, 7, 39, 25, tzinfo=timezone.utc)


def _gz(lines: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        for line in lines:
            g.write((json.dumps(line) + "\n").encode())
    return buf.getvalue()


def _fh_key(dt: datetime) -> str:
    return (
        f"provider=BEDROCK/client=other/dt={dt:%Y-%m-%d}/"
        f"llm-gateway-dev-body-logs-2-{dt:%Y-%m-%d-%H-%M-%S}-aaaaaaaa-0000.gz"
    )


class FakeClientError(Exception):
    """boto3 ClientError 와 같은 ``response['Error']['Code']`` 계약."""

    def __init__(self, code: str, msg: str = "x") -> None:
        super().__init__(msg)
        self.response = {"Error": {"Code": code}}


def _store() -> BodyLogStore:
    store = BodyLogStore.__new__(BodyLogStore)
    store.bucket = "bucket"
    store._s3 = MagicMock()
    # service 가 ``except self._s3.exceptions.ClientError`` 로 잡으므로
    # 실제 예외 클래스를 심어 준다.
    store._s3.exceptions.ClientError = FakeClientError
    return store


def _nf() -> FakeClientError:
    return FakeClientError("404")


def test_partition_prefix_and_key_ts() -> None:
    assert (
        _partition_prefix("BEDROCK", "other", "2026-11-05")
        == "provider=BEDROCK/client=other/dt=2026-11-05/"
    )
    # client NULL → proxy 와 같은 "other" 치환
    assert "client=other" in _partition_prefix("BEDROCK", None, "d")
    ts = _key_timestamp(_fh_key(REQ_AT))
    assert ts == REQ_AT
    assert _key_timestamp("provider=X/client=Y/dt=Z/no-ts.gz") is None


@pytest.mark.asyncio
async def test_fallback_json_hit() -> None:
    """fallback 결정적 키에 있으면 Firehose 스캔 없이 반환."""
    store = _store()
    rec = {"request_id": REQ, "request_body": {"messages": []}}
    store._s3.head_object.return_value = {"ContentLength": 100}
    store._s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(rec).encode())}

    result = await store.fetch(
        REQ, provider="BEDROCK", client="other", requested_at=REQ_AT, latency_ms=1000
    )
    assert result.record == rec
    assert result.source == "fallback"
    store._s3.get_paginator.assert_not_called()


@pytest.mark.asyncio
async def test_firehose_scan_hit_and_duplicates() -> None:
    """파티션 스캔 — 창 안 객체만 열고, 중복 레코드 수를 센다."""
    store = _store()
    # fallback 없음
    store._s3.head_object.side_effect = _nf()

    in_win = _fh_key(REQ_AT + timedelta(seconds=2))
    out_win = _fh_key(REQ_AT - timedelta(minutes=30))
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": in_win}, {"Key": out_win}]}
    ]
    store._s3.get_paginator.return_value = paginator

    rec = {"request_id": REQ, "response_body": {"sse_text": "x"}}
    body = io.BytesIO(_gz([{"request_id": "other"}, rec, dict(rec)]))
    store._s3.get_object.return_value = {"Body": body}

    result = await store.fetch(
        REQ, provider="BEDROCK", client="other", requested_at=REQ_AT, latency_ms=1000
    )
    assert result.record == rec
    assert result.source == "firehose"
    assert result.duplicates == 1
    # 창 밖 객체는 열지 않는다 — get_object 는 in_win 하나만 호출
    assert store._s3.get_object.call_count == 1


@pytest.mark.asyncio
async def test_midnight_partition_boundary() -> None:
    """자정 경계 — dt 두 파티션을 모두 본다."""
    store = _store()
    store._s3.head_object.side_effect = _nf()

    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    store._s3.get_paginator.return_value = paginator

    near_midnight = datetime(2026, 11, 5, 23, 59, 50, tzinfo=timezone.utc)
    await store.fetch(
        REQ, provider="BEDROCK", client=None, requested_at=near_midnight, latency_ms=500
    )
    prefixes = [c.kwargs["Prefix"] for c in paginator.paginate.call_args_list]
    assert "provider=BEDROCK/client=other/dt=2026-11-05/" in prefixes
    assert "provider=BEDROCK/client=other/dt=2026-11-06/" in prefixes


@pytest.mark.asyncio
async def test_oversized_fallback() -> None:
    store = _store()
    store._s3.head_object.return_value = {"ContentLength": 64 * 1024 * 1024}

    result = await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=REQ_AT, latency_ms=1
    )
    assert result.oversized is True
    assert result.record is None
    store._s3.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_prev_day_partition_clock_skew() -> None:
    """자정 직후 요청 — 시계 스큐로 레코드가 전날 파티션에 들어갔을 수 있으므로
    창이 전날로 넘어가면 dt=d-1 도 스캔한다."""
    store = _store()
    store._s3.head_object.side_effect = _nf()

    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    store._s3.get_paginator.return_value = paginator

    just_after = datetime(2026, 11, 5, 0, 1, 0, tzinfo=timezone.utc)
    await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=just_after, latency_ms=100
    )
    prefixes = [c.kwargs["Prefix"] for c in paginator.paginate.call_args_list]
    assert "provider=BEDROCK/client=o/dt=2026-11-04/" in prefixes
    assert "provider=BEDROCK/client=o/dt=2026-11-05/" in prefixes


@pytest.mark.asyncio
async def test_head_object_generic_error_wrapped() -> None:
    """BotoCoreError·연결 오류 등 ClientError 가 아닌 예외도 BodyLogLookupError
    로 감싼다 — 500/감사 누락 방지(R2)."""
    store = _store()
    store._s3.head_object.side_effect = ConnectionError("reset")

    with pytest.raises(BodyLogLookupError):
        await store.fetch(
            REQ, provider="BEDROCK", client="o",
            requested_at=REQ_AT, latency_ms=100,
        )


@pytest.mark.asyncio
async def test_head_object_non404_client_error_wrapped() -> None:
    store = _store()
    store._s3.head_object.side_effect = FakeClientError("500")

    with pytest.raises(BodyLogLookupError):
        await store.fetch(
            REQ, provider="BEDROCK", client="o",
            requested_at=REQ_AT, latency_ms=100,
        )


@pytest.mark.asyncio
async def test_read_timeout_is_lookup_error_not_partial() -> None:
    """botocore ReadTimeoutError 는 OSError 하위라 gzip-파손 분기로 빠지기
    쉽다 — 인프라 실패가 '일부 미스캔 404'로 오인되지 않도록 LookupError 여야 한다."""
    from botocore.exceptions import ReadTimeoutError

    store = _store()
    store._s3.head_object.side_effect = _nf()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": _fh_key(REQ_AT)}]}
    ]
    store._s3.get_paginator.return_value = paginator
    body = MagicMock()
    body.read.side_effect = ReadTimeoutError(
        endpoint_url="https://s3", error="timed out"
    )
    store._s3.get_object.return_value = {"Body": body}

    with pytest.raises(BodyLogLookupError):
        await store.fetch(
            REQ, provider="BEDROCK", client="o",
            requested_at=REQ_AT, latency_ms=100,
        )


@pytest.mark.asyncio
async def test_get_object_client_error_wrapped() -> None:
    store = _store()
    store._s3.head_object.side_effect = _nf()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": _fh_key(REQ_AT)}]}
    ]
    store._s3.get_paginator.return_value = paginator
    store._s3.get_object.side_effect = FakeClientError("AccessDenied")

    with pytest.raises(BodyLogLookupError):
        await store.fetch(
            REQ, provider="BEDROCK", client="o",
            requested_at=REQ_AT, latency_ms=100,
        )


@pytest.mark.asyncio
async def test_corrupt_gzip_marks_incomplete() -> None:
    """gzip 이 아닌/파손된 객체는 스캔 실패로 치지 않지만, 레코드가 안에
    있었을 수 있으므로 incomplete=True 로 남긴다(R3)."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": _fh_key(REQ_AT)}]}
    ]
    store._s3.get_paginator.return_value = paginator
    store._s3.get_object.return_value = {"Body": io.BytesIO(b"not a gzip stream")}

    result = await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=REQ_AT, latency_ms=100
    )
    assert result.record is None
    assert result.incomplete is True


@pytest.mark.asyncio
async def test_object_cap_marks_incomplete(monkeypatch) -> None:
    """파티션 객체 상한에 걸리면 뒤 객체를 안 본다 — incomplete."""
    import app.services.body_log_store as mod

    monkeypatch.setattr(mod, "_MAX_OBJECTS_PER_PARTITION", 1)
    store = _store()
    store._s3.head_object.side_effect = _nf()
    k1 = _fh_key(REQ_AT)
    k2 = _fh_key(REQ_AT + timedelta(seconds=3))
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": k1}, {"Key": k2}]}
    ]
    store._s3.get_paginator.return_value = paginator
    store._s3.get_object.return_value = {
        "Body": io.BytesIO(_gz([{"request_id": "other"}]))
    }

    result = await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=REQ_AT, latency_ms=100
    )
    assert result.incomplete is True
    # 첫 객체만 열고 끝난다
    assert store._s3.get_object.call_count == 1


class _TrackedBody(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.closed_flag = False

    def close(self) -> None:  # noqa: D102
        self.closed_flag = True
        super().close()


@pytest.mark.asyncio
async def test_response_body_closed() -> None:
    """스캔 후 S3 response body 는 반드시 close — 소켓 누수 방지."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": _fh_key(REQ_AT)}]}
    ]
    store._s3.get_paginator.return_value = paginator
    body = _TrackedBody(_gz([{"request_id": REQ}]))
    store._s3.get_object.return_value = {"Body": body}

    result = await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=REQ_AT, latency_ms=100
    )
    assert result.record is not None
    assert body.closed_flag is True


@pytest.mark.asyncio
async def test_not_found_clean() -> None:
    """어디에도 없으면 record=None, incomplete=False — '미수집' 404."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    store._s3.get_paginator.return_value = paginator

    result = await store.fetch(
        REQ, provider="BEDROCK", client="o", requested_at=REQ_AT, latency_ms=100
    )
    assert result.record is None
    assert result.incomplete is False
    assert result.source is None


# ── fetch_many — 세션 trace 다중 조회 ─────────────────────────────────────────

from app.services.body_log_store import BodyLogTarget  # noqa: E402

REQ2 = "4781da38-8565-4b37-a725-60475392f9f6"
REQ3 = "ecf65b98-faff-4dfd-abdb-8e730b64afc9"


def _target(rid: str, req_at: datetime = REQ_AT, lat: int = 1000) -> BodyLogTarget:
    return BodyLogTarget(
        request_id=rid,
        provider="BEDROCK",
        client="other",
        requested_at=req_at,
        latency_ms=lat,
    )


@pytest.mark.asyncio
async def test_fetch_many_same_object_one_open() -> None:
    """같은 .gz 에 있는 두 id — 객체를 한 번만 열어 둘 다 반환한다."""
    store = _store()
    store._s3.head_object.side_effect = _nf()

    in_win = _fh_key(REQ_AT + timedelta(seconds=2))
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": in_win}]}]
    store._s3.get_paginator.return_value = paginator

    rec1 = {"request_id": REQ, "v": 1}
    rec2 = {"request_id": REQ2, "v": 2}
    store._s3.get_object.return_value = {
        "Body": io.BytesIO(_gz([rec1, {"request_id": "other"}, rec2]))
    }

    result = await store.fetch_many([_target(REQ), _target(REQ2)])
    assert result.records[REQ] == rec1
    assert result.records[REQ2] == rec2
    assert result.missing == []
    # 다중 스캔의 핵심 — get_object 는 객체당 한 번
    assert store._s3.get_object.call_count == 1


@pytest.mark.asyncio
async def test_fetch_many_mixed_paths() -> None:
    """fallback + firehose + 미발견 혼합."""
    store = _store()
    rec_fallback = {"request_id": REQ3}

    def _head(Bucket, Key):
        if Key.endswith(f"{REQ3}.json"):
            return {"ContentLength": 50}
        raise _nf()

    store._s3.head_object.side_effect = _head
    in_win = _fh_key(REQ_AT + timedelta(seconds=2))
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": in_win}]}]
    store._s3.get_paginator.return_value = paginator
    rec1 = {"request_id": REQ}
    store._s3.get_object.side_effect = lambda Bucket, Key: {
        "Body": io.BytesIO(
            json.dumps(rec_fallback).encode()
            if Key.endswith(".json")
            else _gz([rec1])
        )
    }

    result = await store.fetch_many(
        [_target(REQ), _target(REQ2), _target(REQ3)]
    )
    assert result.records[REQ] == rec1
    assert result.sources[REQ] == "firehose"
    assert result.records[REQ3] == rec_fallback
    assert result.sources[REQ3] == "fallback"
    assert result.missing == [REQ2]


@pytest.mark.asyncio
async def test_fetch_many_dup_across_objects() -> None:
    """다른 타겟이 아직 미발견이면 스캔이 계속되고, 뒤 객체의 같은 id 는
    at-least-once 중복으로 센다(모두 찾으면 조기 종료라 그 뒤는 안 연다)."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    k1 = _fh_key(REQ_AT + timedelta(seconds=2))
    k2 = _fh_key(REQ_AT + timedelta(seconds=3))
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k1}, {"Key": k2}]}]
    store._s3.get_paginator.return_value = paginator
    rec = {"request_id": REQ}
    # 호출마다 새 버퍼 — 첫 스캔이 Body 를 close 하므로 재사용하면 깨진다
    store._s3.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(_gz([rec]))
    }

    result = await store.fetch_many([_target(REQ), _target(REQ2)])
    assert result.records[REQ] == rec
    assert result.duplicates[REQ] == 1
    assert result.missing == [REQ2]


@pytest.mark.asyncio
async def test_fetch_many_early_exit_when_all_found() -> None:
    """모든 id 를 찾으면 남은 후보 객체는 열지 않는다 — 비용 상한."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    k1 = _fh_key(REQ_AT + timedelta(seconds=2))
    k2 = _fh_key(REQ_AT + timedelta(seconds=3))
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k1}, {"Key": k2}]}]
    store._s3.get_paginator.return_value = paginator
    rec = {"request_id": REQ}
    store._s3.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(_gz([rec]))
    }

    result = await store.fetch_many([_target(REQ)])
    assert result.records[REQ] == rec
    # 두 번째 객체는 열지 않는다 — get_object 1회뿐
    assert store._s3.get_object.call_count == 1


@pytest.mark.asyncio
async def test_fetch_many_multi_scan_space_separated_rid() -> None:
    """JSON 직렬화가 '"request_id": "x"' 공백 형태여도 잡는다(단건 needle 과 동치)."""
    store = _store()
    store._s3.head_object.side_effect = _nf()
    in_win = _fh_key(REQ_AT + timedelta(seconds=2))
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": in_win}]}]
    store._s3.get_paginator.return_value = paginator

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        g.write(f'{{"request_id": "{REQ}", "v": 1}}\n'.encode())
    store._s3.get_object.return_value = {"Body": io.BytesIO(buf.getvalue())}

    result = await store.fetch_many([_target(REQ)])
    assert REQ in result.records
