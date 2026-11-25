# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Analytics 사용자 × 모델 토큰 사용량 (By User 모드 표·상세 드로어·CSV).

## 숫자의 정의 — Total Tokens KPI 와 같은 집합
청구 대상(billable — 상태 무관, wave 3) 행, 리포팅 타임존(REPORTING_TIMEZONE) 일자 구간, `COALESCE(client,'other')` 앱 필터,
요청 시점 team_id 스코프. 즉 `/admin/analytics` 의 token_bucket_totals 와 같은 행 집합이다.

## 소스
services/usage_rollup 의 하이브리드 소스 `x` (월 누적 / 일별 / 시간별 롤업 + 진행 중인 시간만 raw)
— Dashboard·Analytics 와 같은 단일 헬퍼다. 예전엔 이 모듈에 따로 있던 CTE 의 raw 분기가
`JOIN usage_logs ON requested_at >= w.s …` 로 오늘 하루 전체(때로는 전체 테이블)를 훑었다
(30M 행 벤치: 오늘만 raw 69s, 전부 raw 308s — 10s 타임아웃 초과).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.usage_filters import reporting_timezone
from app.services import usage_rollup as ur

#: daily_aggregate_days.grain_version 하한 — usage_rollup 과 같다.
GRAIN_VERSION = ur.GRAIN_VERSION
#: 표의 모델 열 수.
TOP_MODELS = 5

SORT_RE = re.compile(r"(total|input|output|cache|cost|name|other|model:.{1,128})")

DayRange = ur.DayRange
resolve_range = ur.resolve_range


# ── SQL ─────────────────────────────────────────────────────────────────────


def _um(scope: ur.RollupScope) -> str:
    """하이브리드 CTE + `um`(user, model) 합계 — 청구 대상(DECISION 1, 상태 무관) (user, model) 만.

    n = 청구 대상 호출 수(rq), 비용·토큰 = 전 상태 합(비청구 행은 0), l/calls = 그 (user, model)
    의 **모든** 호출의 지연 합/호출 수(평균 지연). 판정을 HAVING 으로 하므로 scope 는 billable_only 가
    아니어야 한다 — 행 단위(raw) 판정과 입도 단위(롤업) 판정이 calls·l 에서 갈린다."""
    assert not scope.billable_only
    return f"""{ur.head(scope)},
um AS (
  SELECT user_id, model_alias, sum(i) AS i, sum(o) AS o, sum(cr) AS cr, sum(cw) AS cw,
         sum(c) AS c, sum(rq) AS n, sum(lat) AS l, sum(n) AS calls
  FROM x GROUP BY user_id, model_alias
  HAVING COALESCE(sum(rq), 0) > 0 OR COALESCE(sum(wq), 0) > 0
)"""


def table_sql(scope: ur.RollupScope) -> str:
    """스코프 전체 결과(전 사용자) — 상위 5 모델 + Other 버킷 셀. 한 행, JSON 2열(text)."""
    return f"""{_um(scope)},
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
     FROM mt LEFT JOIN model.model_aliases ma ON ma.alias = mt.model_alias)::text AS models,
  (SELECT COALESCE(json_agg(json_build_array(c.user_id, u.display_name, u.email, tm.name,
                                             c.bucket, c.i, c.o, c.cr, c.cw, c.c, c.n)), '[]')
     FROM cells c
     LEFT JOIN auth.users u ON u.id = c.user_id
     LEFT JOIN auth.teams tm ON tm.id = u.team_id)::text AS cells"""


def detail_sql(scope: ur.RollupScope) -> str:
    return f"""{_um(scope)}
SELECT u.id AS user_id, u.display_name, u.email, t.name AS team_name, u.team_id,
  (SELECT COALESCE(json_agg(json_build_array(um.model_alias, ma.display_name, um.n, um.i, um.o,
                                             um.cr, um.cw, um.c, um.l, um.calls)
                            ORDER BY um.i + um.o + um.cr + um.cw DESC, um.model_alias), '[]')
     FROM um LEFT JOIN model.model_aliases ma ON ma.alias = um.model_alias)::text AS models
FROM auth.users u LEFT JOIN auth.teams t ON t.id = u.team_id
WHERE u.id = CAST(:uid AS uuid)"""


def export_sql(scope: ur.RollupScope) -> str:
    return f"""{_um(scope)}
SELECT um.user_id, u.display_name, u.email, tm.name AS team_name, um.model_alias,
       um.n, um.i, um.o, um.cr, um.cw, um.c
FROM um
LEFT JOIN auth.users u ON u.id = um.user_id
LEFT JOIN auth.teams tm ON tm.id = u.team_id
ORDER BY sum(um.i + um.o + um.cr + um.cw) OVER (PARTITION BY um.user_id) DESC, um.user_id,
         um.i + um.o + um.cr + um.cw DESC, um.model_alias"""


def _today(tz: str) -> date:
    """리포팅 tz 의 오늘(테스트가 고정할 수 있게 함수로 둔다)."""
    return datetime.now(ZoneInfo(tz)).date()


def _scope(rng: DayRange, scope_ids, client: str | None,
           uid: uuid.UUID | None = None) -> ur.RollupScope:
    tz = reporting_timezone()
    return ur.RollupScope.of(rng, teams=scope_ids, client=client,
                             users=[uid] if uid is not None else None, today=_today(tz), tz=tz)


def _norm_client(client: str | None) -> str | None:
    return ur.norm_client(client)


async def _execute(session: AsyncSession, sql: str, params: dict):
    # 문장 상한은 분석 세션이 트랜잭션 시작에 SET LOCAL 로 건다(core/db.get_analytics_session).
    return await session.execute(text(sql), params)


# ── 스코프 결과(캐시 단위) ──────────────────────────────────────────────────


async def fetch_scope_payload(
    session: AsyncSession, rng: DayRange, scope_ids, client: str | None
) -> dict:
    """스코프 전체 결과 — 이 dict 가 그대로 캐시된다(정렬·페이지·검색은 여기서 파생).

    users: [user_id, name, email, team_name, [[bucket|None, i, o, cr, cw, cost, n], ...]]
    JSON 파싱·재구성은 스레드에서 한다(6,000 사용자 ≈ 3 MB — 루프에서 하면 그동안 다른 요청이 멈춘다).
    """
    scope = _scope(rng, scope_ids, client)
    row = (await _execute(session, table_sql(scope), ur.bind(scope))).one()
    meta = await ur.fetch_meta(session, scope)
    payload = await asyncio.to_thread(_build_payload, rng, row.models, row.cells)
    payload.update(meta.as_dict())
    return payload


def _build_payload(rng: DayRange, models_json, cells_json) -> dict:
    models = _json(models_json)
    users: dict[str, list] = {}
    for uid, name, email, team, bucket, i, o, cr, cw, c, n in _json(cells_json):
        u = users.get(uid)
        if u is None:
            u = users[uid] = [uid, name, email, team, []]
        u[4].append([bucket, int(i), int(o), int(cr), int(cw), round(float(c), 6), int(n)])
    return {
        "v": 2,
        "timezone": reporting_timezone(),
        "start_date": rng.start.isoformat(),
        "end_date": rng.end.isoformat(),
        "period": rng.label,
        "top_models": [[m, dn, int(t)] for m, dn, t, is_top in models if is_top],
        "other_models": [[m, dn, int(t)] for m, dn, t, is_top in models if not is_top],
        "users": list(users.values()),
    }


def _json(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v


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
        "data_as_of": payload.get("data_as_of"),
        "delayed_days": payload.get("delayed_days", []),
    }


# ── 상세 · CSV ──────────────────────────────────────────────────────────────


async def get_user_detail(
    session: AsyncSession, user_id: uuid.UUID, rng: DayRange, scope_ids, client: str | None
) -> dict:
    """한 사용자의 전 모델 — Requests, 4 토큰 버킷, Total, Cost, 평균 지연."""
    scope = _scope(rng, scope_ids, client, user_id)
    row = (
        await _execute(session, detail_sql(scope), {**ur.bind(scope), "uid": str(user_id)})
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
    calls_sum = 0
    for alias, dn, n, i, o, cr, cw, c, lat, calls in models_raw:
        n, i, o, cr, cw, lat = int(n), int(i), int(o), int(cr), int(cw), int(lat or 0)
        calls = int(calls or 0)
        cell = _cell(i, o, cr, cw, float(c), n)
        models.append({"model": alias, "display_name": dn, "requests": n,
                       "input_tokens": i, "output_tokens": o, "cache_read_tokens": cr,
                       "cache_write_tokens": cw, "total_tokens": cell["total_tokens"],
                       "cost_usd": cell["cost_usd"],
                       "avg_latency_ms": round(lat / calls) if calls else None})
        for k in ("requests", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "total_tokens"):
            total[k] += models[-1][k]
        total["cost_usd"] = round(total["cost_usd"] + cell["cost_usd"], 6)
        lat_sum += lat
        calls_sum += calls
    if calls_sum:
        total["avg_latency_ms"] = round(lat_sum / calls_sum)
    return {
        "user_id": str(row.user_id), "name": row.display_name, "email": row.email,
        "team_name": row.team_name, "period": rng.label, "timezone": reporting_timezone(),
        "start_date": rng.start.isoformat(), "end_date": rng.end.isoformat(),
        "models": models, "total": total,
    }


async def export_csv(session: AsyncSession, rng: DayRange, scope_ids, client: str | None) -> str:
    scope = _scope(rng, scope_ids, client)
    rows = (await _execute(session, export_sql(scope), ur.bind(scope))).all()
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
