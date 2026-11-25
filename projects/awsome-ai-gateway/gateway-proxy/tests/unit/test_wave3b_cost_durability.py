# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""wave 3b — 비용 내구성: 요청 수명 원장(start/close), spool → DB 폴백, 종료 순서."""
from __future__ import annotations

import ast
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.resilience.cost_event_store import CostEventStore
from app.resilience.cost_stream_spool import CostStreamSpool
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
    TokenUsage,
)
from app.services import cost_ledger
from app.services.cost_recorder import CostRecorder


def _auth(team="t1"):
    return AuthContext(user_id="u1", team_id=team, dept_id="", roles=[Role.USER],
                       auth_type=AuthType.VIRTUAL_KEY, key_id="k1", key_prefix="vk-abc")


def _model():
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-x", alias="claude-x",
        provider=ProviderType.BEDROCK, api_format=ApiFormat.BEDROCK_NATIVE, endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )


class _Store:
    def __init__(self, fail: int = 0):
        self.rows: list[str] = []
        self.fail = fail

    async def write(self, payloads):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("db down")
        self.rows.extend(payloads)


@pytest.fixture
def ledger():
    """XADD 를 기록하는 Redis 가짜 + 실제 CostRecorder/spool 로 원장을 구성한다."""
    redis = MagicMock()
    redis.xadd = AsyncMock()
    spool = CostStreamSpool(stream_key="cost:stream")
    rec = CostRecorder(spool=spool)
    cost_ledger.configure(recorder=rec, redis=redis, enabled=True)
    yield redis, spool, rec
    cost_ledger.reset()


def _published(redis):
    return [json.loads(c.args[1]["payload"]) for c in redis.xadd.await_args_list]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ── 요청 수명 원장 ─────────────────────────────────────────────────────────────────────

async def test_start_then_no_row_sends_close_at_request_end(ledger):
    redis, _, _ = ledger
    tok = cost_ledger.bind("req-1", "/v1/messages")
    state = {"rate_limit_state": {"estimated_input": 1000, "budget_reservation": [
        {"scope": "user", "usage_key": "budget:user:{u1}:2026-11", "marker_key": "m",
         "reserved": Decimal("0.5"), "marker_ttl": 14400}]}, "client": "claude-code"}
    await cost_ledger.publish_start(state=state, auth_context=_auth(), model_config=_model(),
                                    request_id="req-1", body={"stream": True})
    cost_ledger.request_done(tok)
    await _settle()
    evs = _published(redis)
    assert [e["kind"] for e in evs] == ["start", "close"]
    st = evs[0]
    assert st["request_id"] == "req-1" and st["instance_id"] == cost_ledger.INSTANCE_ID
    assert st["estimated_input_tokens"] == 1000 and st["estimated_cost_usd"] == "0.003000"
    assert st["is_streaming"] is True and st["client"] == "claude-code"
    assert st["dept_id"] == "00000000-0000-4000-a000-000000000002"  # VK 센티널(워커가 팀에서 해석)
    assert st["budget_reservation"][0]["reserved"] == "0.5"
    assert st["key_prefix"] == "vk-abc" and st["schema_version"] == 4


async def test_finalized_request_sends_no_close(ledger):
    redis, _, rec = ledger
    tok = cost_ledger.bind("req-2", "/v1/messages")
    redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    redis.exists = AsyncMock(return_value=1)
    await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                    request_id="req-2")
    await rec.finalize(redis, _auth(), _model(),
                       TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                       request_id="req-2", is_stream=False, duration_ms=5)
    cost_ledger.request_done(tok)
    await _settle()
    kinds = [e.get("kind", "end") for e in _published(redis)]
    assert kinds == ["start", "end"]


async def test_zero_usage_success_sends_close(ledger):
    """0 토큰 성공은 행을 만들지 않는다 → close 가 시작을 닫아야 sweeper 가 추정 행을 안 만든다."""
    redis, _, rec = ledger
    tok = cost_ledger.bind("req-3")
    await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                    request_id="req-3")
    await rec.finalize(redis, _auth(), _model(), TokenUsage(), request_id="req-3",
                       is_stream=False, duration_ms=1)
    cost_ledger.request_done(tok)
    await _settle()
    assert [e.get("kind", "end") for e in _published(redis)] == ["start", "close"]


async def test_spawned_drain_holds_request_open_until_it_finalizes(ledger):
    """disconnect 드레인(_spawn)이 끝나기 전에 close 가 나가면 안 된다.

    그 드레인이 end 를 보낸다.
    """
    from app.services.streaming import _spawn

    redis, _, rec = ledger
    redis.eval = AsyncMock(return_value=b'{"threshold_triggered":null}')
    redis.exists = AsyncMock(return_value=1)
    gate = asyncio.Event()

    async def _drain():
        await gate.wait()
        await rec.finalize(redis, _auth(), _model(),
                           TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                           request_id="req-4", is_stream=True, duration_ms=5)

    async def _request():
        tok = cost_ledger.bind("req-4")
        await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                        request_id="req-4")
        t = _spawn(_drain())
        cost_ledger.request_done(tok)
        return t

    t = await asyncio.create_task(_request())
    await _settle()
    assert [e.get("kind", "end") for e in _published(redis)] == ["start"]  # 아직 진행 중
    gate.set()
    await t
    await _settle()
    assert [e.get("kind", "end") for e in _published(redis)] == ["start", "end"]


async def test_spawned_task_without_row_closes_after_it_ends(ledger):
    from app.services.streaming import _spawn

    redis, _, _ = ledger

    async def _request():
        tok = cost_ledger.bind("req-5")
        await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                        request_id="req-5")
        t = _spawn(asyncio.sleep(0.05))
        cost_ledger.request_done(tok)
        return t

    t = await asyncio.create_task(_request())
    await _settle()
    assert [e["kind"] for e in _published(redis)] == ["start"]
    await t
    await _settle()
    assert [e["kind"] for e in _published(redis)] == ["start", "close"]


async def test_no_start_without_request_boundary_or_when_disabled(ledger):
    redis, _, _ = ledger
    await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                    request_id="req-6")  # bind 없음
    cost_ledger.CONFIG.enabled = False
    tok = cost_ledger.bind("req-7")
    await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                    request_id="req-7")
    cost_ledger.request_done(tok)
    await _settle()
    assert _published(redis) == []


async def test_start_goes_to_spool_when_redis_degraded(ledger):
    from app.schemas.domain import DegradationLevel

    redis, spool, rec = ledger
    dm = MagicMock()
    dm.level = DegradationLevel.REDIS_DEGRADED
    cost_ledger.configure(recorder=rec, redis=redis, degradation_manager=dm)
    tok = cost_ledger.bind("req-8")
    await cost_ledger.publish_start(state={}, auth_context=_auth(), model_config=_model(),
                                    request_id="req-8")
    cost_ledger.request_done(tok)
    await _settle()
    redis.xadd.assert_not_awaited()
    assert [json.loads(p)["kind"] for p in spool._buf] == ["start", "close"]


async def test_enforce_wrapper_publishes_start_only_when_admitted(ledger, monkeypatch):
    from app.services import rate_limit_enforcement as rle

    redis, _, _ = ledger
    calls = []
    monkeypatch.setattr(rle, "_enforce_rate_limits", AsyncMock(side_effect=[None, "429"]))
    monkeypatch.setattr(cost_ledger, "publish_start",
                        AsyncMock(side_effect=lambda **k: calls.append(k)))
    kw = dict(redis=redis, auth_context=_auth(), model_config=_model(), body={}, state={},
              request_id="r")
    assert await rle.enforce_rate_limits(**kw) is None
    assert await rle.enforce_rate_limits(**kw) == "429"
    assert len(calls) == 1 and calls[0]["request_id"] == "r"


async def test_heartbeat_sets_and_clears_key():
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    hb = cost_ledger.Heartbeat(redis, interval=0.01, ttl=45)
    hb.start()
    await asyncio.sleep(0.03)
    await hb.stop(clear=True)
    key = "cost:live:" + cost_ledger.INSTANCE_ID
    assert redis.set.await_args.args[0] == key and redis.set.await_args.kwargs["ex"] == 45
    redis.delete.assert_awaited_with(key)


# ── spool → DB 폴백 ──────────────────────────────────────────────────────────────────

async def test_flush_to_store_success_and_failure_keep_order():
    spool = CostStreamSpool(stream_key="cost:stream")
    for i in range(5):
        spool.enqueue(f"p{i}")
    store = _Store(fail=1)
    assert await spool.flush_to_store(store, max_items=3) == 0  # DB 실패 → 되돌림
    assert list(spool._buf) == ["p0", "p1", "p2", "p3", "p4"]
    assert await spool.flush_to_store(store, max_items=3) == 3
    assert await spool.flush_to_store(store, max_items=3) == 2
    assert store.rows == ["p0", "p1", "p2", "p3", "p4"] and spool.size == 0


async def test_flush_to_store_cancel_rebuffers():
    spool = CostStreamSpool(stream_key="cost:stream")
    spool.enqueue("a")
    spool.enqueue("b")

    class _Hang:
        async def write(self, payloads):
            await asyncio.sleep(10)

    t = asyncio.create_task(spool.flush_to_store(_Hang()))
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert list(spool._buf) == ["a", "b"]


async def test_store_writer_moves_failed_xadd_to_db_quickly():
    """Redis 장애 중 finalize — XADD 실패분이 ms 안에 DB 폴백 표로 간다(파드 종료에 살아남는다)."""
    spool = CostStreamSpool(stream_key="cost:stream")
    store = _Store()
    writer = asyncio.create_task(spool.run_store_writer(store, interval=5.0))
    await asyncio.sleep(0)
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=ConnectionError("redis down"))
    rec = CostRecorder(spool=spool)
    assert await rec.publish_event(redis, '{"request_id":"x1"}', request_id="x1") is False
    await asyncio.sleep(0.05)  # interval(5 s) 보다 훨씬 짧다 — enqueue 가 깨운다
    assert store.rows == ['{"request_id":"x1"}'] and spool.size == 0
    writer.cancel()


async def test_cost_event_store_inserts_rows_with_metadata():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    metrics = MagicMock()
    store = CostEventStore(MagicMock(return_value=session), metrics=metrics)
    await store.write(['{"kind":"start","request_id":"a"}', '{"request_id":"b"}', "garbage\x00"])
    params = session.execute.await_args.args[1]
    assert [(p["request_id"], p["kind"]) for p in params] == [("a", "start"), ("b", "end"),
                                                             (None, "end")]
    assert params[2]["payload"] == "garbage"
    session.commit.assert_awaited_once()
    metrics.cost_events_db_fallback_total.add.assert_called_with(3, {"outcome": "written"})


async def test_cost_event_store_failure_raises_and_counts():
    session = MagicMock()
    session.execute = AsyncMock(side_effect=OSError("db down"))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    metrics = MagicMock()
    store = CostEventStore(MagicMock(return_value=session), metrics=metrics)
    with pytest.raises(OSError):
        await store.write(["{}"])
    metrics.cost_events_db_fallback_total.add.assert_called_with(1, {"outcome": "failed"})


# ── 종료 순서: 스트림 드레인 → spool ─────────────────────────────────────────────────────

def _lifespan_calls_after_yield():
    src = (Path(__file__).resolve().parents[2] / "src" / "app" / "main.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    y = next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Yield))
    calls: dict[str, list[int]] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and n.lineno > y:
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            calls.setdefault(name, []).append(n.lineno)
    return calls


def test_shutdown_drains_stream_tasks_before_the_spool():
    c = _lifespan_calls_after_yield()
    src = (Path(__file__).resolve().parents[2] / "src" / "app" / "main.py").read_text()
    lines = src.splitlines()

    def line_of(snippet: str) -> int:
        return next(i + 1 for i, ln in enumerate(lines) if snippet in ln)

    drain = min(c["drain_pending_tasks"])
    assert drain < min(c["_flush_cost_spool"]), "spool 을 스트림 드레인보다 먼저 비운다"
    # 헬스체커(복구 시 spool 재발행)는 드레인 동안 살아 있다.
    assert drain < line_of("await health_checker.stop()")
    # 마지막 spool 비우기 → 생존 신호 삭제 → Redis 닫기.
    assert max(c["_flush_cost_spool"]) < line_of("await cost_heartbeat.stop(") < line_of(
        "await redis.aclose()")


def test_shutdown_stream_wait_covers_the_drain_timeout():
    from app.config import Settings

    s = Settings()
    wait = min(float(s.stream_disconnect_drain_timeout) + s.shutdown_stream_drain_margin_sec,
               max(0.5, s.shutdown_lifespan_budget_sec - 5.0))
    assert wait >= s.stream_disconnect_drain_timeout
    assert s.shutdown_lifespan_budget_sec == 40.0  # 차트 산식(15 + graceful + 40 + 5)과 같다


async def test_late_finalize_from_drain_reaches_db_after_ordered_shutdown():
    """예전 순서의 결함 재현: 드레인이 spool 비우기 뒤에 finalize 하면 그 이벤트가 버려졌다.

    새 순서(드레인 대기 → spool 비우기)에서는 늦은 finalize 의 XADD 실패분도 DB 폴백 표에 간다.
    """
    from app.main import _flush_cost_spool
    from app.services.streaming import _spawn, drain_pending_tasks

    spool = CostStreamSpool(stream_key="cost:stream")
    store = _Store()
    redis = MagicMock()
    redis.xadd = AsyncMock(side_effect=ConnectionError("redis gone"))
    rec = CostRecorder(spool=spool)

    async def _drain():
        await asyncio.sleep(0.2)  # 상류를 끝까지 읽는 중
        await rec.publish_event(redis, '{"request_id":"late"}', request_id="late")

    _spawn(_drain())
    assert await drain_pending_tasks(timeout=5.0) >= 1
    await _flush_cost_spool(spool, redis, store, redis_timeout=1.0, remaining=lambda: 2.0)
    assert store.rows == ['{"request_id":"late"}'] and spool.size == 0


async def test_flush_cost_spool_prefers_redis_then_db():
    from app.main import _flush_cost_spool

    spool = CostStreamSpool(stream_key="cost:stream")
    spool.enqueue("a")
    spool.enqueue("b")
    ok = MagicMock()
    ok.xadd = AsyncMock()
    store = _Store()
    await _flush_cost_spool(spool, ok, store, redis_timeout=1.0, remaining=lambda: 1.0)
    assert ok.xadd.await_count == 2 and store.rows == []
    spool.enqueue("c")
    bad = MagicMock()
    bad.xadd = AsyncMock(side_effect=ConnectionError())
    await _flush_cost_spool(spool, bad, store, redis_timeout=1.0, remaining=lambda: 1.0)
    assert store.rows == ["c"] and spool.size == 0
