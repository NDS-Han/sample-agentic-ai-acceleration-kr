# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ws-history-fix — 백필 계획/입도/CLI 검증(PG 없이)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Asia/Kolkata")
USER = "d1111111-1111-1111-1111-111111111111"
TEAM = "d2222222-2222-2222-2222-22222222222a"
DEPT = "d3333333-3333-3333-3333-333333333333"


def _row(requested_at: datetime, *, count=2, client="claude-code"):
    from worker.backfill_ws_history import TargetRow
    from worker.daily_aggregator import bucket_of
    from worker.web_search_pricing import web_search_cost

    day, hs, he = bucket_of(TZ, requested_at)
    return TargetRow(
        request_id="r1", user_id=USER, team_id=TEAM, dept_id=DEPT,
        model_alias="m", client=client, status="SUCCESS", web_search_count=count,
        requested_at=requested_at,
        cost=web_search_cost(count, Decimal("7.00")),
        reporting_date=day, hour_start=hs, hour_end=he)


def test_target_cost_is_fixed_historical_price_not_current_setting():
    from worker.backfill_ws_history import HISTORICAL_PRICE_PER_1K, _target

    raw = SimpleNamespace(request_id="r", user_id=USER, team_id=TEAM, dept_id=DEPT,
                          model_alias="m", client="cowork", status="SUCCESS",
                          web_search_count=3,
                          requested_at=datetime(2026, 11, 6, 4, 0, tzinfo=UTC))
    row = _target(raw, TZ)
    assert HISTORICAL_PRICE_PER_1K == Decimal("7.00")
    assert row.cost == Decimal("0.021000")
    assert row.reporting_date == date(2026, 11, 6)


def test_plan_scopes_are_user_team_and_per_app_client():
    from worker.backfill_ws_history import Accumulator

    row = _row(datetime(2026, 11, 6, 4, 0, tzinfo=UTC), count=3)
    acc = Accumulator()
    acc.add(row)
    out = acc.summary()
    assert (out["rows"], out["searches"], out["cost_usd"]) == (1, 3, "0.021000")
    scopes = {(x["scope"], x["scope_id"], x["client"]) for x in out["per_scope"]}
    assert scopes == {("USER", USER, None), ("TEAM", TEAM, None),
                      ("USER", USER, "claude-code")}


@pytest.mark.asyncio
async def test_scoped_refresh_updates_only_ws_columns_for_touched_grain():
    from worker.backfill_ws_history import _refresh_ws_rollup_keys

    # Kolkata 2026-11-05 은 이미 끝난 날 — daily+monthly+hourly 입도 각각 한 번씩만 갱신.
    row = _row(datetime(2026, 11, 5, 4, 0, tzinfo=UTC))
    result = SimpleNamespace(rowcount=1)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    stats = await _refresh_ws_rollup_keys(
        session, [row], tz=TZ, now=datetime(2026, 11, 7, tzinfo=UTC))
    assert stats["daily_keys_updated"] == 1
    assert stats["hourly_keys_updated"] == 1
    assert stats["monthly_keys_updated"] == 1
    assert stats["daily_keys_missing"] == 0
    assert session.execute.await_count == 3
    params = session.execute.await_args_list[0].args[1]
    assert params["user_id"] == USER and params["client_key"] == "claude-code"
    assert params["model_alias"] == "m" and params["status"] == "SUCCESS"
    sqls = [str(c.args[0]) for c in session.execute.await_args_list]
    assert all("web_search_count" in sql and "total_cost_usd" not in sql for sql in sqls)


@pytest.mark.asyncio
async def test_open_hour_and_in_progress_day_are_deferred_not_updated():
    from worker.backfill_ws_history import _refresh_ws_rollup_keys

    now = datetime.now(UTC)
    row = _row(now)  # 같은 시간 버킷 — hour_end 가 반드시 now 뒤다
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)))
    stats = await _refresh_ws_rollup_keys(session, [row], tz=TZ, now=now)
    assert stats["daily_keys_deferred"] == 1
    assert stats["monthly_keys_deferred"] == 1
    assert stats["open_hour_keys_deferred"] == 1
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_cli_validates_arguments(monkeypatch):
    from worker import backfill_ws_history as wh

    monkeypatch.setattr(wh, "get_settings",
                        lambda: MagicMock(reporting_timezone="Asia/Kolkata"))
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert await wh.run(["--cutoff", "2026-11-25T11:07:47"]) == 1  # 오프셋 없음
    assert await wh.run(["--cutoff", future]) == 1
    assert await wh.run(["--cutoff", "2026-11-25T11:07:47Z", "--period", "2026-13"]) == 1
    assert await wh.run(["--cutoff", "2026-11-25T11:07:47Z", "--batch-size", "0"]) == 1


def test_tracking_since_parser_accepts_jsonb_shape_and_rejects_naive_or_bad_values():
    from worker.reconciliation import _parse_tracking_since

    aware = "2026-11-25T11:07:47+00:00"
    assert _parse_tracking_since({"since": aware}).isoformat() == aware
    assert _parse_tracking_since('{"since": "2026-11-25T11:07:47Z"}') is not None
    assert _parse_tracking_since({"since": "2026-11-25T11:07:47"}) is None
    assert _parse_tracking_since({"bad": True}) is None
    assert _parse_tracking_since("not-json") is None


def test_new_reconciliation_checks_have_metric_series_from_startup():
    from worker.observability.metrics import RECONCILIATION_CHECKS

    assert "retro_not_charged" in RECONCILIATION_CHECKS
    assert "web_search_cost_gap" in RECONCILIATION_CHECKS
