# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""하이브리드 롤업 읽기(services/usage_rollup) 용 테스트 대역.

  * ``EmptyAggRow`` — 집계 한 행의 '빈' 값(JSON 집계 열은 '[]', 메타는 없음, 나머지 0).
  * ``recording_session()`` — 실행된 (SQL 텍스트, 바인드) 를 모아 두는 세션. DB 를 건드리지 않는다.
  * ``DATA_TABLES`` / ``team_isolated(sql, params, teams)`` — 사용량을 담은 테이블을 읽는 문장이
    **모든 분기**에서 팀 조건을 걸고, 바인드된 팀 집합이 기대와 같은지.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

DATA_TABLES = ("usage.usage_logs", "usage.daily_aggregates", "usage.hourly_aggregates",
               "usage.monthly_aggregates")
_TEAM_PRED = "team_id = ANY(CAST(:r_teams AS uuid[]))"


class EmptyAggRow:
    _JSON = {"by_model", "by_team", "by_user", "trends", "by_team_day", "models", "cells"}

    def __init__(self, **values):
        self.__dict__.update(values)

    def __getattr__(self, name):
        if name in self._JSON:
            return "[]"
        if name == "delayed":
            return []
        if name in ("as_of", "ws_cost", "lo", "hi"):
            return None
        return 0


def empty_result(row=None):
    result = MagicMock()
    result.one.return_value = row if row is not None else EmptyAggRow()
    result.one_or_none.return_value = None
    result.scalar_one.return_value = 0
    result.scalar.return_value = 0
    result.all.return_value = []
    result.first.return_value = None
    result.__iter__ = MagicMock(return_value=iter([]))
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def recording_session(responder=None):
    """(session, seen) — seen 은 (sql, params) 목록. responder(sql, params) 가 결과를 고를 수 있다."""
    seen: list[tuple[str, dict]] = []

    async def _execute(stmt, params=None, *args, **kwargs):
        sql = str(stmt)
        p = dict(params or {})
        if not p and hasattr(stmt, "compile"):
            try:
                p = dict(stmt.compile().params)
            except Exception:  # noqa: BLE001
                p = {}
        seen.append((sql, p))
        if responder is not None:
            r = responder(sql, p)
            if r is not None:
                return r
        return empty_result()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.rollback = AsyncMock()
    return session, seen


#: 첫 사용량 날짜(min(requested_at)) — 인덱스 끝점 조회, 사용량 값을 돌려주지 않아 격리 대상이 아니다.
_FIRST_DAY = "SELECT (min(requested_at) AT TIME ZONE :r_tz)::date AS d FROM usage.usage_logs"


def data_branches(sql: str) -> int:
    sql = sql.replace(_FIRST_DAY, "")
    return sum(len(re.findall(re.escape(t) + r"\b", sql)) for t in DATA_TABLES)


def team_isolated(sql: str, params: dict, teams) -> bool:
    """사용량 테이블을 읽는 모든 분기에 팀 조건이 있고, 바인드된 팀이 정확히 teams 인가."""
    n = data_branches(sql)
    if n == 0:
        return True
    return (sql.count(_TEAM_PRED) >= n
            and sorted(params.get("r_teams", [])) == sorted(str(t) for t in teams))


def unscoped(sql: str) -> bool:
    return data_branches(sql) > 0 and ":r_teams" not in sql
