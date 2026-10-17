# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Shared construction + gating for request/response body log records.

These lived as private helpers in ``routers/messages.py`` while the Anthropic Messages
route was the only one that logged bodies. ``/v1/responses`` (Codex → Mantle GPT-5.6)
now logs too, and the two routes must emit records of the SAME shape: an operator
reading back a Codex complaint queries the same stream, with the same fields, as for a
Claude Code complaint. Two independent copies of ``BodyLogRecord.make`` call sites would
drift — one would gain a field, or spell ``status`` differently, and the stored bodies
would silently stop being comparable across clients.

The gate lives here for the same reason. It is deliberately a *pre*-gate: ``enqueue()``
already no-ops when the logger is not ``enabled_effective``, but the streaming paths
must decide BEFORE the stream starts whether to accumulate the re-framed SSE text in
memory. Asking afterwards would mean buffering an entire response body for every
request just to throw it away.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog

from app.schemas.body_log import BodyLogRecord
from app.schemas.domain import DegradationLevel

logger = structlog.get_logger(__name__)

# 스트림 누적 버퍼 상한 — 로깅된 스트림은 재프레임된 SSE 전체를 capture 에
# 쌓는데, 장문 웹서치/사고 스트림이 동시에 많아지면 파드 메모리를 잡아먹는다.
# 문자 수 기준이다 — 멀티바이트 문자는 바이트로 더 크므로 logger 측
# max_record_bytes(900k)와 정확히 같지는 않고 대략 상응한다. 초과 레코드는
# truncated 플래그로 적재되고 초과분 자체는 살리지 못해도 S3 큐가 흡수한다.
# 클라이언트에게 가는 청크는 영향 없고 로그 레코드만 절단된다(R22 r1 S4,
# 문자/바이트 구분은 R22 r3 N4).
_MAX_ACCUMULATED_CHARS = 1_000_000


class BoundedCapture:
    """스트림 진단 텍스트의 상한 누적기 (R22 r2 S3).

    ``_MAX_ACCUMULATED_CHARS`` 까지만 보관하고 초과분은 버린다. cap 이후에도
    ``marker``(방언별 종결 프레임) 존재 여부는 조각 단위로 계속 스캔한다 —
    정상 완료된 장문 스트림이 절단 때문에 ``partial`` 로 오기록되는 것을 막는다.
    클라이언트에게 가는 바이트는 절대 건드리지 않는다 — 진단 버퍼만 상한이다.

    ``feed(bytes)`` 는 멀티바이트 경계 안전한 incremental 디코더를 쓰고,
    ``feed(str)`` 는 그대로 받는다(호출부가 이미 디코딩한 프레임).
    """

    def __init__(
        self,
        marker: str | None = None,
        cap: int = _MAX_ACCUMULATED_CHARS,
    ) -> None:
        self._parts: list[str] = []
        self._accumulated = 0
        self._cap = cap
        self._marker = marker
        self._seen_marker = False
        self._scan_tail = ""  # 표지가 조각 경계에 걸칠 때를 위한 꼬리
        self.truncated = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes | str) -> None:
        piece = chunk if isinstance(chunk, str) else self.decoder.decode(chunk)
        if self._marker and not self._seen_marker:
            hay = self._scan_tail + piece
            if self._marker in hay:
                self._seen_marker = True
            else:
                self._scan_tail = hay[-len(self._marker):]
        room = self._cap - self._accumulated
        if room <= 0:
            if piece:
                self.truncated = True
            return
        if len(piece) > room:
            self._parts.append(piece[:room])
            self._accumulated += room
            self.truncated = True
        else:
            self._parts.append(piece)
            self._accumulated += len(piece)

    def finish(self) -> None:
        """디코더 잔여 바이트를 플러시해 마지막 조각을 붙인다."""
        self.feed(self.decoder.decode(b"", final=True))

    @property
    def text(self) -> str:
        return "".join(self._parts)

    @property
    def seen_marker(self) -> bool:
        return self._seen_marker


def safe_json(raw: bytes) -> dict:
    """Parse for logging only — never raise.

    A body log is diagnostic; failing to parse one must not fail the request that
    produced it. Non-dict JSON is wrapped rather than returned as-is so the stored
    ``request_body`` / ``response_body`` are always objects.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except Exception:
        return {"_unparseable": True}


def build_body_record_for_nonstream(
    *,
    request_id: str,
    provider: str,
    client: str | None,
    model_alias: str,
    status_code: int,
    request_body: bytes,
    response_body: bytes,
    is_streaming: bool,
    user_id: str | None = None,
    team_id: str | None = None,
    sso_subject: str | None = None,
    bedrock_request_id: str | None = None,
) -> BodyLogRecord:
    resp = safe_json(response_body)
    is_ok = 200 <= status_code < 300
    error = None if is_ok else (resp.get("error") if isinstance(resp, dict) else None)
    return BodyLogRecord.make(
        request_id=request_id,
        provider=provider,
        client=client or "other",
        model_alias=model_alias,
        status="success" if is_ok else "error",
        is_streaming=is_streaming,
        request_body=safe_json(request_body),
        response_body=resp,
        user_id=user_id,
        team_id=team_id,
        sso_subject=sso_subject,
        bedrock_request_id=bedrock_request_id,
        error=error if error is not None else ({"message": "non-2xx"} if not is_ok else None),
    )


def build_body_record_for_stream(
    *,
    request_id: str,
    provider: str,
    client: str | None,
    model_alias: str,
    status: str,  # "success" | "partial"
    request_body: bytes,
    sse_text: str,
    truncated: bool = False,
    user_id: str | None = None,
    team_id: str | None = None,
    sso_subject: str | None = None,
    bedrock_request_id: str | None = None,
) -> BodyLogRecord:
    response_body: dict = {"sse_text": sse_text}
    if truncated:
        # 누적 버퍼 상한으로 sse_text 가 절단됨 — 조사 시 "본문이 여기서
        # 끝난 게 아니라 로그가 잘린 것" 을 구분할 수 있게 표지한다(R22 r2 S3).
        response_body["truncated"] = True
    return BodyLogRecord.make(
        request_id=request_id,
        provider=provider,
        client=client or "other",
        model_alias=model_alias,
        status="success" if status == "success" else "partial",
        is_streaming=True,
        request_body=safe_json(request_body),
        response_body=response_body,
        user_id=user_id,
        team_id=team_id,
        sso_subject=sso_subject,
        bedrock_request_id=bedrock_request_id,
        error=None if status == "success" else {"type": "stream_incomplete", "message": status},
    )


async def resolve_body_logger(app_state: Any, redis: Any, session_factory: Any) -> Any | None:
    """The BodyLogger to use for this request, or ``None`` when logging is off.

    Two independent switches, both of which must be on:

      * ``enabled_effective`` — static infra readiness (feature enabled AND a Firehose
        stream actually configured). Never changes at runtime.
      * ``body_log_flag`` — the dynamic admin toggle, read through a fast in-process
        cache backed by Redis/DB.

    The flag is only consulted when the logger is already effective, so turning the
    feature off at build time costs no Redis round-trip per request. A missing flag
    object means "no dynamic gate configured" → the static answer stands.
    """
    bl = getattr(app_state, "body_logger", None)
    if bl is None or not bl.enabled_effective:
        return None
    # DB 열화 시 플래그의 DB 재수화가 pool_timeout(기본 10s)까지 블로킹된다 —
    # 요청 경로에서 지연을 피하기 위해 DB 다운 시에는 세션을 넘기지 않는다
    # (Redis 히트는 여전히 읽히고, 미스면 flag 의 default 로 떨어진다).
    dm = getattr(app_state, "degradation_manager", None)
    if dm is not None and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    ):
        session_factory = None
    flag = getattr(app_state, "body_log_flag", None)
    if flag is not None and not await flag.is_enabled(redis, session_factory):
        return None
    return bl


def provider_name(model_config: Any) -> str:
    """``ProviderType`` → its wire string, tolerating an already-plain provider."""
    provider = model_config.provider
    return provider.value if hasattr(provider, "value") else str(provider)


#: 각 방언에서 스트림이 **정상 종료**했음을 뜻하는 종결 프레임.
#: 누적된 SSE 전문에 이 표지가 있으면 success, 없으면 partial 로 채점한다.
_TERMINAL_MARKERS = {
    "anthropic": "message_stop",
    "responses": "response.completed",
}

#: 클라이언트 끊김 경로에서 떼어낸 발화 태스크들의 강한 참조 집합(아래 사용처 주석 참조).
_PENDING_FIRES: set[asyncio.Task] = set()


def wrap_stream_for_body_log(
    gen: AsyncIterator[bytes],
    *,
    dialect: str,
    on_complete: Callable[[str, str, bool], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """SSE 제너레이터를 감싸 전문을 누적하고, 종료 시 ``on_complete`` 를 정확히 1회 부른다.

    왜 래퍼인가 — ``services/streaming.py`` 의 세 헬퍼는 이미 ``on_complete`` 를 직접
    받는다. 그런데 웹서치 루프의 본 경로(``_anthropic_stream`` / ``_responses_stream``)는
    그 헬퍼를 거치지 않고 **자기가 SSE 프레임을 합성해서** yield 한다. 그 두
    제너레이터에 훅을 심으려면 각각 200여 줄 안의 모든 exit 지점을 정확히 한 번씩
    맞춰야 하고(끼워 넣기 누락은 조용한 미기록, 중복은 이중 레코드), 웹서치 스티칭
    로직을 감사 기능이 건드리게 된다. 대신 밖에서 감싼다 — 어떤 경로로 끝나든 누적과
    발화가 한 곳에서 일어난다.

    ⚠️ 대가: 래퍼는 "왜 끝났는지" 를 모른다. SSE 헬퍼들은 클라이언트 연결 끊김을
       **return** 으로 처리하므로, 단순히 "제너레이터가 정상 종료했다" 를 success 로
       읽으면 잘린 스트림이 완전한 것으로 기록된다. 그래서 종료 방식이 아니라
       **누적된 내용**으로 채점한다 — 방언별 종결 프레임이 있으면 success. 이 판정은
       상류가 중간에 끊겼을 때도, 클라이언트가 먼저 끊었을 때도 옳다.

    누적은 ``on_complete`` 가 있을 때만 이 래퍼를 씌우는 호출자 책임이다(사전 게이팅).
    래퍼가 씌워졌다는 것 자체가 "로깅이 켜져 있다" 는 뜻이다.
    """
    marker = _TERMINAL_MARKERS.get(dialect)

    async def _wrapped() -> AsyncIterator[bytes]:
        capture = BoundedCapture(marker)
        fired = False

        async def _fire() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            # 채점은 누적 텍스트가 아니라 표지 스캔 결과로 — cap 을 넘긴
            # 정상 스트림이 partial 로 오기록되지 않는다(R22 r2 S3).
            status = "success" if marker and capture.seen_marker else "partial"
            try:
                await on_complete(capture.text, status, capture.truncated)
            except Exception:
                # 감사 sink 문제로 사용자의 스트림을 깨뜨리지 않는다.
                logger.exception("web_search_body_log_failed")

        def _detach_close_and_fire() -> None:
            # ⚠️ unwind 경로에서 yield 는 못 하고 await 도 재취소에 노출된다 —
            #    정산 체인을 태스크로 떼어 보낸다(`_fire` 는 로컬 capture 를
            #    닫고 있으므로 제너레이터가 사라져도 안전하다).
            # 내부 gen 도 명시적으로 닫는다 — 닫지 않으면 정산이 들어 있는 상류
            # 제너레이터의 finally 가 GC/asyncgen finalizer 시점까지 지연되고,
            # aclosing 의도("명시적으로 닫아 과금 누락 방지")가 로깅 경로에서만
            # 무력화된다.
            async def _close_then_fire() -> None:
                try:
                    await gen.aclose()
                finally:
                    await _fire()

            task = asyncio.ensure_future(_close_then_fire())
            # 태스크에 대한 강한 참조를 유지한다. 두지 않으면 GC 가 실행 전에 수거할 수 있고
            # 그러면 이 레코드만 조용히 사라진다.
            _PENDING_FIRES.add(task)
            task.add_done_callback(_PENDING_FIRES.discard)

        try:
            async for chunk in gen:
                capture.feed(chunk)
                yield chunk
            capture.finish()
        except GeneratorExit:
            _detach_close_and_fire()
            raise
        except asyncio.CancelledError:
            # CancelledError 는 BaseException — 아래 Exception 분기를 지나친다.
            # upstream 대기 중 취소되면 본문 레코드가 통째로 유실되므로 같은
            # detach 경로로 발화한다(R22 r3 N5).
            _detach_close_and_fire()
            raise
        except Exception:
            # 상류 실패로 스트림이 끊긴 경우. 이 경로를 빼면 실패한 요청의 본문이 사라지는데,
            # 조사에 필요한 것은 바로 그 본문이다. (여기는 GeneratorExit 이 아니라 await 가능.)
            await _fire()
            raise
        else:
            await _fire()

    return _wrapped()


def model_alias_of(model_config: Any) -> str:
    """The alias that served the request, falling back to the upstream model id.

    ``alias`` is nullable in the model config; a record whose ``model_alias`` came out
    empty would be unattributable, and this is the field the usage screens join on.
    """
    return model_config.alias or model_config.provider_model_id
