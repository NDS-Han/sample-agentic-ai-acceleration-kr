# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import codecs
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from starlette.requests import Request

from app.config import get_settings
from app.providers.bedrock_adapter import _redact_arns
from app.providers.openai_usage import extract_chat_usage, extract_responses_usage
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

# 2번째 인자 = 첫 콘텐츠 델타의 time.monotonic() (미검출 시 None)
OnUsage = Callable[[TokenUsage, float | None], Awaitable[None]] | None
# KI-08 tokenizer fallback: 누적된 output 텍스트를 받아 토큰 수를 역산.
# None 반환 시 추정 불가 (cost_recorder는 0 토큰 + estimated=True로 기록).
TokenizerHook = Callable[[str], Awaitable[int | None]] | None

#: 스트림 종료 훅 — ``(누적된 원본 SSE 프레임 전문, status)``.
#: status 는 ``"success"`` 또는 ``"partial"``(disconnect / timeout / 중간 예외).
#:
#: ⚠️ 이 훅이 설정되면 제너레이터가 **모든 프레임을 메모리에 누적한다.** 동시 스트림 수 ×
#:    응답 크기만큼 메모리를 쓴다. 그래서 훅이 None 이면 한 바이트도 쌓지 않는다 —
#:    본문 로깅이 꺼진 상태의 비용이 0 이어야 한다.
OnComplete = Callable[[str, str], Awaitable[None]] | None


# fire-and-forget 백그라운드 태스크의 강한 참조 집합. asyncio 는 create_task
# 결과를 약한 참조로만 잡으므로, 버려진 태스크는 GC 되어 조용히 죽을 수 있다 —
# disconnect 드레인이 죽으면 그 스트림의 finalize(과금)가 유실된다.
# shutdown drain 에서도 이 집합을 await 한다(스트림 과금 손실 방지).
_PENDING_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _PENDING_TASKS.add(t)
    t.add_done_callback(_PENDING_TASKS.discard)
    return t


async def drain_pending_tasks(timeout: float) -> int:
    """아직 안 끝난 백그라운드 태스크(스트림 드레인)를 timeout 안에서 기다린다.
    shutdown 순서상 Redis/httpx 를 닫기 **전에** 호출해야 한다."""
    pending = [t for t in _PENDING_TASKS if not t.done()]
    if not pending:
        return 0
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        logger.warning("drain_pending_tasks_timeout", remaining=len(pending))
    return len(pending)


def usage_status_of(outcome: dict | None, http_status: int | None = None) -> str:
    """``usage_logs.status`` (SUCCESS/ERROR/TIMEOUT) 산출 — 라우터가 finalize 에 넘긴다.

    스트림 헬퍼가 ``outcome`` dict 에 자기 종결 경로("success"|"error"|"timeout")를
    기록하고, 라우터는 그것과 자기가 아는 상류 HTTP 상태를 합성한다. outcome 의
    실패 판정이 우선하고, 없으면 HTTP 상태로 판정한다(비-2xx 오류 제너레이터는
    헬퍼에게 정상 종료로 보이므로 라우터 쪽 상태가 필요).

    클라이언트 끊김(disconnect drain)은 outcome 이 "success" 로 남는다 — 과금된
    토큰은 정상 전달된 것이고 끊긴 것은 게이트웨이/상류 실패가 아니다.
    """
    o = (outcome or {}).get("status")
    if o == "timeout" or http_status in (408, 504):
        return "TIMEOUT"
    if o == "error" or (http_status is not None and http_status >= 400):
        return "ERROR"
    return "SUCCESS"


def mark_outcome(outcome: dict | None, status: str) -> None:
    """스트림 종결 경로를 outcome dict 에 기록한다(None 이면 no-op).

    값은 "success"|"error"|"timeout" 소문자 — usage_status_of 가 usage_logs 용
    대문자 enum 으로 옮긴다.
    """
    if outcome is not None:
        outcome["status"] = status


def outcome_from_http(
    outcome: dict | None, http_status: int | None, failed: bool = False
) -> None:
    """비스트림/멀티턴 종결을 ``outcome`` 에 반영 — ``usage_status_of`` 와 같은 판정.

    ``failed`` 는 예외 등 HTTP 상태로 표현되지 않는 실패용이다. 성공 판정은
    setdefault — 이미 기록된 실패(outcome 선행 마킹)를 덮지 않는다.
    """
    if outcome is None:
        return
    if failed or (http_status is not None and http_status >= 400):
        outcome["status"] = "timeout" if http_status in (408, 504) else "error"
    else:
        outcome.setdefault("status", "success")


async def _aclose_iter(iterator) -> None:
    """버려지는 업스트림 이터레이터를 닫는다.

    닫지 않으면 어댑터 제너레이터(예: botocore event-stream 을 executor 스레드로
    돌리는 ``_bedrock_stream_gen``)의 finally 가 GC 시점까지 지연되고, 그동안
    스레드가 죽은 상류 스트림의 blocking ``next()`` 에 묶인다. 스트림 헬퍼가
    이터레이터를 중도 포기하는 모든 경로(idle-timeout/오류/드레인 완료)에서
    호출한다 — aclose 없는 이터레이터는 no-op(E2E S4).
    """
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        pass


def _resolve_timeouts(
    idle_timeout: float | None, drain_timeout: float | None
) -> tuple[float, float]:
    """SSE 타임아웃의 **단일 진실원**: 인자 미지정(None) 시 Settings 에서 해석.

    과거엔 각 헬퍼의 기본값이 하드코딩 60.0/30.0 이었고 **모든 호출부가 인자를 넘기지
    않아** `settings.stream_idle_timeout` / `stream_disconnect_drain_timeout` 이 완전한
    **죽은 설정**이었다 — 차트/env 로 어떤 값을 주입해도 런타임은 60s 로 동작했고,
    그래서 Opus extended thinking 요청이 정상 생성 중에 끊겼다.

    호출부는 5곳(routers/messages.py, routers/openai_compat.py×2,
    services/web_search_loop.py×3 — pass-through 경로 포함)이라 "호출부마다 인자 추가"
    방식은 새 호출부가 하나 생기는 순간 같은 회귀가 재발한다. 그래서 기본값 자체를
    설정에서 끌어오게 만들어 구조적으로 막는다.

    인자를 명시하면 그대로 우선한다(단위테스트가 idle_timeout=0.2 로 타임아웃 경로를
    강제하는 것처럼). 둘 다 명시된 경우엔 get_settings() 를 아예 호출하지 않는다.
    """
    if idle_timeout is not None and drain_timeout is not None:
        return idle_timeout, drain_timeout
    s = get_settings()
    return (
        float(s.stream_idle_timeout) if idle_timeout is None else idle_timeout,
        float(s.stream_disconnect_drain_timeout) if drain_timeout is None else drain_timeout,
    )


async def bedrock_anthropic_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
    outcome: dict | None = None,
) -> AsyncIterator[bytes]:
    """Bedrock EventStream chunks → Anthropic SSE-formatted bytes.

    Each upstream chunk is a JSON blob (one Anthropic event); we emit
    `event: <type>\\ndata: <json>\\n\\n` to the client. Token usage is
    aggregated across `message_start` (input + cache) and `message_delta`
    (output), with `amazon-bedrock-invocationMetrics` as a fallback.

    Edge case handling:
    - Client disconnect: stop yielding; drain remaining chunks in a
      background task so usage is still recorded (best-effort).
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit
      `event: error` SSE and return. Prevents hung upstream streams from
      pinning the connection.
    - Upstream exception mid-stream: emit `event: error` SSE with the
      error message and return gracefully (do not propagate).
    - Malformed JSON chunk: passthrough as `data: <raw>\\n\\n` (no crash).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    counters = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        # 삼값(None|int): 키가 보고될 때만 설정 — 0 기본값은 "미보고"와 구분 불가(A2-4).
        "cache_creation_1h": None,  # usage.cache_creation.ephemeral_1h_input_tokens
        "cache_read": 0,
    }
    accumulated_text: list[str] = []  # KI-08: content_block_delta.delta.text 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    # 과금 멱등 가드. 종료 경로가 4개(정상/idle timeout/upstream 예외/클라이언트 끊김
    # → 백그라운드 drain)라, 가드 없이 각 경로에서 finalize 를 호출하면 **이중 과금**이
    # 된다. 반대로 timeout/예외 경로에서 호출을 빼면 그때까지 생성된 토큰이 **유실**된다
    # (과거 동작: 두 경로가 함수 말미의 _fire_on_usage 를 건너뛰는 return 이었다).
    usage_fired = False
    # 본문 로깅용 원본 SSE 프레임 누적. on_complete 가 없으면 아무것도 쌓지 않는다.
    accumulated_frames: list[str] = []
    # 멀티바이트 문자가 청크 경계에 걸려도 감사 원문이 깨지지 않게.
    _frame_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    complete_fired = False

    def _format(chunk: bytes) -> bytes:
        """Parse chunk, update counters, return SSE-formatted bytes."""
        nonlocal first_token_time
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            raw = (
                chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            )
            return f"data: {raw}\n\n".encode()

        etype = data.get("type", "unknown")
        if etype == "unknown" and isinstance(data.get("error"), dict):
            # 어댑터의 미드스트림 오류 청크는 최상위 "type" 없이 {"error":{...}} 만 낸다
            # (mantle_adapter._gen 등). 그대로 `event: unknown` 으로 나가면 SDK 가
            # 무시해 상류 실패가 조용한 절단으로 보인다 — 표준 error 이벤트로
            # 재프레임(web_search_loop 의 동형 처리와 합을 맞춘다, E2E S4).
            data = {"type": "error", "error": data["error"]}
            etype = "error"
        if etype == "error":
            # 200 스트림 안의 미드스트림 오류 — 상류(또는 합성) error 이벤트가 왔으면
            # 이 요청은 실패 종결이다. 과금은 발생분만 남지만 status 는 ERROR 여야
            # usage_logs/monitoring 이 진실을 보인다(E2E S5).
            mark_outcome(outcome, "error")
        if etype == "message_start":
            u = data.get("message", {}).get("usage", {})
            if v := u.get("input_tokens"):
                counters["input_tokens"] = v
            counters["cache_creation"] = u.get(
                "cache_creation_input_tokens", counters["cache_creation"]
            )
            counters["cache_read"] = u.get("cache_read_input_tokens", counters["cache_read"])
            # 1h TTL 캐시 쓰기 분해 — 혼합 TTL 정확 과금(R2-12).
            # ⚠️ `if v :=` 로 걸러선 안 된다 — 0 이 보고되면 "분해는 있는데 1h=0"
            #    (전부 5m 과금이 정답) 인데 미보고로 잘못 기록된다(A2-4).
            cc = u.get("cache_creation")
            if isinstance(cc, dict) and "ephemeral_1h_input_tokens" in cc:
                counters["cache_creation_1h"] = cc["ephemeral_1h_input_tokens"] or 0
        elif etype == "content_block_delta":
            # KI-08: 스트림 도중 생성된 텍스트 누적. disconnect 시 tokenizer 역산용.
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                if t := delta.get("text"):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(t)
        elif etype == "message_delta":
            u = data.get("usage", {})
            if v := u.get("output_tokens"):
                counters["output_tokens"] = v
        elif m := data.get("amazon-bedrock-invocationMetrics"):
            # invocationMetrics는 Bedrock이 최종 집계한 billable 토큰 수.
            # message_delta.usage.output_tokens와 미세하게 다를 수 있으므로
            # (extended thinking 모델에서 ~3 토큰 차이) 항상 이 값으로 덮어쓴다.
            if v := m.get("inputTokenCount"):
                counters["input_tokens"] = v
            if v := m.get("outputTokenCount"):
                counters["output_tokens"] = v

        return f"event: {etype}\ndata: {json.dumps(data)}\n\n".encode()

    def _current_usage() -> TokenUsage | None:
        it, ot = counters["input_tokens"], counters["output_tokens"]
        if not (it or ot):
            return None
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=it + ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
            cache_creation_1h_input_tokens=counters["cache_creation_1h"],
        )

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: output_tokens=0 인데 누적 텍스트가 있으면 tokenizer로 역산.

        disconnect 케이스: message_start로 input_tokens는 있으나 message_delta 전
        끊김 → output_tokens=0. 누적된 content_block_delta text로 역산.
        """
        if not tokenizer_hook or not accumulated_text:
            return usage
        it = counters["input_tokens"]
        ot = counters["output_tokens"]
        if ot > 0:
            return usage  # provider에서 실제 usage 이벤트 수신됨 → 역산 불필요
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        return TokenUsage(
            input_tokens=it,
            output_tokens=estimated_ot,
            total_tokens=it + estimated_ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
            cache_creation_1h_input_tokens=counters["cache_creation_1h"],
            estimated=True,
        )

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _fire_on_usage() -> None:
        """KI-08: usage 미추출 시에도 빈 TokenUsage로 콜백 실행.

        라우터가 TPM 예약을 설정한 경우, 빈 usage로도 콜백이 돌아가야
        cost_recorder가 ``settle_tpm(actual=0)``을 호출해 예약 해제함.
        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.

        **정확히 1회만** 실행된다(usage_fired 가드) — 위 4개 종료 경로 중
        어디로 빠져도 과금이 유실되지도, 중복되지도 않게.
        """
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
        base = _current_usage()
        estimated = await _estimate_if_needed(base)
        usage = estimated or base or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("stream_drain_timeout")
                    break
                try:
                    # ⚠️ async for 대신 wait_for — 상류가 스톨하면 async for 는
                    #    deadline 을 무시하고 __anext__ 에서 무한 대기해, 클라이언트도
                    #    사라진 드레인 태스크가 상류 커넥션을 붙잡는다(E2E S4).
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=remaining
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    logger.warning("stream_drain_timeout")
                    break
                _format(chunk)  # side effect: updates counters
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            await _fire_on_usage()
            await _fire_on_complete("partial")
            await _aclose_iter(iterator)
            if outcome is not None:
                outcome.pop("drain_pending", None)

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # ⚠️ yield 보다 **먼저** 확정한다. 클라이언트가 이미 끊긴 상태면 아래
                # yield 가 GeneratorExit 을 던져 except 절로 빠지므로, 뒤에 두면
                # 이 경로의 과금이 다시 유실된다.
                mark_outcome(outcome, "timeout")
                await _fire_on_usage()
                await _fire_on_complete("partial")
                await _aclose_iter(iterator)
                err = {
                    "type": "error",
                    "error": {
                        "type": "timeout_error",
                        "message": f"upstream idle timeout after {idle_timeout}s",
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                return

            _frame = _format(chunk)
            if on_complete is not None:
                accumulated_frames.append(_frame_decoder.decode(_frame))
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. Spawn background
        # drain so usage is still recorded, then re-raise per asyncio contract.
        logger.info("bedrock_stream_cancelled")
        client_disconnected = True
        # 클라이언트 측 종결 — 게이트웨이/상류 실패가 아니라 과금은 정상 성공 건.
        # setdefault: 직전에 상류 error 이벤트를 받았다면 그 판정이 우선한다.
        if outcome is not None:
            outcome.setdefault("status", "success")
            # unsettled_reservation_guard 가 드레인 정산과 경쟁해 예약을 먼저
            # release(0) 하면 실비가 유실/이중정산된다 — 정산 소유권 표시.
            outcome["drain_pending"] = True
        _spawn(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("bedrock_stream_proxy_error")
        mark_outcome(outcome, "error")
        # timeout 경로와 동일 이유로 yield 앞에서 확정.
        await _fire_on_usage()
        await _fire_on_complete("partial")
        await _aclose_iter(iterator)
        err = {
            "type": "error",
            # 예외 메시지에 상류 ARN/계정 ID 가 섞일 수 있다 — 그대로 내보내면
            # 인프라 식별자가 클라이언트에 새므로 살균한다.
            "error": {
                "type": "stream_error",
                "message": _redact_arns(str(exc)) or "stream_error",
            },
        }
        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
        return

    if not client_disconnected:
        # setdefault: 미드스트림 error 이벤트를 받고 정상 종료한 경우 실패 판정 유지.
        if outcome is not None:
            outcome.setdefault("status", "success")
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


async def openai_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
    outcome: dict | None = None,
) -> AsyncIterator[bytes]:
    """OpenAI-compatible SSE chunks → passthrough bytes (no re-formatting).

    OpenAI upstreams already emit SSE-formatted frames (`data: {...}\\n\\n`,
    `data: [DONE]\\n\\n`). A single httpx chunk may contain one or many
    frames. We yield chunks as-is and scan each chunk for a `usage` object
    (vLLM emits it on the final chunk when `stream_options.include_usage`).

    Edge case handling mirrors `bedrock_anthropic_sse_stream`:
    - Client disconnect: stop, background-drain so usage is still recorded.
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit an
      OpenAI-shaped error chunk (`data: {"error":{"type":"timeout_error",...}}\\n\\n`)
      and return.
    - Upstream exception mid-stream: same OpenAI-shaped error chunk path.
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []  # KI-08: delta.content 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False
    # 멀티바이트 문자가 청크 경계에 걸려도 감사 원문이 깨지지 않게.
    _frame_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"data: {json.dumps(payload)}\n\n".encode()

    def _emit_error_chunk_framed(err_type: str, message: str, *, had_tail: bool) -> bytes:
        """패스스루는 원시 바이트를 그대로 흘려보내므로 직전 청크가 프레임 중간에서
        끊겼을 수 있다. 개행 없이 error 프레임을 붙이면 클라이언트 SSE 파서가
        두 프레임을 한 라인으로 읽어 **error 프레임까지** 파싱하지 못한다.
        미완결 꼬리가 있었으면(had_tail — _flush 가 지우기 전에 스냅샷) `\\n\\n` 로
        그 프레임을 닫고 낸다 — 꼬리는 어차피 반쪽 JSON 이라 버려지는 이벤트가
        되고, error 프레임은 온전히 전달된다."""
        prefix = b"\n\n" if had_tail else b""
        return prefix + _emit_error_chunk(err_type, message)

    # aiter_bytes 청크는 SSE 프레임 경계와 무관하게 끊긴다 — `data: {...}` 라인이
    # 두 청크에 걸치면 json.loads 가 실패해 그 프레임의 usage 가 통째로 유실된다
    # (vLLM 은 usage 를 마지막 프레임에만 실으므로 최악의 경우 input 토큰이 0 으로
    # 기록됐다). 마지막 개행까지만 완결 라인으로 파싱하고 꼬리는 다음 청크로 넘긴다.
    _pending_sse = bytearray()

    def _scan_lines(text: str) -> TokenUsage | None:
        nonlocal first_token_time
        found: TokenUsage | None = None
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # 상류 오류 프레임(data: {"error":{...}}) — 요청은 실패 종결(E2E S5).
            if isinstance(data.get("error"), dict):
                mark_outcome(outcome, "error")
            # KI-08: delta content 누적. OpenAI SSE: {"choices":[{"delta":{"content":"..."}}]}
            for choice in data.get("choices", []) or []:
                delta = choice.get("delta") or {}
                if isinstance(delta, dict) and (c := delta.get("content")):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(c)
            if u := data.get("usage"):
                # Shared Chat-wire parser: splits the cache-inclusive prompt count into
                # exclusive TokenUsage buckets and picks up reasoning_tokens. vLLM sends
                # none of the details sub-objects, so its behaviour is byte-identical to
                # the previous three-field construction; GPT-5.6 on either Bedrock plane
                # sends them and would otherwise be billed as if nothing were cached.
                found = extract_chat_usage(u)
        return found

    def _scan_usage(chunk: bytes) -> TokenUsage | None:
        """청크를 버퍼에 붙이고 완결 라인까지만 스캔한다."""
        _pending_sse.extend(chunk)
        nl = _pending_sse.rfind(b"\n")
        if nl == -1:
            return None
        try:
            text = bytes(_pending_sse[:nl]).decode("utf-8", errors="ignore")
        except Exception:
            del _pending_sse[: nl + 1]
            return None
        del _pending_sse[: nl + 1]
        return _scan_lines(text)

    def _flush_pending_usage() -> TokenUsage | None:
        """스트림 종료 시 개행 없이 남은 꼬리도 스캔 (업스트림이 마지막 프레임을
        개행 없이 닫는 경우 usage 유실 방지)."""
        if not _pending_sse:
            return None
        tail = bytes(_pending_sse)
        _pending_sse.clear()
        try:
            text = tail.decode("utf-8", errors="ignore")
        except Exception:
            return None
        return _scan_lines(text)

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: usage 없고 누적 텍스트 있으면 tokenizer 역산."""
        if not tokenizer_hook or not accumulated_text:
            return usage
        if usage and usage.output_tokens > 0:
            return usage
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        # ⚠️ 새로 만들지 않고 **복사 후 덮어쓴다** — Responses 방언(아래
        #    `_estimate_output_tokens`)과 같은 규칙. 새 TokenUsage 로 만들면
        #    cache 버킷·web_search_count·cache_ttl_1h 가 지워져, 끊긴 스트림의
        #    캐시된 입력 토큰이 과금에서 증발했다. usage 자체가 없으면 input 은
        #    모르는 그대로 0 — 최소한 output 추정치는 남긴다.
        base = usage or TokenUsage()
        return base.model_copy(
            update={
                "output_tokens": estimated_ot,
                "total_tokens": base.input_tokens + estimated_ot,
                "estimated": True,
            }
        )

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _fire_on_usage() -> None:
        """KI-08: latest_usage 없어도 빈 TokenUsage로 콜백 실행 (TPM 예약 해제용).

        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.
        **정확히 1회만** 실행된다(usage_fired 가드).
        """
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
        estimated = await _estimate_if_needed(latest_usage)
        usage = estimated or latest_usage or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        nonlocal latest_usage
        deadline = time.monotonic() + drain_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("stream_drain_timeout")
                    break
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=remaining
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    logger.warning("stream_drain_timeout")
                    break
                if u := _scan_usage(chunk):
                    latest_usage = u
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            if u := _flush_pending_usage():
                latest_usage = u
            await _fire_on_usage()
            await _fire_on_complete("partial")
            await _aclose_iter(iterator)
            if outcome is not None:
                outcome.pop("drain_pending", None)
            if outcome is not None:
                outcome.pop("drain_pending", None)

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                mark_outcome(outcome, "timeout")
                _had_tail = bool(_pending_sse)
                if u := _flush_pending_usage():
                    latest_usage = u
                await _fire_on_usage()
                await _fire_on_complete("partial")
                await _aclose_iter(iterator)
                yield _emit_error_chunk_framed(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s",
                    had_tail=_had_tail,
                )
                return

            if u := _scan_usage(chunk):
                latest_usage = u
            if on_complete is not None:
                accumulated_frames.append(
                    _frame_decoder.decode(chunk)
                    if isinstance(chunk, bytes)
                    else str(chunk)
                )
            yield chunk

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. See the twin
        # handler in `bedrock_anthropic_sse_stream` for rationale.
        logger.info("openai_stream_cancelled")
        client_disconnected = True
        # setdefault: 직전 상류 error 프레임 판정이 우선한다(E2E S5).
        if outcome is not None:
            outcome.setdefault("status", "success")
            # unsettled_reservation_guard 가 드레인 정산과 경쟁해 예약을 먼저
            # release(0) 하면 실비가 유실/이중정산된다 — 정산 소유권 표시.
            outcome["drain_pending"] = True
        _spawn(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("openai_stream_proxy_error")
        mark_outcome(outcome, "error")
        _had_tail = bool(_pending_sse)
        if u := _flush_pending_usage():
            latest_usage = u
        await _fire_on_usage()  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        await _fire_on_complete("partial")
        await _aclose_iter(iterator)
        yield _emit_error_chunk_framed(
            "stream_error", _redact_arns(str(exc)) or "stream_error", had_tail=_had_tail
        )
        return

    if not client_disconnected:
        # setdefault: 미드스트림 error 프레임 뒤 정상 종료면 실패 판정 유지.
        if outcome is not None:
            outcome.setdefault("status", "success")
        if u := _flush_pending_usage():
            latest_usage = u
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


async def responses_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    on_complete: OnComplete = None,
    tokenizer_hook: TokenizerHook = None,
    outcome: dict | None = None,
) -> AsyncIterator[bytes]:
    """OpenAI **Responses API** → re-framed SSE (`event: {type}\\ndata: {json}\\n\\n`).

    The MantleOpenAIAdapter yields RAW JSON event payloads (one per chunk, `data:`
    prefix already stripped — same contract as MantleAdapter/bedrock_anthropic_sse_stream).
    This stream parses each raw JSON chunk, accumulates text/usage, and re-frames it as
    a proper SSE event for the client (mirrors bedrock_anthropic_sse_stream, NOT the
    openai_sse_stream passthrough which assumes upstream is already SSE-framed).

    Usage shape: the terminal `response.completed` event carries final usage nested at
    `event["response"]["usage"]` (input/output/total + output_tokens_details.reasoning_tokens).
    `response.incomplete`/`response.failed` may carry usage or null. Text deltas arrive
    as `response.output_text.delta`. reasoning_tokens is a submetric (already inside
    output_tokens) — never re-added to total/cost.

    idle/drain 타임아웃 기본값은 Settings 에서 해석된다(_resolve_timeouts).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False
    # 멀티바이트 문자가 청크 경계에 걸려도 감사 원문이 깨지지 않게.
    _frame_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()

    def _usage_from_response(resp: dict) -> TokenUsage | None:
        # Same parser as the non-streaming adapters (providers/openai_usage) — that is
        # what guarantees a prompt bills identically streamed vs non-streamed, on both the
        # Mantle and the bedrock-runtime plane. None (not a zero TokenUsage) when the
        # event carries no usage object, so a usage-less `response.incomplete` cannot
        # erase a good reading from an earlier terminal event.
        if not isinstance(resp.get("usage"), dict):
            return None
        return extract_responses_usage(resp)

    def _process(chunk: bytes) -> bytes:
        """Parse a RAW JSON event chunk → update usage/text, return re-framed SSE bytes.

        Malformed JSON is passed through as a bare `data:` frame (no crash), matching
        bedrock_anthropic_sse_stream's defensive behaviour.
        """
        nonlocal latest_usage, first_token_time
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = chunk.decode("utf-8", errors="ignore").strip()
            return f"data: {raw}\n\n".encode()
        if isinstance(data, dict):
            etype = data.get("type", "")
            if etype == "response.output_text.delta":
                if (d := data.get("delta")) and isinstance(d, str):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(d)
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                resp = data.get("response")
                if isinstance(resp, dict) and (u := _usage_from_response(resp)):
                    latest_usage = u
                if etype == "response.failed":
                    mark_outcome(outcome, "error")  # E2E S5
            # 어댑터 미드스트림 오류 청크({"error":{...}} 단일 형태, type 없음)가
            # `event: message` 로 나가면 SDK 가 일반 이벤트로 삼키고 실패를 모른다 —
            # error 이벤트로 명시(E2E S4).
            if etype != "response.failed" and isinstance(data.get("error"), dict):
                mark_outcome(outcome, "error")  # E2E S5
            etype_label = etype or ("error" if isinstance(data.get("error"), dict) else "message")
            return f"event: {etype_label}\ndata: {json.dumps(data)}\n\n".encode()
        # Non-dict JSON (unexpected) — re-frame defensively.
        return f"data: {json.dumps(data)}\n\n".encode()

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _estimate_output_tokens() -> TokenUsage | None:
        """KI-08 역산 — 이 방언에만 없던 것.

        ⚠️ 왜 필요한가. Responses 방언은 usage 를 **종결 이벤트(response.completed)
           안에서만** 준다. 그래서 상류가 idle timeout 이나 오류로 그 이벤트 전에 끊기면
           ``latest_usage`` 가 None 이고, ``TokenUsage()``(전부 0)로 발화한다.
           ``cost_recorder.finalize`` 는 total/input/output 이 모두 0 이면 TPM 예약만
           돌려주고 **usage_logs 행을 아예 만들지 않은 채** 리턴한다 — provider 가 이미
           AWS 에 청구한 토큰이 우리 쪽에는 존재하지 않게 된다.

           같은 요청 형태가 ``/v1/messages`` 와 ``/v1/chat/completions`` 에서는 역산되어
           기록된다. 즉 이 누락은 **방언별**이라 집계 대시보드에서는 보이지 않는다.

        ⚠️ input 과 cache 버킷을 **앞으로 이어 나른다.** response.incomplete/failed 는
           input+cache 를 담고 output 만 0 인 경우가 있어서, output 만으로 TokenUsage 를
           새로 만들면 캐시 버킷이 지워져 지금보다 더 과소청구가 된다.
        """
        if not tokenizer_hook or not accumulated_text:
            return None
        base = latest_usage or TokenUsage()
        if base.output_tokens > 0:
            return None  # 실제 usage 를 받았다 — 역산 불필요
        try:
            estimated = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            return None
        if not estimated or estimated <= 0:
            return None
        # ⚠️ 새로 만들지 않고 **복사 후 덮어쓴다.** TokenUsage 에는 output 과 무관한 필드가
        #    더 있다(web_search_count — 웹서치 귀속/과금, cache_ttl_1h — 캐시 단가 분기).
        #    필드를 열거해 새로 만들면 나중에 필드가 추가될 때 조용히 유실된다.
        return base.model_copy(
            update={
                "output_tokens": estimated,
                "total_tokens": base.input_tokens + estimated,
                "estimated": True,
            }
        )

    async def _fire_on_usage() -> None:
        """**정확히 1회만** 실행된다(usage_fired 가드)."""
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
        usage = latest_usage or TokenUsage()
        estimated_usage = await _estimate_output_tokens()
        if estimated_usage is not None:
            usage = estimated_usage
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("responses_stream_drain_timeout")
                    break
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=remaining
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    logger.warning("responses_stream_drain_timeout")
                    break
                _process(chunk)  # updates latest_usage/accumulated_text as a side effect
        except Exception:
            logger.exception("responses_stream_drain_error")
        finally:
            await _fire_on_usage()
            await _fire_on_complete("partial")
            await _aclose_iter(iterator)
            if outcome is not None:
                outcome.pop("drain_pending", None)

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("responses_stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                mark_outcome(outcome, "timeout")
                await _fire_on_usage()
                await _fire_on_complete("partial")
                await _aclose_iter(iterator)
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            _frame = _process(chunk)
            if on_complete is not None:
                accumulated_frames.append(
                    _frame_decoder.decode(_frame)
                    if isinstance(_frame, bytes)
                    else str(_frame)
                )
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        logger.info("responses_stream_cancelled")
        client_disconnected = True
        # setdefault: 직전 상류 error 판정이 우선한다(E2E S5).
        if outcome is not None:
            outcome.setdefault("status", "success")
            # unsettled_reservation_guard 가 드레인 정산과 경쟁해 예약을 먼저
            # release(0) 하면 실비가 유실/이중정산된다 — 정산 소유권 표시.
            outcome["drain_pending"] = True
        _spawn(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("responses_stream_proxy_error")
        mark_outcome(outcome, "error")
        await _fire_on_usage()  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        await _fire_on_complete("partial")
        await _aclose_iter(iterator)
        yield _emit_error_chunk("stream_error", _redact_arns(str(exc)) or "stream_error")
        return

    if not client_disconnected:
        # setdefault: response.failed / 미드스트림 error 뒤 종료면 실패 판정 유지.
        if outcome is not None:
            outcome.setdefault("status", "success")
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


def _try_extract_usage(chunk: bytes) -> TokenUsage | None:
    """청크에서 usage 추출 시도 (OpenAI SSE 형식)."""
    try:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                if usage := data.get("usage"):
                    return TokenUsage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        total_tokens=usage.get("total_tokens", 0),
                    )
    except Exception:
        pass
    return None
