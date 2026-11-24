# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Analytics 사용자 × 모델 토큰 사용량 (By User 모드 표·상세 드로어·CSV).

## 숫자의 정의 — Total Tokens KPI 와 같은 집합
SUCCESS 만, 리포팅 타임존(REPORTING_TIMEZONE) 일자 구간, `COALESCE(client,'other')` 앱 필터,
요청 시점 team_id 스코프. 즉 `/admin/analytics` 의 token_bucket_totals 와 같은 행 집합이다.

## 하이브리드 소스 (한 SQL 문)
날짜마다 세 소스 중 하나에서 읽는다 — 결과는 raw 와 행 단위로 같다(테스트가 고정).
  'm' 월 누적 usage.monthly_aggregates — 그 달이 구간에 [1일, covered_through] 통째로 들어오고,
      그 구간의 모든 날짜가 신뢰 가능하며 월 재구성 이후 다시 집계된 날짜가 없을 때
  'd' 일별 롤업 usage.daily_aggregates — 레지스트리상 v2·clean·같은 tz 이고 오늘 이전
  'r' raw usage.usage_logs — 그 외 전부(오늘, dirty, 백필 전 날짜, tz 불일치)
분류는 레지스트리(usage.daily_aggregate_days / monthly_aggregate_months)를 같은 문장 안에서
읽어 결정한다 — 별도 조회가 없으므로 분류와 데이터가 같은 스냅샷이다.

⚠️ `= ANY(ARRAY(SELECT …))` 형태를 쓴다. `IN (SELECT … FROM cte)` 로 쓰면 CTE 행수를
   모르는 플래너가 롤업 테이블을 통째로 해시 조인할 수 있다. ARRAY(...) 는 initplan 으로
   먼저 평가돼 인덱스 조건(ScalarArrayOp)으로 들어간다.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.usage_filters import day_range_to_utc, period_to_utc_range, reporting_timezone

#: daily_aggregate_days.grain_version 하한 — cost-recorder-worker GRAIN_VERSION 과 같다.
GRAIN_VERSION = 2
#: 표의 모델 열 수.
TOP_MODELS = 5
#: 사용자 × 모델 질의 statement_timeout (분석 화면 전체와 같은 예산).
STATEMENT_TIMEOUT = "10s"

SORT_RE = re.compile(r"(total|input|output|cache|cost|name|other|model:.{1,128})")


@dataclass(frozen=True)
class DayRange:
    start: date
    end: date
    label: str  # 응답 period — 'YYYY-MM' 또는 'YYYY-MM-DD~YYYY-MM-DD'


def resolve_range(period: str | None, start_date: str | None, end_date: str | None) -> DayRange:
    """/admin/analytics 와 같은 기간 규칙: custom 은 양쪽 필수(400), 형식·달력·역전 400,
    MAX_RANGE_DAYS 초과 422(day_range_to_utc). 없으면 period(YYYY-MM) 월 전체."""
    if start_date or end_date:
        if not (start_date and end_date):
            raise ValidationError("custom range requires both start_date and end_date")
        day_range_to_utc(start_date, end_date)  # 검증만 — 400/422
        return DayRange(date.fromisoformat(start_date), date.fromisoformat(end_date),
                        f"{start_date}~{end_date}")
    if not period:
        raise ValidationError("period is required (YYYY-MM) unless start_date/end_date are given")
    period_to_utc_range(period)  # 형식 검증 — 400
    y, m = int(period[:4]), int(period[5:7])
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return DayRange(first, last, period)


# ── SQL ─────────────────────────────────────────────────────────────────────


def _source_ctes(*, teams: bool, client: bool, user: bool) -> str:
    """하이브리드 소스 CTE 들 — 마지막 CTE `x` 가 (user_id, model_alias, i, o, cr, cw, c, n, l) 행.

    필터 조각은 고정 문자열이고 값은 전부 바인드다(사용자 입력이 SQL 텍스트에 들어가지 않는다).
    """
    agg_f = ""
    raw_f = ""
    if teams:
        agg_f += " AND a.team_id = ANY(CAST(:teams AS uuid[]))"
        raw_f += " AND l.team_id = ANY(CAST(:teams AS uuid[]))"
    if client:
        agg_f += " AND a.client = :client"
        raw_f += " AND COALESCE(l.client, 'other') = :client"
    if user:
        agg_f += " AND a.user_id = CAST(:uid AS uuid)"
        raw_f += " AND l.user_id = CAST(:uid AS uuid)"
    return f"""
days AS (
  SELECT g.d::date AS d
  FROM generate_series(CAST(:start_day AS date), CAST(:end_day AS date), interval '1 day') AS g(d)
  WHERE g.d::date <= CAST(:today AS date)
),
dclass AS (
  SELECT days.d,
         COALESCE(days.d < CAST(:today AS date) AND r.grain_version >= {GRAIN_VERSION}
                  AND NOT r.dirty AND r.tz = :tz, false) AS trusted,
         r.aggregated_at
  FROM days LEFT JOIN usage.daily_aggregate_days r ON r.date = days.d
),
months AS (
  SELECT m.period, m.covered_through
  FROM usage.monthly_aggregate_months m
  WHERE m.tz = :tz
    AND m.period >= CAST(:start_day AS date)
    AND m.covered_through <= CAST(:end_day AS date)
    AND m.covered_through < CAST(:today AS date)
    AND NOT EXISTS (
        SELECT 1 FROM dclass c
        WHERE c.d BETWEEN m.period AND m.covered_through
          AND (NOT c.trusted OR c.aggregated_at > m.built_at))
),
src AS (
  SELECT c.d,
         CASE WHEN EXISTS (SELECT 1 FROM months m WHERE c.d BETWEEN m.period AND m.covered_through)
                   THEN 'm'
              WHEN c.trusted THEN 'd'
              ELSE 'r' END AS kind
  FROM dclass c
),
x AS (
  SELECT a.user_id, a.model_alias, a.input_tokens AS i, a.output_tokens AS o,
         a.cache_read_tokens AS cr, a.cache_creation_tokens AS cw, a.total_cost_usd AS c,
         a.call_count AS n, a.latency_ms_sum AS l
  FROM usage.monthly_aggregates a
  WHERE a.period = ANY(ARRAY(SELECT period FROM months)) AND a.status = 'SUCCESS'{agg_f}
  UNION ALL
  SELECT a.user_id, a.model_alias, a.input_tokens, a.output_tokens,
         a.cache_read_tokens, a.cache_creation_tokens, a.total_cost_usd,
         a.call_count, a.latency_ms_sum
  FROM usage.daily_aggregates a
  WHERE a.date = ANY(ARRAY(SELECT d FROM src WHERE kind = 'd')) AND a.status = 'SUCCESS'{agg_f}
  -- raw 분기만 (user, model) 로 먼저 접는다 — 접지 않으면 UNION 이 열 형을 bigint 로 넓혀
  -- 바깥 sum 이 행마다 numeric 누적이 된다(백필 전 전량 raw 5M 행 벤치: 7.1s → 5.9s).
  -- 롤업 분기는 이미 접힌 행이라 미리 접으면 오히려 느리다(~100ms).
  UNION ALL
  SELECT l.user_id, l.model_alias, sum(l.input_tokens), sum(l.output_tokens),
         sum(l.cache_read_tokens), sum(l.cache_creation_tokens), sum(l.cost_usd),
         count(*), sum(l.latency_ms)
  FROM (SELECT (d::timestamp AT TIME ZONE :tz) AS s, ((d + 1)::timestamp AT TIME ZONE :tz) AS e
        FROM src WHERE kind = 'r') w
  JOIN usage.usage_logs l ON l.requested_at >= w.s AND l.requested_at < w.e
  WHERE l.status = 'SUCCESS'{raw_f}
  GROUP BY l.user_id, l.model_alias
),
um AS (
  SELECT user_id, model_alias, sum(i) AS i, sum(o) AS o, sum(cr) AS cr, sum(cw) AS cw,
         sum(c) AS c, sum(n) AS n, sum(l) AS l
  FROM x GROUP BY user_id, model_alias
)"""


def table_sql(*, teams: bool, client: bool) -> str:
    """스코프 전체 결과(전 사용자) — 상위 5 모델 + Other 버킷 셀. 한 행, JSON 3열."""
    return f"""WITH {_source_ctes(teams=teams, client=client, user=False)},
mt AS (
  SELECT model_alias, sum(i + o + cr + cw) AS t FROM um GROUP BY model_alias
),
top AS (
  SELECT model_alias, t FROM mt ORDER BY t DESC, model_alias LIMIT {TOP_MODELS}
),
cells AS (
  SELECT user_id,
         CASE WHEN model_alias IN (SELECT model_alias FROM top) THEN model_alias END AS bucket,
         sum(i) AS i, sum(o) AS o, sum(cr) AS cr, sum(cw) AS cw, sum(c) AS c, sum(n) AS n
  FROM um GROUP BY 1, 2
)
SELECT
  (SELECT COALESCE(json_agg(json_build_array(mt.model_alias, ma.display_name, mt.t,
                                             mt.model_alias IN (SELECT model_alias FROM top))
                            ORDER BY mt.t DESC, mt.model_alias), '[]')
     FROM mt LEFT JOIN model.model_aliases ma ON ma.alias = mt.model_alias) AS models,
  (SELECT COALESCE(json_agg(json_build_array(c.user_id, u.display_name, u.email, tm.name,
                                             c.bucket, c.i, c.o, c.cr, c.cw, c.c, c.n)), '[]')
     FROM cells c
     LEFT JOIN auth.users u ON u.id = c.user_id
     LEFT JOIN auth.teams tm ON tm.id = u.team_id) AS cells"""


def detail_sql(*, teams: bool, client: bool) -> str:
    return f"""WITH {_source_ctes(teams=teams, client=client, user=True)}
SELECT u.id AS user_id, u.display_name, u.email, t.name AS team_name, u.team_id,
  (SELECT COALESCE(json_agg(json_build_array(um.model_alias, ma.display_name, um.n, um.i, um.o,
                                             um.cr, um.cw, um.c, um.l)
                            ORDER BY um.i + um.o + um.cr + um.cw DESC, um.model_alias), '[]')
     FROM um LEFT JOIN model.model_aliases ma ON ma.alias = um.model_alias) AS models
FROM auth.users u LEFT JOIN auth.teams t ON t.id = u.team_id
WHERE u.id = CAST(:uid AS uuid)"""


def export_sql(*, teams: bool, client: bool) -> str:
    return f"""WITH {_source_ctes(teams=teams, client=client, user=False)}
SELECT um.user_id, u.display_name, u.email, tm.name AS team_name, um.model_alias,
       um.n, um.i, um.o, um.cr, um.cw, um.c
FROM um
LEFT JOIN auth.users u ON u.id = um.user_id
LEFT JOIN auth.teams tm ON tm.id = u.team_id
ORDER BY sum(um.i + um.o + um.cr + um.cw) OVER (PARTITION BY um.user_id) DESC, um.user_id,
         um.i + um.o + um.cr + um.cw DESC, um.model_alias"""


def _today(tz: str) -> date:
    """리포팅 tz 의 오늘 — 이 날짜는 항상 raw 로 읽는다(테스트가 고정할 수 있게 함수로 둔다)."""
    return datetime.now(ZoneInfo(tz)).date()


def _params(rng: DayRange, scope_ids, client: str | None, uid: uuid.UUID | None = None) -> dict:
    tz = reporting_timezone()
    p: dict[str, Any] = {
        "start_day": rng.start,
        "end_day": rng.end,
        "today": _today(tz),
        "tz": tz,
    }
    if scope_ids is not None:
        p["teams"] = [str(t) for t in scope_ids]
    if client:
        p["client"] = client
    if uid is not None:
        p["uid"] = str(uid)
    return p


def _norm_client(client: str | None) -> str | None:
    return None if client in (None, "", "all") else client


async def _execute(session: AsyncSession, sql: str, params: dict):
    # 트랜잭션 범위 타임아웃 — 요청 세션의 다른 질의·커넥션 풀에 새지 않는다.
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    return await session.execute(text(sql), params)


# ── 스코프 결과(캐시 단위) ──────────────────────────────────────────────────


async def fetch_scope_payload(
    session: AsyncSession, rng: DayRange, scope_ids, client: str | None
) -> dict:
    """스코프 전체 결과 — 이 dict 가 그대로 60s 캐시된다(정렬·페이지·검색은 여기서 파생).

    users: [user_id, name, email, team_name, [[bucket|None, i, o, cr, cw, cost, n], ...]]
    """
    client = _norm_client(client)
    row = (
        await _execute(
            session,
            table_sql(teams=scope_ids is not None, client=client is not None),
            _params(rng, scope_ids, client),
        )
    ).one()
    models = _json(row.models)
    users: dict[str, list] = {}
    for uid, name, email, team, bucket, i, o, cr, cw, c, n in _json(row.cells):
        u = users.get(uid)
        if u is None:
            u = users[uid] = [uid, name, email, team, []]
        u[4].append([bucket, int(i), int(o), int(cr), int(cw), round(float(c), 6), int(n)])
    return {
        "v": 1,
        "timezone": reporting_timezone(),
        "start_date": rng.start.isoformat(),
        "end_date": rng.end.isoformat(),
        "period": rng.label,
        "top_models": [[m, dn, int(t)] for m, dn, t, is_top in models if is_top],
        "other_models": [[m, dn, int(t)] for m, dn, t, is_top in models if not is_top],
        "users": list(users.values()),
    }


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


def _cell(i: int, o: int, cr: int, cw: int, c: float, n: int) -> dict:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "total_tokens": i + o + cr + cw,
        "cost_usd": round(c, 6),
        "requests": n,
    }


def _add(a: dict | None, b: dict) -> dict:
    if a is None:
        return dict(b)
    return {k: (round(a[k] + b[k], 6) if k == "cost_usd" else a[k] + b[k]) for k in a}


_MODE_KEY = {
    "total": lambda c: c["total_tokens"],
    "input": lambda c: c["input_tokens"],
    "output": lambda c: c["output_tokens"],
    "cache": lambda c: c["cache_read_tokens"] + c["cache_write_tokens"],
    "cost": lambda c: c["cost_usd"],
}


def page_from_payload(
    payload: dict,
    *,
    sort: str = "total",
    direction: str = "desc",
    page: int = 1,
    page_size: int = 10,
    q: str | None = None,
) -> dict:
    """캐시된 스코프 결과 → 표 응답(정렬·검색·페이지). DB 를 다시 읽지 않는다."""
    if not SORT_RE.fullmatch(sort or ""):
        raise ValidationError(
            "sort must be total|input|output|cache|cost|name|other|model:<alias>"
        )
    rows: list[dict] = []
    max_cell = {"total": 0, "input": 0, "output": 0, "cache": 0}
    for uid, name, email, team, buckets in payload["users"]:
        cells: dict[str, dict] = {}
        other: dict | None = None
        total: dict | None = None
        for bucket, i, o, cr, cw, c, n in buckets:
            cell = _cell(i, o, cr, cw, c, n)
            total = _add(total, cell)
            if bucket is None:
                other = _add(other, cell)
            else:
                cells[bucket] = cell
                for mode in max_cell:
                    max_cell[mode] = max(max_cell[mode], _MODE_KEY[mode](cell))
        rows.append({
            "user_id": uid, "name": name, "email": email, "team_name": team,
            "cells": cells, "other": other, "total": total or _cell(0, 0, 0, 0, 0.0, 0),
        })

    if q:
        needle = q.strip().lower()
        rows = [
            r for r in rows
            if any(needle in (r[k] or "").lower() for k in ("name", "email", "team_name"))
        ]

    desc = direction != "asc"
    if sort == "name":
        rows.sort(key=lambda r: ((r["name"] or r["email"] or "").lower(), r["user_id"]),
                  reverse=desc)
    else:
        if sort in _MODE_KEY:
            def value(r):
                return _MODE_KEY[sort](r["total"])
        elif sort == "other":
            def value(r):
                return r["other"]["total_tokens"] if r["other"] else None
        else:
            alias = sort.split(":", 1)[1]

            def value(r):
                c = r["cells"].get(alias)
                return c["total_tokens"] if c else None
        present = [r for r in rows if value(r) is not None]
        absent = [r for r in rows if value(r) is None]
        # 결정적 2차 키(user_id) — 같은 값의 사용자가 페이지 사이에서 흔들리지 않게.
        present.sort(key=lambda r: r["user_id"])
        present.sort(key=value, reverse=desc)
        absent.sort(key=lambda r: r["user_id"])
        rows = present + absent  # 안 쓴 사용자는 방향과 무관하게 뒤로

    total_users = len(rows)
    start = (page - 1) * page_size
    return {
        "period": payload["period"],
        "timezone": payload["timezone"],
        "start_date": payload["start_date"],
        "end_date": payload["end_date"],
        "top_models": [{"model": m, "display_name": dn, "total_tokens": t}
                       for m, dn, t in payload["top_models"]],
        "other_models": [{"model": m, "display_name": dn, "total_tokens": t}
                         for m, dn, t in payload["other_models"]],
        "total_users": total_users,
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "dir": "desc" if desc else "asc",
        "q": q or "",
        "max_cell": max_cell,
        "rows": rows[start:start + page_size],
    }


# ── 상세 · CSV ──────────────────────────────────────────────────────────────


async def get_user_detail(
    session: AsyncSession, user_id: uuid.UUID, rng: DayRange, scope_ids, client: str | None
) -> dict:
    """한 사용자의 전 모델 — Requests, 4 토큰 버킷, Total, Cost, 평균 지연."""
    client = _norm_client(client)
    row = (
        await _execute(
            session,
            detail_sql(teams=scope_ids is not None, client=client is not None),
            _params(rng, scope_ids, client, user_id),
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("user", str(user_id))
    models_raw = _json(row.models)
    # 스코프(팀 리더·team: 범위) 밖 사용자의 이름·이메일을 내주지 않는다 — 그 스코프에
    # 사용 기록이 있거나 현재 소속이 스코프 안일 때만 보인다.
    if scope_ids is not None and not models_raw and row.team_id not in set(scope_ids):
        raise NotFoundError("user", str(user_id))
    models = []
    total = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "avg_latency_ms": None}
    lat_sum = 0
    for alias, dn, n, i, o, cr, cw, c, lat in models_raw:
        n, i, o, cr, cw, lat = int(n), int(i), int(o), int(cr), int(cw), int(lat or 0)
        cell = _cell(i, o, cr, cw, float(c), n)
        models.append({"model": alias, "display_name": dn, "requests": n,
                       "input_tokens": i, "output_tokens": o, "cache_read_tokens": cr,
                       "cache_write_tokens": cw, "total_tokens": cell["total_tokens"],
                       "cost_usd": cell["cost_usd"],
                       "avg_latency_ms": round(lat / n) if n else None})
        for k in ("requests", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "total_tokens"):
            total[k] += models[-1][k]
        total["cost_usd"] = round(total["cost_usd"] + cell["cost_usd"], 6)
        lat_sum += lat
    if total["requests"]:
        total["avg_latency_ms"] = round(lat_sum / total["requests"])
    return {
        "user_id": str(row.user_id), "name": row.display_name, "email": row.email,
        "team_name": row.team_name, "period": rng.label, "timezone": reporting_timezone(),
        "start_date": rng.start.isoformat(), "end_date": rng.end.isoformat(),
        "models": models, "total": total,
    }


async def export_csv(session: AsyncSession, rng: DayRange, scope_ids, client: str | None) -> str:
    client = _norm_client(client)
    rows = (
        await _execute(
            session,
            export_sql(teams=scope_ids is not None, client=client is not None),
            _params(rng, scope_ids, client),
        )
    ).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["period", "timezone", "user_id", "name", "email", "team", "model", "requests",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "total_tokens", "cost_usd"])
    tz = reporting_timezone()
    for r in rows:
        w.writerow([rng.label, tz, str(r.user_id), r.display_name, r.email, r.team_name,
                    r.model_alias, int(r.n), int(r.i), int(r.o), int(r.cr), int(r.cw),
                    int(r.i + r.o + r.cr + r.cw), str(Decimal(r.c).quantize(Decimal("0.000001")))])
    return buf.getvalue()
