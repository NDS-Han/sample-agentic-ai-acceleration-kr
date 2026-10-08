# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregator 회귀 테스트 (F10).

예전엔 ``ON CONFLICT ... DO NOTHING`` 이라, cron(KST 00:10) 이후 스트림
백로그/재처리로 늦게 INSERT 된 어제분 usage_logs 가 재실행돼도 합산되지 않아
daily_aggregates 에 영구 누락됐다. 수정 후 어제 윈도우를 같은 트랜잭션에서
DELETE → 전체 재집계한다.

실 Postgres 없이 검증 가능한 부분:
  * 어제 집계가 DELETE(윈도우 재계산)를 먼저 실행한다
  * 집계 SQL 이 충돌키 (date, user_id, model_alias) 와 같은 입도로 GROUP BY 한다
    (team_id/dept_id 를 넣으면 팀 이동자가 자체 충돌로 조용히 유실된다)
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.daily_aggregator import (
    _AGG_SQL,
    _BACKFILL_SQL,
    _DELETE_WINDOW_SQL,
    aggregate_yesterday,
)


@pytest.mark.asyncio
async def test_aggregate_yesterday_deletes_window_then_reaggregates():
    """늦게 도착한 행이 반영되려면 재실행이 기존 행을 먼저 비워야 한다."""
    session = AsyncMock()
    calls: list[str] = []

    async def _execute(stmt, params=None):
        calls.append(str(stmt))
        r = MagicMock()
        r.rowcount = 3
        return r

    session.execute = AsyncMock(side_effect=_execute)
    inserted = await aggregate_yesterday(session)

    assert inserted == 3
    assert len(calls) == 2
    assert "DELETE FROM usage.daily_aggregates" in calls[0]
    assert "INSERT INTO usage.daily_aggregates" in calls[1]
    session.commit.assert_awaited_once()


def test_agg_sql_groups_by_conflict_key_only():
    """GROUP BY 가 (date, user_id, model_alias) 유니크 키와 같은 입도여야 한다.

    team_id/dept_id 를 GROUP BY 에 넣으면 같은 (date, user, model) 에 두 그룹이
    생겨 유니크 제약에 자체 충돌하고 한 행이 조용히 유실된다 — 대신 array_agg …
    ORDER BY requested_at DESC 로 그날 마지막 소속을 귀속시킨다.
    """
    group_by = _AGG_SQL.split("GROUP BY")[-1]
    assert "team_id" not in group_by and "dept_id" not in group_by
    assert "user_id" in group_by and "model_alias" in group_by
    # 충돌 무시로 되돌아가면 안 된다 — 재집계는 DELETE+INSERT 로 한다.
    assert "DO NOTHING" not in _AGG_SQL and "DO UPDATE" not in _AGG_SQL
    assert "array_agg(team_id ORDER BY requested_at DESC)" in _AGG_SQL


def test_delete_window_sql_targets_local_yesterday():
    assert re.search(
        r"DELETE FROM usage\.daily_aggregates WHERE date = :day_local",
        _DELETE_WINDOW_SQL,
    )


def test_backfill_still_conflict_safe_and_same_grain():
    """첫 기동 백필은 빈 테이블에만 도는 경로 — DO NOTHING 유지 + 같은 입도."""
    assert "ON CONFLICT" in _BACKFILL_SQL
    group_by = _BACKFILL_SQL.split("GROUP BY")[-1]
    assert "team_id" not in group_by and "dept_id" not in group_by
