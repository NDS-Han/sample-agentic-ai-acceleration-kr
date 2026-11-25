# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""화면별 사용량 집계 — 전부 ``usage_rollup`` 하이브리드 소스 `x` 위의 SQL 이다.

숫자의 정의(core/usage_filters — wave 3 DECISION 1/2):
  - 비용·토큰·요청 수·활성 사용자 = **청구 대상(billable) 행, 상태 무관** — 프로바이더가 처리한
    실패(ERROR/TIMEOUT)·취소(CANCELLED) 건의 청구분도 합산한다. 요청 수 = 계량 행 수(rq).
  - 청구 총액(Total charged) = 모델 비용(성공 + 실패 + 취소) + 웹 검색 비용 — 분해가 같이 나간다.
  - 상태별 호출 수(성공·에러·취소)는 count(*) 그대로(모니터링 정의).
  - 웹 검색 수·비용·미추적 수 = 전 status(실패 요청의 검색도 실행·과금된다)
  - 팀 귀속 = 요청 시점 usage_logs.team_id(롤업 입도에 그대로 있다)
  - 앱 = COALESCE(client,'other')(롤업은 이미 접혀 있다)

JSON 집계는 ``::text`` 로 받아 ``parse_float=Decimal`` 로 푼다 — 비용이 float 으로 반올림되지 않게.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.usage_filters import BILLABLE_GRAIN_SQL
from app.services import usage_rollup as ur

_S = "status = 'SUCCESS'"
_E = "status IN ('ERROR', 'TIMEOUT')"
_C = "status = 'CANCELLED'"
#: 청구 대상 입도 행(상태 무관) — core/usage_filters 의 단일 정의.
_B = BILLABLE_GRAIN_SQL


def _j(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (bytes, str)):
        return json.loads(v, parse_float=Decimal)
    return v


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


# ── 합계 ─────────────────────────────────────────────────────────────────────

_TOTALS_SELECT = f"""
SELECT
  COALESCE(sum(n) FILTER (WHERE {_S}), 0)::bigint AS total_requests,
  COALESCE(sum(n) FILTER (WHERE {_E}), 0)::bigint AS error_requests,
  COALESCE(sum(n) FILTER (WHERE {_C}), 0)::bigint AS cancelled_requests,
  COALESCE(sum(rq), 0)::bigint AS billable_requests,
  COALESCE(sum(c), 0) AS total_cost,
  COALESCE(sum(c) FILTER (WHERE {_S}), 0) AS success_cost,
  COALESCE(sum(c) FILTER (WHERE {_E}), 0) AS failed_cost,
  COALESCE(sum(c) FILTER (WHERE {_C}), 0) AS cancelled_cost,
  count(DISTINCT user_id) FILTER (WHERE {_B}) AS active_users,
  COALESCE(sum(i), 0)::bigint AS input_tokens,
  COALESCE(sum(o), 0)::bigint AS output_tokens,
  COALESCE(sum(cw), 0)::bigint AS cache_creation_tokens,
  COALESCE(sum(cr), 0)::bigint AS cache_read_tokens,
  COALESCE(sum(wq), 0)::bigint AS ws_queries,
  sum(wc) AS ws_cost,
  COALESCE(sum(wu), 0)::bigint AS ws_untracked
FROM x
"""


@dataclass
class Totals:
    #: 상태별 호출 수(count(*)) — SUCCESS / ERROR+TIMEOUT / CANCELLED.
    total_requests: int
    error_requests: int
    #: 모델 토큰 비용 합 — 청구 대상 전 상태(DECISION 1). 웹 검색은 ws_cost 로 따로.
    total_cost: Decimal
    active_users: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    ws_queries: int
    ws_cost: Decimal | None
    ws_untracked: int
    cancelled_requests: int = 0
    #: 청구 대상 호출 수(계량 행 — 토큰·비용 > 0, 상태 무관).
    billable_requests: int = 0
    success_cost: Decimal = Decimal("0")
    failed_cost: Decimal = Decimal("0")
    cancelled_cost: Decimal = Decimal("0")

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_creation_tokens
                + self.cache_read_tokens)

    @property
    def charged_total(self) -> Decimal:
        """청구 총액 = 모델 비용(성공+실패+취소) + 웹 검색 비용(미추적 NULL 은 0 으로)."""
        return self.total_cost + (self.ws_cost or Decimal("0"))

    def charged(self) -> dict:
        """Total (charged) 분해 — UI 카드와 응답 공용(float, 6자리). 웹 검색 None = 미추적."""
        return charged_breakdown(self.success_cost, self.failed_cost, self.cancelled_cost,
                                 self.ws_cost)


def charged_breakdown(success: Decimal, failed: Decimal, cancelled: Decimal,
                      ws: Decimal | None) -> dict:
    model = success + failed + cancelled
    return {
        "success_usd": round(float(success), 6),
        "failed_usd": round(float(failed), 6),
        "cancelled_usd": round(float(cancelled), 6),
        "model_usd": round(float(model), 6),
        "web_search_usd": round(float(ws), 6) if ws is not None else None,
        "total_usd": round(float(model + (ws or Decimal("0"))), 6),
    }


async def totals(session: AsyncSession, scope: ur.RollupScope) -> Totals:
    r = (await ur.execute(session, scope, _TOTALS_SELECT)).one()
    return Totals(
        total_requests=int(r.total_requests), error_requests=int(r.error_requests),
        total_cost=_d(r.total_cost), active_users=int(r.active_users or 0),
        input_tokens=int(r.input_tokens), output_tokens=int(r.output_tokens),
        cache_creation_tokens=int(r.cache_creation_tokens),
        cache_read_tokens=int(r.cache_read_tokens),
        ws_queries=int(r.ws_queries), ws_cost=r.ws_cost, ws_untracked=int(r.ws_untracked),
        cancelled_requests=int(r.cancelled_requests),
        billable_requests=int(r.billable_requests),
        success_cost=_d(r.success_cost), failed_cost=_d(r.failed_cost),
        cancelled_cost=_d(r.cancelled_cost),
    )


# ── 대시보드 ─────────────────────────────────────────────────────────────────

_MODEL_SHARE_SELECT = f"""
SELECT t.model_alias, t.cost_usd, ma.display_name
FROM (SELECT model_alias, COALESCE(sum(c), 0) AS cost_usd FROM x WHERE {_B}
      GROUP BY model_alias) t
LEFT JOIN model.model_aliases ma ON ma.alias = t.model_alias
ORDER BY t.cost_usd DESC, t.model_alias
"""


async def model_share(session: AsyncSession, scope: ur.RollupScope):
    return (await ur.execute(session, scope, _MODEL_SHARE_SELECT)).all()


# client-share: 모델 비용·호출 수는 청구 대상(상태 무관), 웹 검색은 전 status.
_CLIENT_SHARE_SELECT = """
SELECT client,
       COALESCE(sum(c), 0) AS cost_usd,
       COALESCE(sum(rq), 0)::bigint AS call_count,
       COALESCE(sum(wq), 0)::bigint AS ws_queries,
       sum(wc) AS ws_cost,
       COALESCE(sum(wu), 0)::bigint AS ws_untracked
FROM x
GROUP BY client
ORDER BY COALESCE(sum(c), 0) DESC, client
"""


async def client_share(session: AsyncSession, scope: ur.RollupScope):
    return (await ur.execute(session, scope, _CLIENT_SHARE_SELECT)).all()


_TOP_USERS_SELECT = f"""
SELECT u.id AS user_id, u.display_name AS name, u.email, u.team_id, tm.name AS team_name,
       d.name AS department_name, t.cost_usd, t.call_count
FROM (SELECT user_id, COALESCE(sum(c), 0) AS cost_usd, sum(rq)::bigint AS call_count
      FROM x WHERE {_B} GROUP BY user_id) t
JOIN auth.users u ON u.id = t.user_id
LEFT JOIN auth.teams tm ON tm.id = u.team_id
LEFT JOIN auth.departments d ON d.id = tm.dept_id
ORDER BY t.cost_usd DESC, u.id
LIMIT :lim
"""


async def top_users(session: AsyncSession, scope: ur.RollupScope, limit: int):
    return (await ur.execute(session, scope, _TOP_USERS_SELECT, {"lim": limit})).all()


_TOP_TEAMS_SELECT = f"""
SELECT tm.id AS team_id, tm.name, d.name AS department_name, t.cost_usd, t.call_count
FROM (SELECT team_id, COALESCE(sum(c), 0) AS cost_usd, sum(rq)::bigint AS call_count
      FROM x WHERE {_B} GROUP BY team_id) t
JOIN auth.teams tm ON tm.id = t.team_id
LEFT JOIN auth.departments d ON d.id = tm.dept_id
ORDER BY t.cost_usd DESC, tm.id
LIMIT :lim
"""


async def top_teams(session: AsyncSession, scope: ur.RollupScope, limit: int):
    return (await ur.execute(session, scope, _TOP_TEAMS_SELECT, {"lim": limit})).all()


_WS_BY_CLIENT_SELECT = """
SELECT client, COALESCE(sum(wq), 0)::bigint AS queries, sum(wc) AS cost
FROM x GROUP BY client HAVING COALESCE(sum(wq), 0) > 0
ORDER BY sum(wq) DESC, client
"""

_WS_DAILY_SELECT = """
SELECT day, COALESCE(sum(wq), 0)::bigint AS q FROM x WHERE wq > 0 GROUP BY day ORDER BY day
"""


async def ws_by_client(session: AsyncSession, scope: ur.RollupScope):
    return (await ur.execute(session, scope, _WS_BY_CLIENT_SELECT)).all()


async def ws_daily(session: AsyncSession, scope: ur.RollupScope):
    """일별 검색 수 — 검색이 있었던 날만. scope.by_day 여야 한다."""
    assert scope.by_day
    return (await ur.execute(session, scope, _WS_DAILY_SELECT)).all()


# ── Analytics 개요 (한 문장, x 한 번 계산) ───────────────────────────────────

_ANALYTICS_SELECT = f"""
, xm AS MATERIALIZED (SELECT * FROM x)
SELECT
  (SELECT COALESCE(json_agg(json_build_array(t.model_alias, t.c, t.n, ma.display_name)
                            ORDER BY t.c DESC, t.model_alias), '[]')
     FROM (SELECT model_alias, COALESCE(sum(c), 0) AS c, sum(rq)::bigint AS n
           FROM xm WHERE {_B} GROUP BY model_alias) t
     LEFT JOIN model.model_aliases ma ON ma.alias = t.model_alias)::text AS by_model,
  (SELECT COALESCE(json_agg(json_build_array(t.team_id, tm.name, d.name, t.c, t.u)
                            ORDER BY t.c DESC, t.team_id), '[]')
     FROM (SELECT team_id, COALESCE(sum(c), 0) AS c, count(DISTINCT user_id) AS u
           FROM xm WHERE {_B} GROUP BY team_id) t
     JOIN auth.teams tm ON tm.id = t.team_id
     LEFT JOIN auth.departments d ON d.id = tm.dept_id)::text AS by_team,
  CASE WHEN CAST(:want_users AS boolean) THEN
  (SELECT COALESCE(json_agg(json_build_array(u.id, u.display_name, u.email, t.c, t.n)
                            ORDER BY t.c DESC, u.id), '[]')
     FROM (SELECT user_id, COALESCE(sum(c), 0) AS c, sum(rq)::bigint AS n
           FROM xm WHERE {_B} GROUP BY user_id) t
     JOIN auth.users u ON u.id = t.user_id)::text END AS by_user
"""

_TRENDS_SELECT = f"""
, xd AS MATERIALIZED (SELECT team_id, day, sum(c) AS c, sum(rq)::bigint AS n
                      FROM x WHERE {_B} GROUP BY team_id, day)
SELECT
  (SELECT COALESCE(json_agg(json_build_array(day, c, n) ORDER BY day), '[]')
     FROM (SELECT day, COALESCE(sum(c), 0) AS c, sum(n)::bigint AS n FROM xd GROUP BY day) t
  )::text AS trends,
  (SELECT COALESCE(json_agg(json_build_array(t.team_id, tm.name, d.name, t.day, t.c, t.n)
                            ORDER BY tm.name, t.day, t.team_id), '[]')
     FROM xd t
     JOIN auth.teams tm ON tm.id = t.team_id
     LEFT JOIN auth.departments d ON d.id = tm.dept_id)::text AS by_team_day
"""


async def analytics_overview(session: AsyncSession, scope: ur.RollupScope, *, want_users: bool):
    """(by_model, by_team, by_user|None) — 정렬은 종전 응답과 같다(비용 DESC + 결정적 2차 키)."""
    sql = ur.head(scope) + _ANALYTICS_SELECT
    row = (await session.execute(text(sql), {**ur.bind(scope), "want_users": want_users})).one()
    return _j(row.by_model), _j(row.by_team), _j(row.by_user)


async def analytics_trends(session: AsyncSession, scope: ur.RollupScope):
    """(trends[day,c,n], by_team_day[team_id,team,dept,day,c,n]). scope.by_day 여야 한다."""
    assert scope.by_day
    sql = ur.head(scope) + _TRENDS_SELECT
    row = (await session.execute(text(sql), ur.bind(scope))).one()
    return _j(row.trends), _j(row.by_team_day)


# ── 모델 상세 ────────────────────────────────────────────────────────────────

_MODEL_DETAIL_SELECT = f"""
SELECT t.model_alias, ma.display_name, t.n AS request_count, t.c AS total_cost_usd,
       t.i AS input_tokens, t.o AS output_tokens, t.cr AS cache_read_tokens,
       t.cw AS cache_creation_tokens,
       CASE WHEN t.calls > 0 THEN t.lat::numeric / t.calls END AS avg_latency_ms
FROM (SELECT model_alias, sum(rq)::bigint AS n, COALESCE(sum(c), 0) AS c,
             COALESCE(sum(i), 0)::bigint AS i, COALESCE(sum(o), 0)::bigint AS o,
             COALESCE(sum(cr), 0)::bigint AS cr, COALESCE(sum(cw), 0)::bigint AS cw,
             COALESCE(sum(lat), 0)::bigint AS lat, sum(n)::bigint AS calls
      FROM x GROUP BY model_alias
      HAVING COALESCE(sum(rq), 0) > 0 OR COALESCE(sum(wq), 0) > 0) t
LEFT JOIN model.model_aliases ma ON ma.alias = t.model_alias
ORDER BY t.c DESC, t.model_alias
"""

_MODEL_DAILY_SELECT = f"""
SELECT day, model_alias, COALESCE(sum(c), 0) AS cost_usd
FROM x WHERE {_B} GROUP BY day, model_alias ORDER BY day, model_alias
"""


async def model_detail(session: AsyncSession, scope: ur.RollupScope):
    """모델별 상세 — request_count = 청구 대상 호출 수(rq), 비용·토큰 = 청구 대상 전 상태,
    평균 지연 = 그 모델의 **모든** 호출(0 토큰 실패 포함). 지연이 전 호출 기준이어야 하므로 scope 는
    billable_only 가 아니어야 한다(행 단위 판정과 입도 단위 판정이 n·lat 에서 갈린다)."""
    assert not scope.billable_only
    return (await ur.execute(session, scope, _MODEL_DETAIL_SELECT)).all()


async def model_daily(session: AsyncSession, scope: ur.RollupScope):
    assert scope.by_day
    return (await ur.execute(session, scope, _MODEL_DAILY_SELECT)).all()


# ── 사용자별 ─────────────────────────────────────────────────────────────────

_BY_USER_DETAIL_SELECT = f"""
SELECT t.user_id, u.display_name AS user_name, u.email AS user_email, t.c AS cost_usd,
       t.n AS calls, t.i AS input_tokens, t.o AS output_tokens, t.cr AS cache_read_tokens,
       t.cw AS cache_creation_tokens, tm.id AS team_id, tm.name AS team_name,
       d.id AS department_id, d.name AS department_name
FROM (SELECT user_id, COALESCE(sum(c), 0) AS c, sum(rq)::bigint AS n,
             COALESCE(sum(i), 0)::bigint AS i, COALESCE(sum(o), 0)::bigint AS o,
             COALESCE(sum(cr), 0)::bigint AS cr, COALESCE(sum(cw), 0)::bigint AS cw
      FROM x WHERE {_B} GROUP BY user_id) t
LEFT JOIN auth.users u ON u.id = t.user_id
LEFT JOIN auth.teams tm ON tm.id = u.team_id
LEFT JOIN auth.departments d ON d.id = tm.dept_id
ORDER BY t.c DESC, t.user_id
"""


async def by_user_detail(session: AsyncSession, scope: ur.RollupScope):
    return (await ur.execute(session, scope, _BY_USER_DETAIL_SELECT)).all()


_COST_BY_USER_ALL_STATUS_SELECT = """
SELECT user_id, COALESCE(sum(c), 0) + COALESCE(sum(wc), 0) AS actual FROM x GROUP BY user_id
"""


async def cost_by_user_all_status(session: AsyncSession, scope: ur.RollupScope
                                  ) -> dict[uuid.UUID, Decimal]:
    """사용자별 청구액 합(전 status, 모델 + 웹 검색) — seed 잔차(budget_usages − 실사용)의 뺄 값.

    budget_usages 가 웹 검색 비용을 포함하므로(DECISION 2) 여기서도 더한다 — 빼지 않으면 검색
    비용이 '시드 이관분' 으로 잘못 보인다.
    """
    assert not scope.billable_only
    rows = (await ur.execute(session, scope, _COST_BY_USER_ALL_STATUS_SELECT)).all()
    return {r.user_id: _d(r.actual) for r in rows}


# ── 웹 검색 사용량 ───────────────────────────────────────────────────────────

_WS_SUM_SELECT = "SELECT COALESCE(sum(wq), 0)::bigint AS q FROM x"
_WS_BY_TEAM_SELECT = "SELECT team_id, COALESCE(sum(wq), 0)::bigint AS q FROM x GROUP BY team_id"
_WS_BY_USER_SELECT = "SELECT user_id, COALESCE(sum(wq), 0)::bigint AS q FROM x GROUP BY user_id"


async def ws_sum(session: AsyncSession, scope: ur.RollupScope) -> int:
    return int((await ur.execute(session, scope, _WS_SUM_SELECT)).scalar() or 0)


async def ws_by_team(session: AsyncSession, scope: ur.RollupScope) -> dict[uuid.UUID, int]:
    rows = (await ur.execute(session, scope, _WS_BY_TEAM_SELECT)).all()
    return {r.team_id: int(r.q) for r in rows}


async def ws_by_user(session: AsyncSession, scope: ur.RollupScope) -> dict[uuid.UUID, int]:
    rows = (await ur.execute(session, scope, _WS_BY_USER_SELECT)).all()
    return {r.user_id: int(r.q) for r in rows}


_COST_SUM_SELECT = "SELECT COALESCE(sum(c), 0) + COALESCE(sum(wc), 0) AS c FROM x"


async def charged_cost(session: AsyncSession, scope: ur.RollupScope) -> Decimal:
    """청구 총액(모델 비용 전 billable 상태 + 웹 검색) — ROI 분모."""
    return _d((await ur.execute(session, scope, _COST_SUM_SELECT)).scalar())
