# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""spec<2.4 '미시작 제너레이터' 예약 누수 안전망 + 취소 스코프 정산 분리 회귀.

Kiro C 리뷰로 확정된 두 HIGH:

1. StreamingResponse 의 body iterator 가 **한 번도 시작되지 않으면**(클라이언트가
   첫 SSE 바이트 전에 끊김 → listen_for_disconnect 가 task group 취소)
   제너레이터의 finally 도 돌지 않아 on_usage → pop + finalize 가 통째로
   건너뛰어진다. ``unsettled_reservation_guard`` 가 StreamingResponse.background
   로 이 구멍을 메운다 — background 는 task group 탈출 뒤 취소 스코프 **밖**에서
   실행되는 것이 근거다.

2. 취소된 anyio 스코프 안에서 unwinding 되는 제너레이터의 finally 는 ``await`` 가
   CancelledError 재전달로 죽는다(실측 확인). ``_settle_stream_usage`` 는
   ``task.cancelling() > 0`` 이면 정산을 스코프 밖 태스크로 떼어낸다.
"""

import asyncio

import anyio
import pytest

from app.services.fallback_loop import unsettled_reservation_guard


class _Auth:
    user_id = "u1"
    team_id = "t1"


class _Redis:
    """release_reservations 의 settle 경로가 호출하는 최소 표면."""

    def __init__(self):
        self.eval_calls = 0

    async def eval(self, *a, **kw):
        self.eval_calls += 1
        return b"{}"


def _state_with_reservation(redis):
    return {
        "_redis": redis,
        "auth_context": _Auth(),
        "rate_limit_state": {
            "tpm_descriptors": [],
            "tpm_reserved": 0,
            "cost_reserved": None,
            "budget_reservation": [
                {"scope": "user", "usage_key": "k", "config_key": "c",
                 "marker_key": "m", "resvsum_key": "r", "fallback": ""}
            ],
        },
    }


async def test_guard_releases_reservation_when_state_survives():
    """제너레이터가 정산 없이 끝났다(state 잔존) → background 가 되돌린다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts.setdefault("budget_settle", "-- mock")
    redis = _Redis()
    state = _state_with_reservation(redis)
    bg = unsettled_reservation_guard(state)
    await bg()  # BackgroundTask 는 호출 가능
    assert "rate_limit_state" not in state
    assert redis.eval_calls >= 1  # budget settle(0) 이 실제로 나갔다


async def test_guard_is_noop_after_normal_settlement():
    """정상 정산 뒤(state pop 됨)에는 no-op — 모든 응답에 달아도 안전해야 한다."""
    redis = _Redis()
    state = {"_redis": redis, "auth_context": _Auth()}
    bg = unsettled_reservation_guard(state)
    await bg()
    assert redis.eval_calls == 0


async def test_guard_skips_when_redis_absent():
    """Redis 없으면 Lua 예약 자체를 안 잡으므로 되돌릴 것도 없다."""
    state = _state_with_reservation(None)
    state.pop("_redis")
    bg = unsettled_reservation_guard(state)
    await bg()
    assert "rate_limit_state" in state  # 손대지 않는다


async def test_every_streaming_response_carries_the_guard():
    """가드가 빠진 StreamingResponse 반환 지점이 생기면 이 테스트가 잡는다."""
    import ast
    import inspect

    from app.routers import bedrock, messages, openai_compat

    found = 0
    for mod in (messages, openai_compat, bedrock):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", getattr(fn, "attr", ""))
            if name != "StreamingResponse":
                continue
            found += 1
            kw = {k.arg for k in node.keywords}
            assert "background" in kw, (
                f"{mod.__name__}:{node.lineno} StreamingResponse 에 "
                f"background=unsettled_reservation_guard 가 없다"
            )
    assert found >= 4  # messages 1 + openai 2 + bedrock 1


async def test_web_search_response_gets_guard_at_router():
    """web-search 는 루프가 응답을 만들어 라우터가 background 를 붙인다.

    S-1 이후 guard 는 루프가 단 스티처-클로즈 태스크와 BackgroundTasks 로
    합성된다 — 덮어쓰면 peek 가 시작한 턴-1 의 drain_pending 이 안 서서
    release-vs-settle 경쟁이 다시 열린다."""
    import inspect

    from app.routers import messages, openai_compat

    for mod in (messages, openai_compat):
        src = inspect.getsource(mod)
        assert "BackgroundTasks" in src, f"{mod.__name__} 에 합성이 없다"
        assert "unsettled_reservation_guard(state)" in src, (
            f"{mod.__name__} 에 guard 배선이 없다")


async def test_settle_stream_usage_detaches_under_cancellation():
    """취소 스코프 finally 에서 정산 await 는 죽는다 — _spawn 으로 탈출해야 한다."""
    from app.services.web_search_loop import _settle_stream_usage
    from app.services import streaming

    settled: list[str] = []
    spawned: list[asyncio.Task] = []

    async def on_usage(merged):
        await asyncio.sleep(0.01)
        settled.append("done")

    real_spawn = streaming._spawn
    def tracking_spawn(coro):
        t = real_spawn(coro)
        spawned.append(t)
        return t

    streaming._spawn = tracking_spawn
    # web_search_loop 는 임포트 시점에 _spawn 을 바인딩한다 — 모듈 속성을 직접 친다.
    import app.services.web_search_loop as wsl
    wsl._spawn = tracking_spawn
    try:
        async def victim():
            task = asyncio.current_task()
            task.cancel()
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            # finally 상황과 동일: cancelling() > 0 이면 스폰 경로를 탄다.
            await _settle_stream_usage(on_usage, object())

        try:
            await victim()
        except asyncio.CancelledError:
            pass
    finally:
        streaming._spawn = real_spawn
        wsl._spawn = real_spawn

    assert spawned, "취소 중인 태스크에서 정산이 스폰되지 않았다"
    await asyncio.gather(*spawned)
    assert settled == ["done"]


async def test_settle_stream_usage_awaits_when_not_cancelled():
    """정상 종결에서는 동기 정산 — 예외를 그 자리에서 관측한다."""
    from app.services.web_search_loop import _settle_stream_usage

    settled: list[str] = []

    async def on_usage(merged):
        settled.append("done")

    await _settle_stream_usage(on_usage, object())
    assert settled == ["done"]


async def test_reserve_budget_refunds_scope_on_eval_timeout():
    """eval 타임아웃은 서버 실행 완료일 수 있다 — 그 scope 도 환불 대상이어야 한다."""
    import inspect

    from app.services.budget_service import BudgetService

    src = inspect.getsource(BudgetService.reserve_budget)
    # eval+loads 가 같은 try 안 + except 에서 committed.append 후 refund.
    eval_idx = src.index("await redis.eval")
    loads_idx = src.index("result = json.loads(raw)")
    except_idx = src.index("except Exception", eval_idx)
    assert loads_idx < except_idx, "json.loads 가 try 밖이면 eval 성공분이 고아"
    refund_idx = src.index("_refund_reservations", except_idx)
    append_idx = src.index("committed.append", except_idx)
    assert append_idx < refund_idx, "타임아웃 scope 가 환불 대상에서 빠졌다"


async def test_error_chunk_closes_partial_sse_frame():
    """패스스루 error 프레임은 미완결 꼬리를 닫고 나간다."""
    import json

    from app.services.streaming import openai_sse_stream

    class _Req:
        async def is_disconnected(self):
            return False

    async def chunks():
        yield b'data: {"choices":[{"delta":{"content":"par'  # 반쪽 프레임
        raise RuntimeError("upstream died")

    out = [
        c
        async for c in openai_sse_stream(
            _Req(), chunks(), idle_timeout=5.0, drain_timeout=0.5
        )
    ]
    # 마지막 청크: \n\n 로 반쪽 프레임 종결 후 온전한 error 프레임
    tail = out[-1]
    assert tail.startswith(b"\n\n")
    frame = tail.lstrip(b"\n")
    assert frame.startswith(b"data: ")
    payload = json.loads(frame[len(b"data: ") :].strip())
    assert payload["error"]["type"] == "stream_error"


async def test_completions_rejects_unsupported_before_reservation():
    """/v1/completions 는 BEDROCK_RUNTIME_OPENAI(미지원) 거절을 enforce_rate_limits
    보다 앞에 둬야 한다 — 예약을 잡은 뒤 404 를 내면 해제 경로가 없어
    RPM/TPM/CPM·월예산 마커가 새는 회귀를 AST 로 잠근다."""
    import ast
    import inspect

    from app.routers import openai_compat

    tree = ast.parse(inspect.getsource(openai_compat))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_openai"
    )
    src_lines = inspect.getsource(openai_compat).splitlines()

    def _lineno(pred):
        for node in ast.walk(fn):
            if pred(node):
                return node.lineno
        return None

    reject_ln = _lineno(
        lambda n: isinstance(n, ast.If)
        and "is_bedrock_runtime" in ast.dump(n.test)
    )
    enforce_ln = _lineno(
        lambda n: isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "enforce_rate_limits"
    )
    assert reject_ln is not None and enforce_ln is not None
    assert reject_ln < enforce_ln, (
        f"unsupported-provider 거절({reject_ln})이 예약({enforce_ln})보다 뒤에 있다 — "
        "예약 후 404 는 전량 누수"
    )
