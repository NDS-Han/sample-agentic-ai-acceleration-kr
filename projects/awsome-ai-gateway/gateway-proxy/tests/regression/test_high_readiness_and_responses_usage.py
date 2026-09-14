# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Redis 단독 장애에 파드를 빼지 않는가, 그리고 Responses 방언이 usage 를 잃지 않는가.

두 결함
-------
1. **readiness 가 REDIS_DEGRADED 에서 503 이었다.** REDIS_DEGRADED 는 파드별 상태가 아니라
   **플릿 전체가 동시에** 들어가는 상태다(같은 ElastiCache). primary failover 로 Redis 가
   ~90초 끊기면 모든 replica 가 거의 동시에 NotReady 가 되고, target-type: ip 이므로 ALB
   타깃 그룹이 비어 사용자는 장애 구간 전체를 503 으로 받는다. liveness 는 관대한 /health
   라 파드는 재시작되지도 않는다 — 멀쩡한 파드가 아무도 닿을 수 없는 상태로 앉아 있는다.

   그런데 이 코드베이스는 Redis 다운 연속성에 이미 많이 투자돼 있다(인메모리 폴백 리미터,
   예산 DB 폴백, 서킷브레이커, cost:stream 스풀). 그것들을 만들어 두고 파드를 빼면 전부
   무의미하다.

2. **``responses_sse_stream`` 만 KI-08 역산이 없었다.** 이 방언은 usage 를 종결
   이벤트(``response.completed``) 안에서만 준다. 그래서 상류가 그 전에 끊기면 usage 가
   전부 0 이고, ``cost_recorder.finalize`` 는 그 경우 TPM 예약만 돌려주고 **usage_logs 행을
   아예 만들지 않은 채** 리턴한다 — provider 가 이미 AWS 에 청구한 토큰이 우리 쪽에
   존재하지 않는다. 같은 요청 형태가 ``/v1/messages`` 와 ``/v1/chat/completions`` 에서는
   역산되어 기록되므로, 이 누락은 **방언별**이고 집계 대시보드에서는 보이지 않는다.

⚠️ 이 파일은 use-before-assignment 를 **순서로** 검사한다. 앞선 수정에서 "함수 안에
   정의돼 있다" 만 확인하는 AST 검사를 썼다가 두 번 물렸다: 훅 정의가 웹서치 분기보다
   **뒤에** 있어서 그 경로에서 UnboundLocalError 였다. 정의 존재와 그 지점에서의 바인딩은
   다른 주장이다.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.domain import DegradationLevel, TokenUsage
from app.services.streaming import responses_sse_stream

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# 1. readiness
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, level: DegradationLevel):
        dm = SimpleNamespace(level=level)
        self.app = SimpleNamespace(state=SimpleNamespace(degradation_manager=dm, db_engine=None))
        self.scope = {"state": {"_degradation_manager": dm}}


async def _ready(level: DegradationLevel):
    from app.routers.health import readiness_check

    resp = await readiness_check(_FakeRequest(level))
    return resp.status_code, json.loads(bytes(resp.body))


async def test_redis_only_outage_keeps_the_pod_in_rotation():
    """⚠️ 이 파일의 핵심. Redis 단독 장애로 파드를 로드밸런서에서 빼면 안 된다.

    REDIS_DEGRADED 는 플릿 공통 상태라 이 판정 하나가 **전 파드**를 동시에 뺀다.
    Redis 다운 폴백들이 전부 존재하므로 그 요청들은 서빙될 수 있었다.
    """
    status, body = await _ready(DegradationLevel.REDIS_DEGRADED)
    assert status == 200, f"{status} — Redis 단독 장애로 파드가 빠진다(전 파드 동시)"
    assert body["status"] == "ready"


async def test_redis_degradation_is_still_reported_in_the_body():
    """판정에서는 빠지되 **보고는 되어야** 한다.

    운영자가 "왜 200 인데 레이트리밋이 근사인가" 를 알 수 있어야 한다.
    """
    _status, body = await _ready(DegradationLevel.REDIS_DEGRADED)
    assert body["redis_degraded"] is True
    assert body["degradation_level"] == DegradationLevel.REDIS_DEGRADED.value


@pytest.mark.parametrize(
    "level", [DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED]
)
async def test_db_degradation_still_takes_the_pod_out(level: DegradationLevel):
    """⚠️ 대조군이자 요구사항. DB 가 없으면 폴백 자체가 성립하지 않는다.

    이 단정이 없으면 "readiness 를 항상 200 으로" 만들어도 위 테스트가 통과한다.
    """
    status, body = await _ready(level)
    assert status == 503, f"{level.value} 인데 파드가 rotation 에 남는다"
    assert body["status"] == "not_ready"


async def test_healthy_is_ready():
    status, body = await _ready(DegradationLevel.HEALTHY)
    assert status == 200 and body["redis_degraded"] is False


def test_the_readiness_predicate_names_the_db_levels_explicitly():
    """게이트가 "HEALTHY 인가" 로 되돌아가면 다시 전 파드가 빠진다.

    허용 레벨의 여집합이 아니라 **차단 레벨을 명시**하는 형태여야, 나중에 레벨이 추가될 때
    (예: 새 종속성) 기본이 "서빙 계속" 이 된다.
    """
    src = (_SRC / "routers" / "health.py").read_text(encoding="utf-8")
    assert "_NOT_READY_LEVELS" in src, "차단 레벨을 명시하는 상수가 없다"
    assert "level == DegradationLevel.HEALTHY and not pool_saturated" not in src, (
        "판정이 'HEALTHY 인가' 로 되돌아갔다 — Redis 단독 장애가 전 파드를 뺀다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Responses 방언의 usage 역산
# ─────────────────────────────────────────────────────────────────────────────


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


class _Req:
    async def is_disconnected(self) -> bool:
        return False


async def _drain_responses(frames, *, tokenizer_hook=None):
    async def gen():
        for f in frames:
            yield f

    seen: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, _ttft=None):
        seen.append(u)

    out = []
    async for chunk in responses_sse_stream(
        _Req(), gen(), on_usage=on_usage, tokenizer_hook=tokenizer_hook
    ):
        out.append(chunk)
    return out, (seen[0] if seen else None)


_TRUNCATED = [
    _raw({"type": "response.created", "response": {"id": "r1"}}),
    _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "hello world"}),
]


async def test_truncated_responses_stream_records_zero_without_the_hook():
    """⚠️ 먼저 결함의 전제를 확인한다 — 훅이 없으면 usage 가 전부 0 이다.

    그 상태에서 cost_recorder 는 usage_logs 행을 만들지 않는다.
    """
    _out, usage = await _drain_responses(_TRUNCATED, tokenizer_hook=None)
    assert usage is not None
    assert usage.output_tokens == 0 and usage.total_tokens == 0


async def test_the_hook_estimates_output_tokens_on_a_truncated_stream():
    """⚠️ 핵심. 종결 이벤트 없이 끊긴 스트림도 기록 가능한 usage 를 남겨야 한다."""

    async def hook(text: str) -> int:
        assert "hello world" in text, f"누적 텍스트가 전달되지 않았다: {text!r}"
        return 7

    _out, usage = await _drain_responses(_TRUNCATED, tokenizer_hook=hook)
    assert usage.output_tokens == 7, f"역산이 반영되지 않았다: {usage}"
    assert usage.total_tokens == usage.input_tokens + 7
    assert usage.estimated is True, "추정치인데 estimated 플래그가 서지 않았다"


async def test_the_estimate_preserves_input_and_cache_buckets():
    """⚠️ response.incomplete/failed 는 input+cache 를 담고 output 만 0 인 경우가 있다.

    output 만으로 TokenUsage 를 새로 만들면 캐시 버킷이 지워져 지금보다 **더** 과소청구된다.
    """
    frames = [
        _raw({"type": "response.created", "response": {"id": "r1"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "partial"}),
        _raw(
            {
                "type": "response.incomplete",
                "response": {
                    "id": "r1",
                    "status": "incomplete",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 0,
                        "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 25},
                    },
                },
            }
        ),
    ]

    async def hook(_text: str) -> int:
        return 9

    _out, usage = await _drain_responses(frames, tokenizer_hook=hook)
    assert usage.output_tokens == 9
    assert usage.cache_read_input_tokens == 40, f"캐시 read 버킷이 지워졌다: {usage}"
    assert usage.cache_creation_input_tokens == 25, f"캐시 write 버킷이 지워졌다: {usage}"
    assert usage.input_tokens > 0, "input 이 지워졌다"


async def test_a_real_usage_event_is_not_overwritten_by_the_estimate():
    """⚠️ 대조군. 실제 usage 를 받았으면 추정치로 덮지 않아야 한다."""
    frames = [
        _raw({"type": "response.created", "response": {"id": "r1"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "text"}),
        _raw(
            {
                "type": "response.completed",
                "response": {
                    "id": "r1",
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 33},
                },
            }
        ),
    ]

    async def hook(_text: str) -> int:
        return 999

    _out, usage = await _drain_responses(frames, tokenizer_hook=hook)
    assert usage.output_tokens == 33, f"실제 usage 가 추정치로 덮였다: {usage}"
    assert usage.estimated is False


async def test_a_failing_hook_does_not_break_the_stream():
    """토크나이저 실패로 응답을 깨뜨리는 것은 거래가 성립하지 않는다."""

    async def boom(_text: str) -> int:
        raise RuntimeError("tokenizer down")

    out, usage = await _drain_responses(_TRUNCATED, tokenizer_hook=boom)
    assert len(out) == 2
    assert usage.output_tokens == 0  # 추정 실패 → 기존 동작으로 되돌아간다


# ─────────────────────────────────────────────────────────────────────────────
# 3. 배선 — **순서까지** 본다
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 2000, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def _func(tree: ast.Module, name: str):
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    assert fn is not None, f"{name} 를 찾지 못했다"
    return fn


def _hook_kwarg_uses(fn, kwarg: str) -> list[tuple[int, str]]:
    """``kwarg=<name>`` 로 전달되는 지점들의 ``(줄번호, 이름)``."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == kwarg and isinstance(kw.value, ast.Name):
                out.append((node.lineno, kw.value.id))
    return out


def _def_line(fn, name: str) -> int | None:
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno
    return None


@pytest.mark.parametrize(
    ("router", "func"),
    [
        ("routers/openai_compat.py", "_handle_responses"),
        ("routers/messages.py", "messages"),
    ],
)
def test_every_tokenizer_hook_is_defined_before_it_is_passed(router: str, func: str):
    """⚠️ 이 검사가 이 파일에 있는 이유.

    앞선 수정에서 "함수 안에 정의돼 있다" 만 확인하는 AST 검사를 썼다가 **두 번** 물렸다:
    훅 정의가 웹서치 분기보다 뒤에 있어서 그 경로에서 UnboundLocalError 였다. 웹서치 분기는
    아래 ``if is_stream:`` 보다 먼저 리턴하므로, 정의가 그 안에 있으면 바인딩되지 않는다.

    정의 존재와 그 지점에서의 바인딩은 다른 주장이다 — 그래서 **줄 순서**를 본다.
    """
    fn = _func(_tree(router), func)
    uses = _hook_kwarg_uses(fn, "tokenizer_hook")
    assert uses, f"{func}: tokenizer_hook 을 넘기는 곳이 없다"
    for use_line, name in uses:
        dline = _def_line(fn, name)
        assert dline is not None, f"{func} L{use_line}: {name} 정의를 찾지 못했다"
        assert dline < use_line, (
            f"{func}: {name} 정의(L{dline})가 사용(L{use_line})보다 뒤에 있다 — "
            "그 경로에서 UnboundLocalError 다"
        )


def test_the_responses_helper_accepts_the_hook():
    fn = _func(_tree("services/streaming.py"), "responses_sse_stream")
    kwonly = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
    assert "tokenizer_hook" in kwonly


def test_the_web_search_loop_forwards_the_hook_to_both_pass_through_helpers():
    """패스스루 경로도 같은 노출이 있다 — 한쪽만 배선하면 그 경로만 조용히 0 이 된다."""
    fn = _func(_tree("services/web_search_loop.py"), "run_web_search_loop")
    forwarded = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and any(kw.arg == "tokenizer_hook" for kw in node.keywords)
    ]
    assert len(forwarded) >= 3, (
        f"루프가 훅을 {len(forwarded)}곳에만 넘긴다(L{forwarded}) — 패스스루 2경로 × "
        "방언 분기를 모두 덮어야 한다"
    )
