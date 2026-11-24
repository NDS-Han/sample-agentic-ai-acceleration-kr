# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""usage_logs.web_search_cost_usd — 기록 시점 단가 스냅샷, 예산·한도와 완전 분리.

  - 비용 = web_search_count × 단가/1000 (6자리), 0회 → 0 (NULL 아님 = 추적 중)
  - 단가는 system_settings 에서 TTL 캐시로 읽고, 바뀌면 그 뒤 기록분부터 적용
  - budget_usages UPSERT·일별 카운터·임계 알림은 웹 검색 유무와 무관하게 동일
  - 추적 시작 표지는 처음 기록한 배치의 가장 이른 requested_at 으로 1회
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.batch_flusher import BatchFlusher
from worker.schemas.cost_stream import CostStreamEntry
from worker.web_search_pricing import (
    DEFAULT_PRICE_PER_1K,
    PRICING_KEY,
    TRACKING_SINCE_KEY,
    WebSearchPricing,
    parse_price,
    web_search_cost,
)

pytestmark = pytest.mark.unit


def _entry(rid: str, *, ws: int = 0, cost: str = "0.10", at: str = "2026-11-24T10:00:00+00:00",
           client: str | None = "claude-code") -> CostStreamEntry:
    return CostStreamEntry(
        request_id=rid,
        user_id="00000000-0000-0000-0000-000000000001",
        team_id="00000000-0000-0000-0000-000000000002",
        dept_id="00000000-0000-0000-0000-000000000003",
        model_alias="claude-sonnet-4-6",
        provider="BEDROCK",
        input_tokens=100,
        output_tokens=50,
        web_search_count=ws,
        cost_usd=Decimal(cost),
        latency_ms=200,
        requested_at=at,
        completed_at=at,
        period="2026-11",
        date="2026-11-24",
        client=client,
        threshold_triggered=80 if rid.endswith("-t") else None,
        threshold_scope="USER" if rid.endswith("-t") else None,
    )


class _Recorder:
    """session.execute 를 SQL 종류별로 기록하는 가짜 세션(공유 1개)."""

    def __init__(self) -> None:
        self.inserts: list[dict] = []
        self.upserts: list[list[dict]] = []
        self.other: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        result = MagicMock()
        if "INSERT INTO usage.usage_logs" in sql:
            self.inserts.append(dict(params))
            result.scalar_one_or_none.return_value = params["request_id"]
        elif "budget.budget_usages" in sql and "INSERT" in sql:
            self.upserts.append([dict(p) for p in params])
        else:
            self.other.append(sql)
            result.__iter__ = lambda _self: iter([])  # replay SELECT → 없음
            result.scalar_one_or_none.return_value = None
            result.scalar.return_value = Decimal("0")
        return result


def _flusher(rec: _Recorder, pricing=None) -> tuple[BatchFlusher, MagicMock]:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.execute = AsyncMock(side_effect=rec.execute)
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()
    return (
        BatchFlusher(session_factory=MagicMock(return_value=session), redis=redis,
                     web_search_pricing=pricing),
        redis,
    )


class _FixedPricing:
    def __init__(self, price: str) -> None:
        self.price = Decimal(price)
        self.marked: list[datetime] = []

    async def price_per_1k(self) -> Decimal:
        return self.price

    async def mark_tracking_since(self, at: datetime) -> None:
        self.marked.append(at)


# ── 계산 ────────────────────────────────────────────────────────────────────
def test_cost_math_list_price():
    assert web_search_cost(0, DEFAULT_PRICE_PER_1K) == Decimal("0")
    assert web_search_cost(1, DEFAULT_PRICE_PER_1K) == Decimal("0.007000")
    assert web_search_cost(3, DEFAULT_PRICE_PER_1K) == Decimal("0.021000")
    assert web_search_cost(1000, DEFAULT_PRICE_PER_1K) == Decimal("7.000000")
    assert web_search_cost(7, Decimal("5.5")) == Decimal("0.038500")


def test_cost_clamped_to_column_precision():
    # 카운트는 스키마가 1e12 까지 허용 — NUMERIC(14,6) 오버플로(DataError)로 행이 유실되면 안 된다.
    assert web_search_cost(10**12, Decimal("1000")) == Decimal("99999999.999999")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"price_per_1k_queries": "7.00"}, Decimal("7.00")),
        ({"price_per_1k_queries": 5}, Decimal("5")),
        (json.dumps({"price_per_1k_queries": "6.25"}), Decimal("6.25")),
        ("8", Decimal("8")),
        ({"price_per_1k_queries": "0"}, Decimal("0")),
        ({"price_per_1k_queries": "-1"}, None),
        ({"price_per_1k_queries": "1000.01"}, None),
        ({"price_per_1k_queries": "NaN"}, None),
        ({"price_per_1k_queries": True}, None),
        ({"other": 1}, None),
        ("garbage", None),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


# ── flush: 행별 비용 + 예산 불변 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_flush_writes_cost_with_snapshot_price_and_zero_for_no_search():
    rec = _Recorder()
    pricing = _FixedPricing("7.00")
    flusher, _ = _flusher(rec, pricing)
    await flusher.flush([
        _entry("r-ws", ws=3, at="2026-11-24T10:00:05+00:00"),
        _entry("r-none", ws=0, at="2026-11-24T10:00:01+00:00"),
    ])
    by_id = {p["request_id"]: p for p in rec.inserts}
    assert by_id["r-ws"]["web_search_cost_usd"] == "0.021000"
    # 0 은 "추적 중·검색 없음" — NULL(미추적)과 구별되는 값으로 기록한다.
    assert by_id["r-none"]["web_search_cost_usd"] == "0"
    # 모델 비용은 손대지 않는다.
    assert by_id["r-ws"]["cost_usd"] == "0.10"
    # 추적 시작 표지 = 처음 기록한 배치의 가장 이른 requested_at.
    assert pricing.marked == [datetime.fromisoformat("2026-11-24T10:00:01+00:00")]


@pytest.mark.asyncio
async def test_flush_without_provider_uses_list_price():
    rec = _Recorder()
    flusher, _ = _flusher(rec, None)
    await flusher.flush([_entry("r1", ws=2)])
    assert rec.inserts[0]["web_search_cost_usd"] == "0.014000"


async def _budget_side_effects(entries: list[CostStreamEntry]):
    rec = _Recorder()
    flusher, redis = _flusher(rec, _FixedPricing("7.00"))
    await flusher.flush(entries)
    pipe = redis.pipeline.return_value
    counters = sorted(
        (c[0], tuple(str(a) for a in c.args)) for c in pipe.method_calls
    )
    published = [c.args for c in redis.publish.await_args_list]
    return rec.upserts, counters, published


@pytest.mark.asyncio
async def test_budget_and_limits_identical_with_and_without_web_search():
    """회귀 방지 — 웹 검색 비용은 budget_usages(사용자·팀·앱별)·일별 카운터·임계 알림
    어디에도 들어가지 않는다. 같은 요청에 검색 횟수만 다르게 해도 결과가 같아야 한다."""
    plain = [_entry("a"), _entry("b", cost="0.25"), _entry("c-t", cost="0.05", client="cowork")]
    searched = [_entry("a", ws=40), _entry("b", ws=1, cost="0.25"),
                _entry("c-t", ws=999, cost="0.05", client="cowork")]
    base = await _budget_side_effects(plain)
    upserts, counters, published = base
    # 공허한 통과 방지 — 비교 대상이 실제로 채워져 있어야 한다.
    assert upserts and counters and published
    assert await _budget_side_effects(searched) == base


@pytest.mark.asyncio
async def test_budget_upsert_amounts_are_model_cost_only():
    rec = _Recorder()
    flusher, _ = _flusher(rec, _FixedPricing("7.00"))
    await flusher.flush([_entry("x", ws=100, cost="0.40")])  # 검색비 $0.70
    amounts = {(p["scope"], p["client"]): p["cost"] for batch in rec.upserts for p in batch}
    assert amounts == {("USER", None): "0.40", ("TEAM", None): "0.40",
                       ("USER", "claude-code"): "0.40"}


# ── 단가 공급자: TTL 스냅샷, 실패 폴백, 표지 1회 ────────────────────────────────────
class _SettingsDb:
    def __init__(self) -> None:
        self.price_value = None
        self.fail = False
        self.marks: list[dict] = []
        self.commits = 0

    def factory(self):
        db = self

        class _S:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return None

            async def execute(self_inner, stmt, params=None):
                if db.fail:
                    raise RuntimeError("db down")
                sql = str(stmt)
                r = MagicMock()
                if "SELECT value" in sql:
                    assert params == {"key": PRICING_KEY}
                    r.scalar_one_or_none.return_value = db.price_value
                else:
                    assert params["key"] == TRACKING_SINCE_KEY
                    db.marks.append(json.loads(params["value"]))
                return r

            async def commit(self_inner):
                db.commits += 1

        return MagicMock(side_effect=lambda: _S())


@pytest.mark.asyncio
async def test_pricing_snapshot_changes_only_after_ttl():
    db = _SettingsDb()
    now = [0.0]
    p = WebSearchPricing(db.factory(), ttl=30, clock=lambda: now[0])
    assert await p.price_per_1k() == DEFAULT_PRICE_PER_1K  # 행 없음 → 정가
    db.price_value = {"price_per_1k_queries": "5.00"}
    now[0] = 10
    assert await p.price_per_1k() == DEFAULT_PRICE_PER_1K  # 캐시 안
    now[0] = 31
    assert await p.price_per_1k() == Decimal("5.00")  # TTL 뒤 새 단가


@pytest.mark.asyncio
async def test_price_change_does_not_rewrite_earlier_rows():
    """단가 변경 전에 기록된 행은 옛 단가 그대로 — 행마다 고정(이력 보존)."""
    db = _SettingsDb()
    now = [0.0]
    pricing = WebSearchPricing(db.factory(), ttl=30, clock=lambda: now[0])
    rec = _Recorder()
    flusher, _ = _flusher(rec, pricing)
    await flusher.flush([_entry("before", ws=10)])
    db.price_value = {"price_per_1k_queries": "3.00"}
    now[0] = 60
    await flusher.flush([_entry("after", ws=10)])
    by_id = {p["request_id"]: p["web_search_cost_usd"] for p in rec.inserts}
    assert by_id == {"before": "0.070000", "after": "0.030000"}


@pytest.mark.asyncio
async def test_invalid_or_unreadable_price_falls_back():
    db = _SettingsDb()
    now = [0.0]
    p = WebSearchPricing(db.factory(), ttl=30, clock=lambda: now[0])
    db.price_value = {"price_per_1k_queries": "-3"}
    assert await p.price_per_1k() == DEFAULT_PRICE_PER_1K
    db.price_value = {"price_per_1k_queries": "4"}
    now[0] = 40
    assert await p.price_per_1k() == Decimal("4")
    db.fail = True
    now[0] = 80
    assert await p.price_per_1k() == Decimal("4")  # 조회 실패 → 마지막 단가 유지
    fresh = WebSearchPricing(db.factory(), ttl=30, clock=lambda: 0.0)
    assert await fresh.price_per_1k() == DEFAULT_PRICE_PER_1K  # 아는 값이 없으면 정가


@pytest.mark.asyncio
async def test_tracking_marker_written_once_and_failure_is_retried():
    db = _SettingsDb()
    p = WebSearchPricing(db.factory())
    db.fail = True
    await p.mark_tracking_since(datetime.fromisoformat("2026-11-24T00:00:00+00:00"))  # 삼킨다
    assert db.marks == []
    db.fail = False
    await p.mark_tracking_since(datetime.fromisoformat("2026-11-24T01:00:00+00:00"))
    await p.mark_tracking_since(datetime.fromisoformat("2026-11-24T02:00:00+00:00"))
    assert db.marks == [{"since": "2026-11-24T01:00:00+00:00"}]
    assert db.commits == 1
