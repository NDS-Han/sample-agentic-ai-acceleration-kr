# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""S3 body-log lookup — usage row 의 partition 정보로 레코드 1건을 찾는다.

레이아웃(gateway-proxy ``BodyLogRecord.partition_key()`` / Firehose prefix 와 동일)::

    provider={provider}/client={client|other}/dt={YYYY-MM-DD}/

두 계통의 객체가 같은 prefix 아래 섞인다:

* Firehose 배달 객체 — ``{stream}-{ver}-{YYYY}-{MM}-{dd}-{HH}-{mm}-{ss}-{rand}.gz``.
  키의 시각은 **전송 시각**(UTC). gzip JSONL, 한 객체에 다수 레코드.
* proxy S3 fallback — ``{partition_key}/{request_id}.json``. 무압축 단일 레코드,
  oversized(>1MB) 레코드용 경로라 최대 128MiB 까지 갈 수 있다.

조회 순서는 fallback 결정적 GetObject 먼저(키를 정확히 알 수 있으므로), 없으면
파티션을 List 해서 키 타임스탬프가 요청 시각 창 안인 객체만 스트리밍 스캔한다.
``dt`` 는 Firehose 도착 시각 기준이라 자정 경계에서는 다음 날 파티션에 들어갈 수
있어 항상 {d, d+1} 두 파티션을 본다.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from botocore.exceptions import BotoCoreError

logger = structlog.get_logger()

#: Firehose 객체명에 박힌 UTC 시각: `...-YYYY-MM-DD-HH-MM-SS-<rand>.gz`
_KEY_TS_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-[^/]+$")

#: 키 타임스탬프가 이 창 밖이면 스캔하지 않는다. 요청 전에 쓰인 객체는 불가능하고,
#: 요청 뒤로는 latency + Firehose buffer(60s) + 재시도/스큐 여유면 충분하다.
_EARLY_SKEW_S = 120
_LATE_SLACK_S = 300

#: 한 파티션 스캔에서 여는 객체 상한 — 창을 좁혀도 이례적 버스트는 있을 수 있다.
_MAX_OBJECTS_PER_PARTITION = 16
#: gzip 을 풀며 읽는 총량 상한. Firehose 버퍼 기본 64MB 를 넉넉히 덮는다 — 줄 단위
#: 스트리밍이라 메모리는 한 줄(≤~1MB)뿐이고 늘어나는 건 시간뿐이다. 이 상한에
#: 걸리면 incomplete 로 표시해 "미수집" 오진을 막는다.
_MAX_DECOMPRESSED_BYTES = 96 * 1024 * 1024
#: fallback 단일 .json 객체 상한 — 초과면 본문 파싱 대신 oversized 로 답한다.
_MAX_FALLBACK_BYTES = 8 * 1024 * 1024


class BodyLogNotConfigured(RuntimeError):
    """BODY_LOG_S3_BUCKET 미설정 — 엔드포인트는 503으로 번역한다."""


class BodyLogLookupError(RuntimeError):
    """S3 호출 자체가 실패 — 엔드포인트는 502로 번역한다."""


#: 요청마다 boto3 클라이언트를 만들지 않도록 모듈 단위로 캐시한다(동기 생성 비용).
_clients: dict[str, Any] = {}


def _s3_client(region: str | None) -> Any:
    key = region or ""
    if key not in _clients:
        import boto3  # noqa: PLC0415 — 요청 시점에만 필요

        _clients[key] = (
            boto3.client("s3", region_name=region) if region else boto3.client("s3")
        )
    return _clients[key]


@dataclass
class BodyLogFetchResult:
    record: dict[str, Any] | None = None
    source: str | None = None  # "firehose" | "fallback"
    key: str | None = None
    duplicates: int = 0
    oversized: bool = False
    #: 상한(객체 수·디압축 바이트)에 걸려 스캔을 다 못 했으면 True —
    #: record=None + incomplete=True 는 "미수집"이 아니라 "못 찾았을 수 있음".
    incomplete: bool = False
    scanned_keys: list[str] = field(default_factory=list)


def _partition_prefix(provider: str, client: str | None, date_str: str) -> str:
    return f"provider={provider}/client={client or 'other'}/dt={date_str}/"


def _key_timestamp(key: str) -> datetime | None:
    m = _KEY_TS_RE.search(key)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()[:6]), tzinfo=timezone.utc)
    except ValueError:
        return None


class BodyLogStore:
    """boto3 s3 클라이언트 래퍼 — 동기 호출을 to_thread 로 감싼다."""

    def __init__(self, bucket: str, region: str | None = None) -> None:
        if not bucket:
            raise BodyLogNotConfigured(
                "BODY_LOG_S3_BUCKET 이 비어 있습니다 — body_logging 인프라가 "
                "배포되지 않았거나 env 주입이 빠졌습니다"
            )
        self.bucket = bucket
        self._s3 = _s3_client(region)

    async def fetch(
        self,
        request_id: str,
        *,
        provider: str,
        client: str | None,
        requested_at: datetime,
        latency_ms: int | None,
    ) -> BodyLogFetchResult:
        """request_id 에 대응하는 BodyLogRecord 를 찾는다. 없으면 record=None."""
        req_utc = requested_at.astimezone(timezone.utc)

        # 2) Firehose 파티션 스캔 — 키 타임스탬프가 요청 창 안인 객체만 연다.
        win_start = req_utc - timedelta(seconds=_EARLY_SKEW_S)
        win_end = req_utc + timedelta(
            milliseconds=(latency_ms or 0), seconds=_LATE_SLACK_S
        )
        # 파티션 날짜는 창에서 유도한다: dt= 는 Firehose 수신 시각이라 proxy
        # 시계 스큐(_EARLY_SKEW_S 안)로 요청 당일 레코드가 전날 파티션에 들어갈
        # 수 있고, 자정+버퍼 지연이면 다음 날로 간다. {d-1 .. d+1} 이 자연히 커버된다.
        dates: list[str] = []
        d = win_start.date()
        while d <= win_end.date():
            dates.append(d.isoformat())
            d += timedelta(days=1)

        # 1) proxy S3 fallback — 결정적 키라 List 없이 GetObject 두 방이면 끝난다.
        for d in dates:
            key = f"{_partition_prefix(provider, client, d)}{request_id}.json"
            hit = await asyncio.to_thread(self._try_fallback_key, key)
            if hit is not None:
                return hit

        result = BodyLogFetchResult()
        for d in dates:
            prefix = _partition_prefix(provider, client, d)
            keys, capped = await asyncio.to_thread(
                self._candidate_keys, prefix, win_start, win_end
            )
            result.incomplete = result.incomplete or capped
            result.scanned_keys.extend(keys)
            for key in keys:
                found, dupes, capped = await asyncio.to_thread(
                    self._scan_gzip_object, key, request_id
                )
                result.incomplete = result.incomplete or capped
                if found is not None:
                    result.record = found
                    result.source = "firehose"
                    result.key = key
                    result.duplicates += dupes
                    return result
        return result

    # ── sync helpers (run via to_thread) ──────────────────────────────────────

    def _try_fallback_key(self, key: str) -> BodyLogFetchResult | None:
        try:
            head = self._s3.head_object(Bucket=self.bucket, Key=key)
        except self._s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise BodyLogLookupError(f"head_object {key}: {exc}") from exc
        except Exception as exc:
            # BotoCoreError(연결/자격증명/타임아웃) 등 — "모르면 실패"로 흘려보내
            # 감사 계약(시도는 무조건 기록)이 깨지는 500 을 막는다.
            raise BodyLogLookupError(
                f"head_object {key}: {type(exc).__name__}: {exc}"
            ) from exc

        size = head.get("ContentLength", 0)
        result = BodyLogFetchResult(source="fallback", key=key)
        if size > _MAX_FALLBACK_BYTES:
            # oversized 단일 레코드 — 통째로 읽지 않고 있음만 알린다.
            result.oversized = True
            return result

        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            result.record = json.loads(body)
        except Exception as exc:
            raise BodyLogLookupError(f"fallback read {key}: {exc}") from exc
        return result

    def _candidate_keys(
        self, prefix: str, win_start: datetime, win_end: datetime
    ) -> tuple[list[str], bool]:
        """(창 안 후보 키 목록, 상한으로 잘렸는가)."""
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".gz"):
                        continue
                    ts = _key_timestamp(key)
                    if ts is not None and win_start <= ts <= win_end:
                        keys.append(key)
                    if len(keys) >= _MAX_OBJECTS_PER_PARTITION:
                        return keys, True
            return keys, False
        except Exception as exc:
            raise BodyLogLookupError(
                f"list {prefix}: {type(exc).__name__}: {exc}"
            ) from exc

    def _scan_gzip_object(
        self, key: str, request_id: str
    ) -> tuple[dict[str, Any] | None, int, bool]:
        """gzip JSONL 을 스트리밍하며 request_id 행을 찾는다.

        반환은 (매칭 레코드 또는 None, 같은 request_id 의 추가 레코드 수, 스캔이
        상한/파손으로 잘렸는가) — Firehose 는 at-least-once 라 같은 레코드가
        중복 적재될 수 있고, 잘렸으면 호출부는 "미수집"과 구분해 표시한다.
        """
        needle = f'"request_id":"{request_id}"'.encode()
        needle_spaced = f'"request_id": "{request_id}"'.encode()
        found: dict[str, Any] | None = None
        dupes = 0
        total = 0
        capped = False
        resp = None
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            with gzip.GzipFile(fileobj=resp["Body"]) as gz:
                for line in gz:
                    total += len(line)
                    if total > _MAX_DECOMPRESSED_BYTES:
                        logger.warning("body_log scan cap hit", key=key)
                        capped = True
                        break
                    if needle not in line and needle_spaced not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("request_id") != request_id:
                        continue
                    if found is None:
                        found = rec
                    else:
                        dupes += 1
        except self._s3.exceptions.ClientError as exc:
            raise BodyLogLookupError(f"get {key}: {exc}") from exc
        except BotoCoreError as exc:
            # ReadTimeoutError·EndpointConnectionError 등 — botocore 의
            # ReadTimeoutError 는 OSError 도 상속하므로, 아래 gzip-파손 분기보다
            # **먼저** 잡아야 인프라 실패가 "일부 미스캔 404"로 오인되지 않는다.
            raise BodyLogLookupError(
                f"get {key}: {type(exc).__name__}: {exc}"
            ) from exc
        except (OSError, EOFError, zlib.error) as exc:
            # gzip 파손/비gzip 객체는 스캔 실패로 치지 않고 건너뛴다 — 단 레코드가
            # 안에 있었을 수 있으므로 incomplete 표시는 남긴다.
            logger.warning("body_log gzip unreadable", key=key, error=str(exc))
            capped = True
        except Exception as exc:
            raise BodyLogLookupError(
                f"get {key}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if resp is not None:
                try:
                    resp["Body"].close()
                except Exception:
                    pass
        return found, dupes, capped
