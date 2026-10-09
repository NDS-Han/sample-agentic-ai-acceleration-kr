# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""daily_aggregator 회귀 테스트 (F10 + R2-3).

예전엔 ``ON CONFLICT ... DO NOTHING`` 이라, cron(KST 00:10) 이후 스트림
백로그/재처리로 늦게 INSERT 된 어제분 usage_logs 가 재실행돼도 합산되지 않아
daily_aggregates 에 영구 누락됐다(F10). 한동안 DELETE+재집계로 고쳤는데 그건
멀티-replica 크론 동시 실행에서 같은 윈도우를 지우고 넣다가 유니크 키에 깨져
매일 N-1 pod 가 UniqueViolation 을 남겼다(R2-3). 이제 윈도우 전체 재집계를
``ON CONFLICT (date, user_id, model_alias) DO UPDATE`` 로 덮어쓴다 — 늦게 온
행도 반영되면서 같은 키는 행 잠금으로 직렬화돼 replica 동시 실행도 안전하다.

실 Postgres 없이 검증 가능한 부분:
  * 어제 집계가 DELETE 없이 단일 UPSERT 한 번으로 끝난다
  * DO UPDATE 가 모든 비-키 컬럼을 EXCLUDED 로 덮어쓴다(늦은 행 반영)
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
    aggregate_yesterday,
)


@pytest.mark.asyncio
async def test_aggregate_yesterday_upserts_window():
    """늦게 도착한 행이 반영되고 replica 동시 실행이 안전하려면 단일 UPSERT 여야 한다."""
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
    assert len(calls) == 1
    assert "INSERT INTO usage.daily_aggregates" in calls[0]
    assert "ON CONFLICT" in calls[0] and "DO UPDATE" in calls[0]
    assert "DELETE" not in calls[0]
    session.commit.assert_awaited_once()


def test_agg_sql_groups_by_conflict_key_only():
    """GROUP BY 가 (date, user_id, model_alias) 유니크 키와 같은 입도여야 한다.

    team_id/dept_id 를 GROUP BY 에 넣으면 같은 (date, user, model) 에 두 그룹이
    생겨 유니크 제약에 자체 충돌하고 한 행이 조용히 유실된다 — 대신 array_agg …
    ORDER BY requested_at DESC 로 그날 마지막 소속을 귀속시킨다.
    """
    group_by = _AGG_SQL.split("GROUP BY")[-1].split("ON CONFLICT")[0]
    assert "team_id" not in group_by and "dept_id" not in group_by
    assert "user_id" in group_by and "model_alias" in group_by
    # 충돌 무시(DO NOTHING)로 되돌아가면 늦게 온 행이 다시 누락된다 — 덮어써야 한다.
    assert "DO NOTHING" not in _AGG_SQL
    assert "DO UPDATE" in _AGG_SQL
    assert "array_agg(team_id ORDER BY requested_at DESC)" in _AGG_SQL


def test_agg_sql_updates_all_non_key_columns():
    """DO UPDATE 가 비-키 컬럼 전부를 EXCLUDED 로 덮어써야 재집계가 반영된다."""
    conflict = _AGG_SQL.split("DO UPDATE SET")[-1]
    for col in (
        "team_id",
        "dept_id",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "total_tokens",
        "total_cost_usd",
        "request_count",
    ):
        assert re.search(rf"{col}\s*=\s*EXCLUDED\.{col}", conflict), col


def test_backfill_still_conflict_safe_and_same_grain():
    """첫 기동 백필은 빈 테이블에만 도는 경로 — DO NOTHING 유지 + 같은 입도."""
    assert "ON CONFLICT" in _BACKFILL_SQL
    group_by = _BACKFILL_SQL.split("GROUP BY")[-1]
    assert "team_id" not in group_by and "dept_id" not in group_by
