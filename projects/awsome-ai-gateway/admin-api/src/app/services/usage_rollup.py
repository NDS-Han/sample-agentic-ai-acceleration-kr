# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""사용량 하이브리드 읽기 — Dashboard·Analytics·웹 검색·사용자×모델·ROI 카드의 **단일 소스**.

## 무엇을 어디서 읽나 (한 SQL 문, 같은 스냅샷)
리포팅 tz 날짜 [start, end] 의 날짜마다(오늘 이후·첫 사용량 이전 제외 — 정의상 비어 있다):
  'm' 월 누적 usage.monthly_aggregates — 그 달 [1일, covered_through] 이 구간에 통째로 들어오고,
      그 날짜들이 전부 신뢰 가능하며 월 재구성 뒤 다시 집계된 날짜가 없을 때(by_day=False 일 때만)
  'd' 일별 롤업 usage.daily_aggregates — 레지스트리상 grain≥3 · clean · 같은 tz · 표시 없음 · 오늘 이전
  'h' 그 밖(오늘, dirty/실패/미집계 날짜) — 그 날짜를 1시간 버킷(로컬 자정 + k 시간)으로 나눠
      · 시간 롤업 usage.hourly_aggregates 가 신뢰 가능한 버킷(grain≥3 · 같은 tz · 표시 없음)은 롤업,
      · 나머지 버킷만 raw usage.usage_logs 를 **버킷 하나씩 requested_at 범위 스캔**(LATERAL).
        오늘은 보통 진행 중인 한 시간만 raw 다. 신뢰 불가 과거 날짜도 그 하루만 범위 스캔한다 —
        월 전체 raw 스캔은 어떤 경로로도 일어나지 않는다. 그런 과거 날짜는 delayed_days 로 알린다.
분류는 레지스트리(daily_aggregate_days / monthly_aggregate_months / hourly_aggregate_hours /
rollup_dirty_marks)를 같은 문장 안에서 읽어 결정하므로 분류와 데이터가 같은 스냅샷이다.

## 결과 관계 `x` (UNION ALL, 입도 행)
(day, user_id, team_id, dept_id, model_alias, client, status,
 i, o, cw, cr, c, n, rq, lat, wq, wc, wu)
  day: 리포팅 tz 날짜 — 월 누적 행은 NULL(by_day=True 면 월 누적을 쓰지 않으므로 항상 값이 있다)
  i/o/cw/cr = input/output/cache_creation/cache_read 토큰, c = 비용(모델), n = 호출 수(count(*)),
  rq = 계량 호출 수, lat = 지연 합, wq/wc/wu = 웹 검색 수 / 비용(추적 행이 없으면 NULL) / 미추적 수
호출자는 ``WITH {ctes} SELECT … FROM x …`` 로 필요한 집계를 한다. 비용·토큰·활성 사용자 =
**청구 대상(billable) 행, 상태 무관**(wave 3 DECISION 1 — core/usage_filters.BILLABLE_GRAIN_SQL),
상태별 분해(성공·실패·취소)와 에러 수는 호출자의 FILTER 다.

## 값은 전부 바인드, 필터 조각은 고정 문자열
사용자 입력이 SQL 텍스트에 들어가지 않는다. tz 는 Settings validator 가 검증한 IANA 이름(바인드).

⚠️ `= ANY(ARRAY(SELECT …))` 형태를 쓴다. `IN (SELECT … FROM cte)` 로 쓰면 CTE 행수를 모르는
   플래너가 롤업 테이블을 통째로 해시 조인할 수 있다. ARRAY(...) 는 initplan 으로 먼저 평가돼
   인덱스 조건(ScalarArrayOp)으로 들어간다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.usage_filters import (
    billable_row_sql,
    day_range_to_utc,
    period_to_utc_range,
    reporting_timezone,
)

#: daily_aggregate_days / monthly_aggregate_months / hourly_aggregate_hours.grain_version 하한 —
#: cost-recorder-worker daily_aggregator.GRAIN_VERSION 과 같다(3 = 웹 검색 열 포함, 0061).
GRAIN_VERSION = 3

_HOUR = timedelta(hours=1)


# ── 범위 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayRange:
    start: date
    end: date
    label: str  # 응답 period — 'YYYY-MM' 또는 'YYYY-MM-DD~YYYY-MM-DD'
    is_custom: bool = False


def month_range(period: str) -> DayRange:
    period_to_utc_range(period)  # 형식 검증 — 400
    y, m = int(period[:4]), int(period[5:7])
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return DayRange(first, last, period)


def resolve_range(period: str | None, start_date: str | None, end_date: str | None) -> DayRange:
    """/admin/analytics 와 같은 기간 규칙: custom 은 양쪽 필수(400), 형식·달력·역전 400,
    MAX_RANGE_DAYS 초과 422(day_range_to_utc). 없으면 period(YYYY-MM) 월 전체."""
    if start_date or end_date:
        if not (start_date and end_date):
            raise ValidationError("custom range requires both start_date and end_date")
        day_range_to_utc(start_date, end_date)  # 검증만 — 400/422
        return DayRange(date.fromisoformat(start_date), date.fromisoformat(end_date),
                        f"{start_date}~{end_date}", True)
    if not period:
        raise ValidationError("period is required (YYYY-MM) unless start_date/end_date are given")
    return month_range(period)


def hour_buckets(tz: ZoneInfo, local_day: date) -> list[tuple[datetime, datetime]]:
    """그 날짜의 시간 버킷 [start, end) — 로컬 자정 + k 시간. 워커의 같은 함수와 같은 정의."""
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), datetime.min.time(),
                           tzinfo=tz).astimezone(UTC)
    out = []
    s = start
    while s < end:
        e = min(s + _HOUR, end)
        out.append((s, e))
        s = e
    return out


def reporting_today(tz: str | None = None) -> date:
    return datetime.now(ZoneInfo(tz or reporting_timezone())).date()


# ── 스코프 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RollupScope:
    """한 하이브리드 읽기의 범위·필터.

    teams None = 전사(제한 없음). 빈 리스트는 거부한다 — 좁히지 못하는 상황에서 넓은 결과를
    주지 않는다(analytics_repository._apply_scope_filter 와 같은 원칙).
    """

    start: date
    end: date
    teams: tuple[uuid.UUID, ...] | None = None
    users: tuple[uuid.UUID, ...] | None = None
    client: str | None = None
    #: 청구 대상(billable) 입도 행만 — 상태 무관(DECISION 1). 비용·토큰·rq·웹 검색 합은 판정
    #: 없이도 같다(비청구 행은 $0·0 토큰). ⚠️ 판정이 raw 분기는 행 단위, 롤업 분기는 입도 단위라
    #: 이 스코프에서 n(count(*))·lat 는 소스에 따라 달라진다 — 호출 수는 rq 를 쓰고, 지연이
    #: 필요한 질의는 billable_only 없이 HAVING 으로 거른다(usage_reads.model_detail).
    billable_only: bool = False
    by_day: bool = False
    #: 테스트·벤치가 "오늘"·"지금" 을 고정할 수 있게 둔다. None = 리포팅 tz 의 오늘 / 현재 시각.
    today: date | None = None
    tz: str | None = None
    now: datetime | None = None
    extra: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        if self.teams is not None and len(self.teams) == 0:
            raise ValueError("teams scope is empty — refusing to widen to all teams")
        if self.users is not None and len(self.users) == 0:
            raise ValueError("users scope is empty")
        if self.end < self.start:
            raise ValueError("end before start")

    @staticmethod
    def of(rng: DayRange, *, teams=None, users=None, client: str | None = None,
           billable_only: bool = False, by_day: bool = False, **kw) -> RollupScope:
        return RollupScope(
            start=rng.start, end=rng.end,
            teams=tuple(teams) if teams is not None else None,
            users=tuple(users) if users is not None else None,
            client=norm_client(client), billable_only=billable_only, by_day=by_day, **kw,
        )

    def cache_part(self) -> str:
        """캐시 키 조각 — 응답을 바꾸는 모든 필드."""
        teams = ",".join(sorted(str(t) for t in self.teams)) if self.teams is not None else "*"
        users = ",".join(sorted(str(u) for u in self.users)) if self.users is not None else "*"
        return (f"{self.start}~{self.end}|t={teams}|u={users}|c={self.client or 'all'}"
                f"|b={int(self.billable_only)}|d={int(self.by_day)}")


def norm_client(client: str | None) -> str | None:
    return None if client in (None, "", "all") else client


# ── SQL ─────────────────────────────────────────────────────────────────────

_COLS = ("day", "user_id", "team_id", "dept_id", "model_alias", "client", "status",
         "i", "o", "cw", "cr", "c", "n", "rq", "lat", "wq", "wc", "wu")

_ROLLUP_COLS = """a.user_id, a.team_id, a.dept_id, a.model_alias, a.client::text AS client, a.status,
         a.input_tokens AS i, a.output_tokens AS o, a.cache_creation_tokens AS cw,
         a.cache_read_tokens AS cr, a.total_cost_usd AS c, a.call_count AS n,
         a.request_count AS rq, a.latency_ms_sum AS lat, a.web_search_count AS wq,
         a.web_search_cost_usd AS wc, a.web_search_untracked AS wu"""


def _filters(scope: RollupScope) -> tuple[str, str]:
    agg_f = ""
    raw_f = ""
    if scope.teams is not None:
        agg_f += " AND a.team_id = ANY(CAST(:r_teams AS uuid[]))"
        raw_f += " AND l.team_id = ANY(CAST(:r_teams AS uuid[]))"
    if scope.users is not None:
        agg_f += " AND a.user_id = ANY(CAST(:r_users AS uuid[]))"
        raw_f += " AND l.user_id = ANY(CAST(:r_users AS uuid[]))"
    if scope.client is not None:
        agg_f += " AND a.client = :r_client"
        raw_f += " AND COALESCE(l.client, 'other') = :r_client"
    if scope.billable_only:
        # 같은 정의의 두 판 — 롤업 입도(계량 행 수·웹 검색 수)와 raw 행.
        agg_f += " AND (a.request_count > 0 OR a.web_search_count > 0)"
        raw_f += f" AND {billable_row_sql('l')}"
    return agg_f, raw_f


def _class_ctes(by_day: bool) -> str:
    """날짜·월·시간 버킷 분류 CTE(레지스트리만 읽는다). 데이터 질의와 메타 질의가 공유한다."""
    months = f"""
r_months AS (
  SELECT m.period, m.covered_through, m.built_at
  FROM usage.monthly_aggregate_months m
  WHERE m.tz = :r_tz AND m.grain_version >= {GRAIN_VERSION}
    AND m.period >= CAST(:r_start AS date)
    AND m.covered_through <= CAST(:r_end AS date)
    AND m.covered_through < CAST(:r_today AS date)
    AND NOT EXISTS (
        SELECT 1 FROM r_dclass c
        WHERE c.d BETWEEN m.period AND m.covered_through
          AND (NOT c.trusted OR c.aggregated_at > m.built_at))
),""" if not by_day else """
r_months AS (
  SELECT NULL::date AS period, NULL::date AS covered_through, NULL::timestamptz AS built_at
  WHERE false
),"""
    # 첫 사용량 이전 날짜는 정의상 비어 있다 — 레지스트리가 없어도 읽지 않는다(지연 표시도 없음).
    # min(requested_at) 은 requested_at 인덱스의 끝점 조회 하나다(테이블 크기와 무관).
    return f"""
r_first AS (
  SELECT (min(requested_at) AT TIME ZONE :r_tz)::date AS d FROM usage.usage_logs
),
r_days AS (
  SELECT g.d::date AS d
  FROM generate_series(CAST(:r_start AS date), CAST(:r_end AS date), interval '1 day') AS g(d)
  WHERE g.d::date <= CAST(:r_today AS date)
    AND g.d::date >= COALESCE((SELECT d FROM r_first), CAST(:r_today AS date) + 1)
),
r_dclass AS (
  SELECT r_days.d,
         COALESCE(r_days.d < CAST(:r_today AS date)
                  AND r.grain_version >= {GRAIN_VERSION} AND NOT r.dirty AND r.tz = :r_tz
                  AND r.aggregated_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM usage.rollup_dirty_marks dm
                                  WHERE dm.date = r_days.d AND dm.hour_start IS NULL),
                  false) AS trusted,
         r.aggregated_at
  FROM r_days LEFT JOIN usage.daily_aggregate_days r ON r.date = r_days.d
),{months}
r_src AS (
  SELECT c.d, c.aggregated_at,
         CASE WHEN EXISTS (SELECT 1 FROM r_months m WHERE c.d BETWEEN m.period AND m.covered_through)
                   THEN 'm'
              WHEN c.trusted THEN 'd'
              ELSE 'h' END AS kind
  FROM r_dclass c
),
r_hgood AS (
  SELECT h.hour_start, h.aggregated_at
  FROM usage.hourly_aggregate_hours h
  WHERE h.date = ANY(ARRAY(SELECT d FROM r_src WHERE kind = 'h'))
    AND h.tz = :r_tz AND h.grain_version >= {GRAIN_VERSION}
    AND NOT EXISTS (SELECT 1 FROM usage.rollup_dirty_marks dm WHERE dm.hour_start = h.hour_start)
),
r_bk AS (
  SELECT s.d, b.s, LEAST(b.s + interval '1 hour', w.de) AS e,
         (b.s = ANY(ARRAY(SELECT hour_start FROM r_hgood))) AS good
  FROM r_src s
  CROSS JOIN LATERAL (SELECT (s.d::timestamp AT TIME ZONE :r_tz) AS ds,
                             ((s.d + 1)::timestamp AT TIME ZONE :r_tz) AS de) w
  CROSS JOIN LATERAL generate_series(w.ds, w.de - interval '1 microsecond', interval '1 hour') AS b(s)
  WHERE s.kind = 'h'
    -- 아직 시작하지 않은 시간은 비어 있다(10분 여유 = 게이트웨이·admin-api 시계 차).
    AND b.s < CAST(:r_now AS timestamptz) + interval '10 minutes'
)"""


def source_ctes(scope: RollupScope) -> str:
    """하이브리드 소스 CTE 들 — 마지막 CTE `x` 가 입도 행(모듈 docstring)."""
    agg_f, raw_f = _filters(scope)
    month_branch = f"""
  SELECT NULL::date AS day, {_ROLLUP_COLS}
  FROM usage.monthly_aggregates a
  WHERE a.period = ANY(ARRAY(SELECT period FROM r_months)){agg_f}
  UNION ALL""" if not scope.by_day else ""
    return f"""{_class_ctes(scope.by_day)},
x AS ({month_branch}
  SELECT a.date AS day, {_ROLLUP_COLS}
  FROM usage.daily_aggregates a
  WHERE a.date = ANY(ARRAY(SELECT d FROM r_src WHERE kind = 'd')){agg_f}
  UNION ALL
  SELECT a.date AS day, {_ROLLUP_COLS}
  FROM usage.hourly_aggregates a
  WHERE a.hour_start = ANY(ARRAY(SELECT s FROM r_bk WHERE good)){agg_f}
  UNION ALL
  -- raw: 신뢰 가능한 롤업이 없는 시간 버킷만, 버킷 하나씩 requested_at 범위 스캔.
  SELECT r.*
  FROM (SELECT d, s, e FROM r_bk WHERE NOT good) bk
  CROSS JOIN LATERAL (
    SELECT bk.d AS day, l.user_id, l.team_id, l.dept_id, l.model_alias,
           COALESCE(l.client, 'other') AS client, l.status,
           sum(l.input_tokens)::bigint, sum(l.output_tokens)::bigint,
           sum(l.cache_creation_tokens)::bigint, sum(l.cache_read_tokens)::bigint,
           sum(l.cost_usd), count(*),
           count(*) FILTER (WHERE l.input_tokens + l.output_tokens + l.cache_creation_tokens
                                  + l.cache_read_tokens > 0 OR l.cost_usd > 0),
           sum(l.latency_ms)::bigint, sum(l.web_search_count)::bigint, sum(l.web_search_cost_usd),
           COALESCE(sum(l.web_search_count) FILTER (WHERE l.web_search_cost_usd IS NULL), 0)::bigint
    FROM usage.usage_logs l
    WHERE l.requested_at >= bk.s AND l.requested_at < bk.e{raw_f}
    GROUP BY l.user_id, l.team_id, l.dept_id, l.model_alias, COALESCE(l.client, 'other'), l.status
  ) r
)"""


def bind(scope: RollupScope) -> dict[str, Any]:
    tz = scope.tz or reporting_timezone()
    p: dict[str, Any] = {
        "r_start": scope.start,
        "r_end": scope.end,
        "r_today": scope.today or reporting_today(tz),
        "r_tz": tz,
        "r_now": scope.now or datetime.now(UTC),
    }
    if scope.teams is not None:
        p["r_teams"] = [str(t) for t in scope.teams]
    if scope.users is not None:
        p["r_users"] = [str(u) for u in scope.users]
    if scope.client is not None:
        p["r_client"] = scope.client
    return p


_META_SQL_TMPL = """WITH {ctes}
SELECT
  (SELECT COALESCE(array_agg(DISTINCT d ORDER BY d), '{{}}') FROM r_bk
    WHERE NOT good AND d < CAST(:r_today AS date)) AS delayed,
  GREATEST(
    (SELECT max(aggregated_at) FROM r_src WHERE kind = 'd'),
    (SELECT max(built_at) FROM r_months),
    (SELECT max(aggregated_at) FROM r_hgood)) AS as_of,
  (SELECT count(*) FROM r_bk WHERE NOT good AND d = CAST(:r_today AS date)) AS live_buckets
"""


@dataclass
class ReadMeta:
    data_as_of: datetime | None
    delayed_days: list[date]

    def as_dict(self) -> dict:
        return {
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "delayed_days": [d.isoformat() for d in self.delayed_days],
        }


async def fetch_meta(session: AsyncSession, scope: RollupScope) -> ReadMeta:
    """data_as_of(사용한 롤업 중 가장 늦은 집계 시각) + delayed_days(raw 로 읽은 과거 날짜).

    레지스트리만 읽는다(usage_logs 를 보지 않는다) — 데이터 질의와 별도 문장이지만 같은
    트랜잭션이라 대개 같은 시점이다(READ COMMITTED 라 문장마다 스냅샷은 다를 수 있다 — 표시용).
    """
    sql = _META_SQL_TMPL.format(ctes=_class_ctes(scope.by_day).lstrip())
    row = (await session.execute(text(sql), bind(scope))).one()
    return ReadMeta(data_as_of=row.as_of, delayed_days=list(row.delayed or []))


def merge_meta(*metas: ReadMeta) -> ReadMeta:
    stamps = [m.data_as_of for m in metas if m.data_as_of is not None]
    days = sorted({d for m in metas for d in m.delayed_days})
    return ReadMeta(data_as_of=max(stamps) if stamps else None, delayed_days=days)


def head(scope: RollupScope) -> str:
    """`WITH <하이브리드 CTE>` — 호출자가 `, more AS (…) SELECT …` 를 이어 붙인다."""
    return f"WITH {source_ctes(scope).lstrip()}"


def sql(scope: RollupScope, select: str) -> str:
    """`WITH <하이브리드 CTE> <select>` — select 는 `x` 를 읽는다."""
    return f"{head(scope)}\n{select}"


async def execute(session: AsyncSession, scope: RollupScope, select: str,
                  params: dict | None = None):
    return await session.execute(text(sql(scope, select)), {**bind(scope), **(params or {})})


def columns() -> tuple[str, ...]:
    return _COLS
