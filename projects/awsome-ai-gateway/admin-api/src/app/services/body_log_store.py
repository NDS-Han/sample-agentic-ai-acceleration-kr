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
import time
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
# fetch_many 한 호출이 여는 객체 총량 상한 — 창별 16 상한만 두면 호출 수가
# 많은 세션(수십 창)에서 열리는 객체가 무한정 늘 수 있어 전역 마감을 둔다.
# 연속 세션은 창이 겹쳐 실제로는 창 수보다 훨씬 적게 연다.
_MAX_TOTAL_OBJECTS = 96
# 파티션 LIST 결과 상한 — 하루 Firehose 분할 주기(~1분)보다 넉넉하다.
_MAX_LIST_KEYS = 4096
#: gzip 을 풀며 읽는 총량 상한. Firehose 버퍼 기본 64MB 를 넉넉히 덮는다 — 줄 단위
#: 스트리밍이라 메모리는 한 줄(≤~1MB)뿐이고 늘어나는 건 시간뿐이다. 이 상한에
#: 걸리면 incomplete 로 표시해 "미수집" 오진을 막는다.
_MAX_DECOMPRESSED_BYTES = 96 * 1024 * 1024
#: fallback 단일 .json 객체 상한 — 초과면 본문 파싱 대신 oversized 로 답한다.
_MAX_FALLBACK_BYTES = 8 * 1024 * 1024
#: 한 조회 호출의 시간 예산. admin-ui 업스트림 타임아웃은 15s — 그보다 먼저
#: 멈춰 찾은 것까지만 돌려야 한다. 초과 시 incomplete=True 로 표시해 나머지는
#: 건별 재시도로 넘긴다. 이대로라도 클라이언트가 끊으면 서버는 스캔을 끝내고
#: "열람 성공" 감사를 남긴다 — 못 본 열람이 성공으로 기록되는 걸 막는 상한.
_FETCH_TIME_BUDGET_S = 10.0

#: JSONL 한 줄에서 request_id 를 뽑는 다중타겟 스캔용 — 유효 문자셋만 허용해
#: 다른 필드의 유사 문자열이 매칭되는 걸 막는다.
_RID_LINE_RE = re.compile(rb'"request_id"\s*:\s*"([A-Za-z0-9._-]{4,128})"')


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


@dataclass
class BodyLogTarget:
    """``fetch_many`` 의 조회 대상 하나 — usage 행에서 유도하는 조회 인자."""

    request_id: str
    provider: str
    client: str | None
    requested_at: datetime
    latency_ms: int | None


@dataclass
class BodyLogBatchResult:
    """다중 조회 결과 — id 별 record/source/key 와 못 찾은 id 목록."""

    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=dict)
    oversized: set[str] = field(default_factory=set)
    missing: list[str] = field(default_factory=list)
    incomplete: bool = False
    scanned_keys: list[str] = field(default_factory=list)


def _lookup_window(
    requested_at: datetime, latency_ms: int | None
) -> tuple[datetime, datetime]:
    """요청 창: [requested_at - skew, requested_at + latency + slack].

    키 타임스탬프가 이 창 밖인 객체는 열지 않는다 — 요청 전에 쓰인 객체는
    불가능하고, 요청 뒤로는 latency + Firehose buffer(60s) + 재시도/스큐
    여유면 충분하다.
    """
    req_utc = requested_at.astimezone(timezone.utc)
    return (
        req_utc - timedelta(seconds=_EARLY_SKEW_S),
        req_utc + timedelta(milliseconds=(latency_ms or 0), seconds=_LATE_SLACK_S),
    )


def _window_dates(win_start: datetime, win_end: datetime) -> list[str]:
    """창이 걸치는 UTC 날짜 목록 — dt= 파티션은 Firehose 수신 시각이라 proxy
    시계 스큐로 요청 당일 레코드가 전날 파티션에 들어갈 수 있고, 자정+버퍼
    지연이면 다음 날로 간다. {d-1 .. d+1} 이 자연히 커버된다."""
    dates: list[str] = []
    d = win_start.date()
    while d <= win_end.date():
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


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
        win_start, win_end = _lookup_window(requested_at, latency_ms)
        dates = _window_dates(win_start, win_end)
        deadline = time.monotonic() + _FETCH_TIME_BUDGET_S

        # 1) proxy S3 fallback — 결정적 키라 List 없이 GetObject 두 방이면 끝난다.
        for d in dates:
            if time.monotonic() > deadline:
                return BodyLogFetchResult(incomplete=True)
            key = f"{_partition_prefix(provider, client, d)}{request_id}.json"
            hit = await asyncio.to_thread(self._try_fallback_key, key)
            if hit is not None:
                return hit

        result = BodyLogFetchResult()
        for d in dates:
            if time.monotonic() > deadline:
                result.incomplete = True
                return result
            prefix = _partition_prefix(provider, client, d)
            keys, capped = await asyncio.to_thread(
                self._candidate_keys, prefix, win_start, win_end
            )
            result.incomplete = result.incomplete or capped
            result.scanned_keys.extend(keys)
            for key in keys:
                if time.monotonic() > deadline:
                    result.incomplete = True
                    return result
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

    async def fetch_many(self, targets: list[BodyLogTarget]) -> BodyLogBatchResult:
        """여러 request_id 를 한 번의 파티션 스캔으로 찾는다 — 세션 trace 용.

        같은 대화의 호출들은 시간적으로 붙어 있어 같은 몇 개의 Firehose 객체에
        들어가는 경우가 대부분이다 — 단건 ``fetch`` 를 N 번 돌리면 같은 .gz 를
        N 번 연다. 여기서는 fallback 을 id 별로 시도한 뒤, Firehose 스캔은
        (prefix → union 창) 으로 묶어 객체마다 **한 번만** 연다.
        """
        result = BodyLogBatchResult()
        remaining: dict[str, BodyLogTarget] = {t.request_id: t for t in targets}
        deadline = time.monotonic() + _FETCH_TIME_BUDGET_S
        expired = False

        # 1) fallback — id 별 결정적 키라 개별 GetObject 가 유일한 경로다.
        for rid, t in list(remaining.items()):
            if time.monotonic() > deadline:
                expired = True
                break
            win_start, win_end = _lookup_window(t.requested_at, t.latency_ms)
            for d in _window_dates(win_start, win_end):
                key = f"{_partition_prefix(t.provider, t.client, d)}{rid}.json"
                hit = await asyncio.to_thread(self._try_fallback_key, key)
                if hit is None:
                    continue
                if hit.record is not None:
                    result.records[rid] = hit.record
                elif hit.oversized:
                    result.oversized.add(rid)
                else:
                    continue
                result.sources[rid] = "fallback"
                result.keys[rid] = key
                del remaining[rid]
                break

        # 2) Firehose — 남은 id 들을 (provider/client/dt) prefix 로 묶고,
        #    prefix 당 LIST 는 한 번만 돌린 뒤 **타깃별 창마다** 후보 키를
        #    골라 각각 _MAX_OBJECTS_PER_PARTITION 상한을 준다. 창을 병합하면
        #    호출이 연속된 세션(창 간격 < 창 길이)이 하나의 긴 창이 돼서
        #    16객체 상한에 뒤쪽 호출이 통째로 걸린다 — 창별 상한 + 이미 연
        #    객체는 opened 집합으로 건너뛰는 방식이 정확하고 중복 스캔도 없다.
        jobs: dict[str, list[tuple[str, datetime, datetime]]] = {}
        for t in remaining.values():
            win_start, win_end = _lookup_window(t.requested_at, t.latency_ms)
            for d in _window_dates(win_start, win_end):
                prefix = _partition_prefix(t.provider, t.client, d)
                jobs.setdefault(prefix, []).append((t.request_id, win_start, win_end))

        # 스캔 대상은 firehose 경로의 전체 id 로 고정 — 이미 찾은 id 도 매칭해
        # 뒤 객체의 at-least-once 중복을 센다. 조기 종료는 remaining 기준.
        scan_ids = set(remaining)
        opened: set[str] = set()

        for prefix, win_jobs in jobs.items():
            if not remaining or len(opened) >= _MAX_TOTAL_OBJECTS or expired:
                break
            # 이 prefix 의 타깃이 전부 fallback 등으로 해결됐으면 LIST 도 생략.
            if not any(r in remaining for r, _, _ in win_jobs):
                continue
            all_keys, capped = await asyncio.to_thread(
                self._list_partition_keys, prefix, min(ws for _, ws, _ in win_jobs)
            )
            result.incomplete = result.incomplete or capped
            # 각 타깃 창은 자기 상한의 후보를 갖는다 — 창 순서는 요청 시각 순.
            for rid, win_start, win_end in sorted(win_jobs, key=lambda j: j[1]):
                if not remaining or len(opened) >= _MAX_TOTAL_OBJECTS or expired:
                    break
                if rid not in remaining:
                    continue
                candidates = [
                    key
                    for ts, key in all_keys
                    if win_start <= ts <= win_end and key not in opened
                ]
                if len(candidates) > _MAX_OBJECTS_PER_PARTITION:
                    result.incomplete = True
                    candidates = candidates[:_MAX_OBJECTS_PER_PARTITION]
                for key in candidates:
                    # 타깃을 찾으면 그 창의 나머지 후보는 열지 않는다 —
                    # 전역 객체 예산을 다른 타깃을 위해 남긴다. 시간 예산을
                    # 넘기면 incomplete 로 찾은 것까지만 돌린다.
                    if (
                        rid not in remaining
                        or not remaining
                        or len(opened) >= _MAX_TOTAL_OBJECTS
                        or (expired := time.monotonic() > deadline)
                    ):
                        break
                    opened.add(key)
                    result.scanned_keys.append(key)
                    found_map, capped = await asyncio.to_thread(
                        self._scan_gzip_object_multi, key, scan_ids
                    )
                    result.incomplete = result.incomplete or capped
                    for frid, (rec, dupes) in found_map.items():
                        if frid in result.records:
                            # 다른 객체에 같은 id — at-least-once 중복
                            result.duplicates[frid] = result.duplicates.get(frid, 0) + 1 + dupes
                        else:
                            result.records[frid] = rec
                            result.sources[frid] = "firehose"
                            result.keys[frid] = key
                            if dupes:
                                result.duplicates[frid] = dupes
                        remaining.pop(frid, None)

        if (len(opened) >= _MAX_TOTAL_OBJECTS or expired) and remaining:
            # 전역 객체 상한이나 시간 예산에 닿은 채 미해결 id 가 남았으면 미스캔 표시.
            result.incomplete = True

        result.missing = sorted(remaining)
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

    def _stream_key_prefix(self, prefix: str) -> str | None:
        """파티션 첫 .gz 키에서 스트림 이름 접두를 추출한다.

        Firehose 키는 ``{stream}-{ver}-{YYYY-MM-DD-HH-MM-SS}-{rand}.gz`` —
        시각 패턴 앞부분이 스트림 접두다. StartAfter 계산용으로만 쓰이며
        못 구하면(비표준 이름) None 을 돌려 전체 LIST 로 폴백한다.
        """
        try:
            resp = self._s3.list_objects_v2(
                Bucket=self.bucket, Prefix=prefix, MaxKeys=10
            )
            for obj in resp.get("Contents") or []:
                key = obj.get("Key", "")
                if not isinstance(key, str) or not key.endswith(".gz"):
                    continue
                base = key[len(prefix):] if key.startswith(prefix) else key
                m = re.match(
                    r"(.+)-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[^/]+\.gz$", base
                )
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _list_partition_keys(
        self, prefix: str, win_start: datetime | None = None
    ) -> tuple[list[tuple[datetime, str]], bool]:
        """(timestamp, key) 목록을 시간 오름차순으로 반환 + LIST 상한 도달 여부.

        fetch_many 는 prefix 당 LIST 를 한 번만 돌리고 창별로 후보를 골라
        쓴다. ``win_start`` 가 주어지면 스트림 접두를 추출해 StartAfter 부터
        읽는다 — 하루 수천 개 객체가 쌓이는 혼잡 파티션에서 상한에 뒤쪽
        키가 잘려 매번 incomplete 로 빠지는 것을 막는다.
        """
        try:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if win_start is not None:
                stream = self._stream_key_prefix(prefix)
                if stream is not None:
                    kwargs["StartAfter"] = (
                        f"{prefix}{stream}-{win_start:%Y-%m-%d-%H-%M-%S}"
                    )
            paginator = self._s3.get_paginator("list_objects_v2")
            pairs: list[tuple[datetime, str]] = []
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".gz"):
                        continue
                    ts = _key_timestamp(key)
                    if ts is not None:
                        pairs.append((ts, key))
                if len(pairs) > _MAX_LIST_KEYS:
                    return sorted(pairs)[:_MAX_LIST_KEYS], True
            return sorted(pairs), False
        except Exception as exc:
            raise BodyLogLookupError(
                f"list {prefix}: {type(exc).__name__}: {exc}"
            ) from exc

    def _candidate_keys(
        self, prefix: str, win_start: datetime, win_end: datetime
    ) -> tuple[list[str], bool]:
        """(창 안 후보 키 목록, 상한으로 잘렸는가)."""
        all_keys, capped = self._list_partition_keys(prefix, win_start)
        keys = [key for ts, key in all_keys if win_start <= ts <= win_end]
        if len(keys) > _MAX_OBJECTS_PER_PARTITION:
            return keys[:_MAX_OBJECTS_PER_PARTITION], True
        return keys, capped

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

    def _scan_gzip_object_multi(
        self, key: str, request_ids: set[str]
    ) -> tuple[dict[str, tuple[dict[str, Any], int]], bool]:
        """객체 한 번 열기로 여러 request_id 를 찾는다 — 세션 trace 용.

        반환은 (id → (첫 매칭 레코드, 같은 id 의 추가 레코드 수), 상한/파손으로
        잘렸는가). 단건 ``_scan_gzip_object`` 와 달리 히트해도 객체 끝까지 읽는다
        — 같은 객체에 세션의 다음 호출이 이어서 들어있는 게 흔하다.
        """
        found: dict[str, tuple[dict[str, Any], int]] = {}
        total = 0
        capped = False
        resp = None
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            with gzip.GzipFile(fileobj=resp["Body"]) as gz:
                for line in gz:
                    total += len(line)
                    if total > _MAX_DECOMPRESSED_BYTES:
                        logger.warning("body_log batch scan cap hit", key=key)
                        capped = True
                        break
                    if b'"request_id"' not in line:
                        continue
                    m = _RID_LINE_RE.search(line)
                    if not m:
                        continue
                    rid = m.group(1).decode()
                    if rid not in request_ids:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("request_id") != rid:
                        continue
                    if rid in found:
                        rec0, dupes = found[rid]
                        found[rid] = (rec0, dupes + 1)
                    else:
                        found[rid] = (rec, 0)
        except self._s3.exceptions.ClientError as exc:
            raise BodyLogLookupError(f"get {key}: {exc}") from exc
        except BotoCoreError as exc:
            # 단건 스캔과 같은 이유 — OSError 계열과 순서를 바꾸면 안 된다.
            raise BodyLogLookupError(
                f"get {key}: {type(exc).__name__}: {exc}"
            ) from exc
        except (OSError, EOFError, zlib.error) as exc:
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
        return found, capped
