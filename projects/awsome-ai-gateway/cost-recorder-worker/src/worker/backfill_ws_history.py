# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""역사적 웹 검색 비용 소급 기록(option B) — usage/rollup 만 고치고 예산에는 청구하지 않는다.

    python -m worker.backfill_ws_history --cutoff 2026-11-25T11:07:47Z
        [--period YYYY-MM ...] [--apply] [--batch-size 100] [--wait-lock 60] [--out plan.json]

대상은 ``requested_at < --cutoff`` 이고 ``web_search_count > 0`` 인데
``web_search_cost_usd IS NULL`` 인 ``usage.usage_logs`` 행이다(wave-3 비용 추적 이전).
역사적 정가 **$7 / 1,000 searches** 로 ``web_search_cost_usd`` 만 채운다. ``cost_usd`` 는
0058/0061 의 의미대로 모델 토큰 비용이므로 바꾸지 않는다.

option B(사용자 결정): ``budget.budget_usages``·예산 카운터·Redis 는 어떤 경로로도 쓰지 않는다.
대신 행마다 ``audit.audit_logs`` 감사 행(action=BACKFILL_WS_HISTORY, resource_type=UsageLog)을
같은 트랜잭션에 남긴다. 대조 R3 는 이 감사 행의 delta 를 읽어 소급분을 ``retro_not_charged``
정보 카테고리로 빼므로 budget_vs_raw 불일치가 되지 않는다.

안전장치(3c 와 같은 패턴):
- 기본 dry-run — JSON 계획(rows/searches/$/day/scope)만 출력하고 아무것도 쓰지 않는다.
- ``--apply`` 만 쓰기. 배치마다 ``lock_timeout 5s``·``statement_timeout 30s``, 최대 5회 재시도.
- ``--apply`` 는 롤업 락(727001)과 대조 락(727002)을 함께 잡는다 — 롤업/대조가 반쯤
  스탬프된 상태를 보지 않는다. dry-run은 읽기 전용 스냅샷이라 이 락들을 잡지 않아
  계획 조회가 시간/야간 롤업을 밀어내지 않는다.
- 멱등: 스탬프된 행은 조건에서 빠지므로 재실행 계획은 0 rows 다.
- usage 행 스탬프 + 행별 감사 + 해당 롤업 입도의 웹 검색 열 재계산을 **한 트랜잭션**에 커밋한다.
  프로세스가 배치 사이에 죽어도 커밋된 배치는 롤업까지 같이 반영돼 있고, 다음 실행은 남은
  NULL 행만 이어서 처리한다.
- 롤업은 영향받은 (날짜/시간, user/team/dept/model/client/status) 입도만 고친다. 바뀌는 값은
  웹 검색 수/비용/미추적 수뿐이므로 토큰·모델 비용은 재계산하지 않는다(전체 날짜 DELETE+INSERT
  없음, usage_logs 테이블 재작성 없음). 일 롤업 행이 아직 없는 진행 중 날짜·시간은 시간/야간
  롤업 잡이 스탬프된 raw 를 읽어 맞춘다.

워커 pod 안에서 워커와 같은 env(DB_URL·REPORTING_TIMEZONE)로 실행한다.
종료 코드: 0 성공(dry-run 포함) · 1 오류 · 2 lock 을 못 잡음.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from worker.batch_flusher import _PER_APP_CLIENTS, DEFAULT_TEAM_ID
from worker.config import get_settings
from worker.daily_aggregator import (
    _day_window,
    _month_end,
    aggregation_lock,
    bucket_of,
)
from worker.reconciliation import RECONCILIATION_LOCK_KEY
from worker.web_search_pricing import web_search_cost

logger = structlog.get_logger(__name__)

ACTION = "BACKFILL_WS_HISTORY"
SYSTEM_USER_ID = "00000000-0000-4000-a000-000000000010"
#: 이 보정의 고정 역사적 단가. 현재 system_settings 단가를 읽지 않는다(이력 보존).
HISTORICAL_PRICE_PER_1K = Decimal("7.00")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: keyset 페이징 — usage_logs 를 잠그지 않고 후보만 bounded batch 로 읽는다.
_SCAN_SQL = """
SELECT request_id, user_id::text AS user_id, team_id::text AS team_id,
       dept_id::text AS dept_id, model_alias, client, status::text AS status,
       web_search_count, requested_at
FROM usage.usage_logs
WHERE requested_at < :cutoff
  AND web_search_count > 0
  AND web_search_cost_usd IS NULL
  AND (NOT :has_bounds OR (requested_at >= :start AND requested_at < :end))
  AND (NOT :initialized
       OR requested_at > :last_at
       OR (requested_at = :last_at AND request_id > :last_id))
ORDER BY requested_at, request_id
LIMIT :limit
"""

_UPDATE_SQL = """
UPDATE usage.usage_logs
   SET web_search_cost_usd = CAST(:cost AS numeric(14,6))
 WHERE request_id = :request_id
   AND web_search_count > 0
   AND web_search_cost_usd IS NULL
"""

_AUDIT_SQL = """
INSERT INTO audit.audit_logs
  (actor_user_id, actor_role, action, resource_type, resource_id, changes, result,
   ip_address, request_id)
VALUES (CAST(:actor AS uuid), 'SYSTEM', :action, :rtype, :rid, CAST(:changes AS jsonb),
        'SUCCESS', '127.0.0.1', :run_id)
"""

#: 한 롤업 입도의 웹 검색 3열만 raw 에서 다시 계산한다. cost 는 SUM 그대로 둔다 —
#: 추적된 행이 하나도 없으면 NULL 이어야 UI/대조가 0 과 미추적을 구분한다.
_WS_SUM_SQL = """
SELECT COALESCE(SUM(web_search_count), 0)::bigint AS wq,
       SUM(web_search_cost_usd) AS wc,
       COALESCE(SUM(web_search_count) FILTER (WHERE web_search_cost_usd IS NULL), 0)::bigint
         AS wu
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :end
  AND user_id = CAST(:user_id AS uuid)
  AND team_id = CAST(:team_id AS uuid)
  AND dept_id = CAST(:dept_id AS uuid)
  AND model_alias = :model_alias
  AND COALESCE(client, 'other') = :client_key
  AND status = CAST(:status AS usage.usage_status)
"""

_UPDATE_DAILY_WS_SQL = f"""
WITH src AS ({_WS_SUM_SQL})
UPDATE usage.daily_aggregates d
   SET web_search_count = src.wq,
       web_search_cost_usd = src.wc,
       web_search_untracked = src.wu
FROM src
WHERE d.date = :day
  AND d.user_id = CAST(:user_id AS uuid)
  AND d.team_id = CAST(:team_id AS uuid)
  AND d.dept_id = CAST(:dept_id AS uuid)
  AND d.model_alias = :model_alias
  AND d.client = :client_key
  AND d.status = CAST(:status AS usage.usage_status)
"""

_UPDATE_HOURLY_WS_SQL = f"""
WITH src AS ({_WS_SUM_SQL})
UPDATE usage.hourly_aggregates h
   SET web_search_count = src.wq,
       web_search_cost_usd = src.wc,
       web_search_untracked = src.wu
FROM src
WHERE h.hour_start = :hour_start
  AND h.user_id = CAST(:user_id AS uuid)
  AND h.team_id = CAST(:team_id AS uuid)
  AND h.dept_id = CAST(:dept_id AS uuid)
  AND h.model_alias = :model_alias
  AND h.client = :client_key
  AND h.status = CAST(:status AS usage.usage_status)
"""

#: 월 누적은 raw 가 아니라 신뢰 가능한 일 롤업의 합이다. 레지스트리가 덮는 구간까지만
#: 다시 합산하므로 오늘/미래 날짜나 아직 일 롤업이 없는 날짜는 포함되지 않는다.
_UPDATE_MONTHLY_WS_SQL = """
WITH reg AS (
  SELECT covered_through FROM usage.monthly_aggregate_months
   WHERE period = CAST(:period AS date)
), src AS (
  SELECT COALESCE(SUM(d.web_search_count), 0)::bigint AS wq,
         SUM(d.web_search_cost_usd) AS wc,
         COALESCE(SUM(d.web_search_untracked), 0)::bigint AS wu
  FROM usage.daily_aggregates d JOIN reg ON true
  WHERE d.date >= CAST(:period AS date) AND d.date <= reg.covered_through
    AND d.user_id = CAST(:user_id AS uuid)
    AND d.team_id = CAST(:team_id AS uuid)
    AND d.dept_id = CAST(:dept_id AS uuid)
    AND d.model_alias = :model_alias
    AND d.client = :client_key
    AND d.status = CAST(:status AS usage.usage_status)
)
UPDATE usage.monthly_aggregates m
   SET web_search_count = src.wq,
       web_search_cost_usd = src.wc,
       web_search_untracked = src.wu
FROM src
WHERE m.period = CAST(:period AS date)
  AND m.user_id = CAST(:user_id AS uuid)
  AND m.team_id = CAST(:team_id AS uuid)
  AND m.dept_id = CAST(:dept_id AS uuid)
  AND m.model_alias = :model_alias
  AND m.client = :client_key
  AND m.status = CAST(:status AS usage.usage_status)
"""


@dataclass(frozen=True)
class TargetRow:
    request_id: str
    user_id: str
    team_id: str
    dept_id: str
    model_alias: str
    client: str | None
    status: str
    web_search_count: int
    requested_at: datetime
    cost: Decimal
    reporting_date: date
    hour_start: datetime
    hour_end: datetime

    @property
    def client_key(self) -> str:
        return self.client or "other"


@dataclass
class Accumulator:
    rows: int = 0
    searches: int = 0
    cost: Decimal = Decimal(0)
    per_day: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_scope: dict[tuple[str, str, str | None], dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _add(bucket: dict[str, Any], row: TargetRow) -> None:
        bucket["rows"] += 1
        bucket["searches"] += row.web_search_count
        bucket["cost_usd"] += row.cost

    def add(self, row: TargetRow) -> None:
        self.rows += 1
        self.searches += row.web_search_count
        self.cost += row.cost
        day = row.reporting_date.isoformat()
        self._add(self.per_day.setdefault(
            day, {"date": day, "rows": 0, "searches": 0, "cost_usd": Decimal(0)}), row)
        scopes = [("USER", row.user_id, None)]
        if row.team_id != DEFAULT_TEAM_ID:
            scopes.append(("TEAM", row.team_id, None))
        if row.client in _PER_APP_CLIENTS:
            scopes.append(("USER", row.user_id, row.client))
        for key in scopes:
            label = {"scope": key[0], "scope_id": key[1], "client": key[2],
                     "rows": 0, "searches": 0, "cost_usd": Decimal(0)}
            self._add(self.per_scope.setdefault(key, label), row)

    def summary(self) -> dict[str, Any]:
        def clean(bucket: dict[str, Any]) -> dict[str, Any]:
            return {**bucket, "cost_usd": str(bucket["cost_usd"])}

        return {
            "rows": self.rows,
            "searches": self.searches,
            "cost_usd": str(self.cost),
            "per_day": [clean(self.per_day[k]) for k in sorted(self.per_day)],
            "per_scope": [clean(self.per_scope[k]) for k in sorted(
                self.per_scope, key=lambda x: (x[0], x[1], x[2] or "")
            )],
        }


def _period_bounds(tz: ZoneInfo, periods: list[str] | None) -> tuple[datetime, datetime] | None:
    """기간 필터의 넓은 UTC 하한/상한 — 정확한 월 판정은 리포팅 tz 로 다시 한다."""
    if not periods:
        return None
    firsts = sorted(date(int(p[:4]), int(p[5:7]), 1) for p in periods)
    return (_day_window(tz, firsts[0])[0],
            _day_window(tz, _month_end(firsts[-1]) + timedelta(days=1))[0])


def _period_of(tz: ZoneInfo, ts: datetime) -> str:
    local = ts.astimezone(tz)
    return f"{local.year}-{local.month:02d}"


def _target(raw: Any, tz: ZoneInfo) -> TargetRow:
    day, hs, he = bucket_of(tz, raw.requested_at)
    return TargetRow(
        request_id=raw.request_id, user_id=raw.user_id, team_id=raw.team_id,
        dept_id=raw.dept_id, model_alias=raw.model_alias, client=raw.client,
        status=raw.status, web_search_count=int(raw.web_search_count),
        requested_at=raw.requested_at,
        cost=web_search_cost(int(raw.web_search_count), HISTORICAL_PRICE_PER_1K),
        reporting_date=day, hour_start=hs, hour_end=he)


async def _fetch_batch(session, *, cutoff: datetime, bounds, initialized: bool,
                       last_at: datetime, last_id: str, batch_size: int):
    return (await session.execute(text(_SCAN_SQL), {
        "cutoff": cutoff, "has_bounds": bounds is not None,
        "start": bounds[0] if bounds else cutoff, "end": bounds[1] if bounds else cutoff,
        "initialized": initialized, "last_at": last_at, "last_id": last_id,
        "limit": batch_size})).all()


async def _scan(session, *, cutoff: datetime, tz: ZoneInfo, periods: list[str] | None,
                batch_size: int):
    """후보를 keyset bounded batch 로 넘긴다. period 필터는 리포팅 tz 월 기준."""
    bounds = _period_bounds(tz, periods)
    initialized, last_at, last_id = False, cutoff, ""
    wanted = set(periods or [])
    while True:
        rows = await _fetch_batch(session, cutoff=cutoff, bounds=bounds,
                                  initialized=initialized, last_at=last_at,
                                  last_id=last_id, batch_size=batch_size)
        if not rows:
            return
        initialized = True
        last_at, last_id = rows[-1].requested_at, rows[-1].request_id
        batch = [_target(r, tz) for r in rows
                 if not wanted or _period_of(tz, r.requested_at) in wanted]
        if batch:
            yield batch


async def build_plan(session_factory, *, tz_name: str, cutoff: datetime,
                     periods: list[str] | None = None, batch_size: int = 100) -> dict:
    """dry-run 계획 — REPEATABLE READ READ ONLY 한 스냅샷, 아무것도 쓰지 않는다."""
    tz = ZoneInfo(tz_name)
    acc = Accumulator()
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        async for batch in _scan(session, cutoff=cutoff, tz=tz, periods=periods,
                                 batch_size=batch_size):
            for row in batch:
                acc.add(row)
        await session.commit()
    return acc.summary()


def _sqlstate(exc: BaseException) -> str | None:
    cur = getattr(exc, "orig", None)
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code:
            return str(code)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return None


def _is_lock_or_serialization_error(exc: BaseException) -> bool:
    return _sqlstate(exc) in ("55P03", "40001", "40P01")


def _grain_params(row: TargetRow) -> dict[str, Any]:
    return {"user_id": row.user_id, "team_id": row.team_id, "dept_id": row.dept_id,
            "model_alias": row.model_alias, "client_key": row.client_key,
            "status": row.status}


async def _refresh_ws_rollup_keys(session, rows: list[TargetRow], *, tz: ZoneInfo,
                                  now: datetime) -> dict[str, int]:
    """스탬프된 행이 속한 롤업 입도의 웹 검색 열만 다시 계산한다(같은 트랜잭션).

    전체 날짜를 DELETE+INSERT 하지 않는다. 이 보정이 바꾸는 값은 웹 검색 3열뿐이라
    토큰·모델 비용을 다시 쓸 필요가 없고, 쓰기 범위도 SPEC 의 (day, scope) 제한에 맞다.
    롤업 행이 아직 없는 시간/날짜는 건너뛴다 — 시간·야간 롤업 잡이 스탬프된 raw 에서
    정확한 행을 만든다. 기존 롤업 행이 없는 상태를 이 보정이 새로 만들지는 않는다.
    """
    stats = {
        "daily_keys_updated": 0, "daily_keys_missing": 0, "daily_keys_deferred": 0,
        "hourly_keys_updated": 0, "hourly_keys_missing": 0, "open_hour_keys_deferred": 0,
        "monthly_keys_updated": 0, "monthly_keys_missing": 0, "monthly_keys_deferred": 0,
    }
    today = now.astimezone(tz).date()

    daily: dict[tuple, TargetRow] = {}
    hourly: dict[tuple, TargetRow] = {}
    monthly: dict[tuple, TargetRow] = {}
    deferred_daily: set[tuple] = set()
    deferred_monthly: set[tuple] = set()
    open_hours: set[tuple] = set()
    for row in rows:
        grain = (row.user_id, row.team_id, row.dept_id, row.model_alias,
                 row.client_key, row.status)
        if row.reporting_date < today:
            daily.setdefault((row.reporting_date, *grain), row)
            monthly.setdefault((row.reporting_date.replace(day=1), *grain), row)
        else:
            deferred_daily.add((row.reporting_date, *grain))
            deferred_monthly.add((row.reporting_date.replace(day=1), *grain))
        if row.hour_end <= now:
            hourly.setdefault((row.hour_start, row.hour_end, *grain), row)
        else:
            open_hours.add((row.hour_start, *grain))

    stats["daily_keys_deferred"] = len(deferred_daily)
    stats["monthly_keys_deferred"] = len(deferred_monthly)
    stats["open_hour_keys_deferred"] = len(open_hours)

    for key, row in daily.items():
        start, end = _day_window(tz, row.reporting_date)
        result = await session.execute(text(_UPDATE_DAILY_WS_SQL), {
            **_grain_params(row), "day": row.reporting_date, "start": start, "end": end})
        stats["daily_keys_updated" if result.rowcount else "daily_keys_missing"] += 1

    for key, row in hourly.items():
        result = await session.execute(text(_UPDATE_HOURLY_WS_SQL), {
            **_grain_params(row), "hour_start": row.hour_start,
            "start": row.hour_start, "end": row.hour_end})
        stats["hourly_keys_updated" if result.rowcount else "hourly_keys_missing"] += 1

    for key, row in monthly.items():
        result = await session.execute(text(_UPDATE_MONTHLY_WS_SQL), {
            **_grain_params(row), "period": row.reporting_date.replace(day=1)})
        stats["monthly_keys_updated" if result.rowcount else "monthly_keys_missing"] += 1
    return stats


async def _apply_batch(session_factory, batch: list[TargetRow], *, run_id: str,
                       cutoff: datetime, tz: ZoneInfo) -> tuple[list[TargetRow], dict[str, int]]:
    """usage 행 스탬프 + 행별 감사 + 해당 롤업 입도 갱신을 한 트랜잭션에 커밋한다.

    예산 테이블/Redis 는 건드리지 않는다. 롤업 갱신이 같은 트랜잭션이라 크래시가 나도
    usage 만 스탬프되고 롤업이 남는 상태(재실행 불가능한 반쪽 상태)는 생기지 않는다.
    """
    applied: list[TargetRow] = []
    async with session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
        for row in batch:
            result = await session.execute(text(_UPDATE_SQL), {
                "request_id": row.request_id, "cost": str(row.cost)})
            if result.rowcount != 1:
                continue  # 동시 실행/수동 보정으로 이미 채워진 행 — 감사도 다시 쓰지 않는다
            changes = {
                "run_id": run_id, "request_id": row.request_id,
                "requested_at": row.requested_at.isoformat(),
                "reporting_date": row.reporting_date.isoformat(),
                "user_id": row.user_id, "team_id": row.team_id, "dept_id": row.dept_id,
                "model_alias": row.model_alias, "client": row.client, "status": row.status,
                "web_search_count": row.web_search_count,
                "before": None, "after": str(row.cost), "delta": str(row.cost),
                "price_per_1k_queries": str(HISTORICAL_PRICE_PER_1K),
                "cutoff": cutoff.isoformat(), "retro_not_charged": True,
            }
            await session.execute(text(_AUDIT_SQL), {
                "actor": SYSTEM_USER_ID, "action": ACTION, "rtype": "UsageLog",
                "rid": f"ws_history:{row.request_id}", "changes": json.dumps(changes),
                "run_id": run_id})
            applied.append(row)
        rollup_stats = await _refresh_ws_rollup_keys(
            session, applied, tz=tz, now=datetime.now(UTC)) if applied else {}
        await session.commit()
    return applied, rollup_stats


async def _apply_with_retry(session_factory, batch: list[TargetRow], **kw):
    for attempt in range(5):
        try:
            return await _apply_batch(session_factory, batch, **kw)
        except DBAPIError as exc:
            if not _is_lock_or_serialization_error(exc) or attempt == 4:
                raise
            logger.warning("ws_history_backfill_lock_retry", attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _merge_stats(total: dict[str, int], part: dict[str, int]) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + int(value)


async def apply_backfill(session_factory, *, tz_name: str, cutoff: datetime,
                         periods: list[str] | None = None, batch_size: int = 100,
                         run_id: str | None = None) -> dict[str, Any]:
    """후보를 다시 bounded batch 로 훑으며 스탬프한다(apply 는 한 번의 스캔)."""
    tz = ZoneInfo(tz_name)
    run_id = run_id or str(uuid.uuid4())
    bounds = _period_bounds(tz, periods)
    wanted = set(periods or [])
    acc = Accumulator()
    rollup_stats: dict[str, int] = {}
    scanned = 0
    initialized, last_at, last_id = False, cutoff, ""
    while True:
        async with session_factory() as scan_session:
            rows = await _fetch_batch(scan_session, cutoff=cutoff, bounds=bounds,
                                      initialized=initialized, last_at=last_at,
                                      last_id=last_id, batch_size=batch_size)
            await scan_session.commit()
        if not rows:
            break
        initialized = True
        last_at, last_id = rows[-1].requested_at, rows[-1].request_id
        batch = [_target(r, tz) for r in rows
                 if not wanted or _period_of(tz, r.requested_at) in wanted]
        scanned += len(rows)
        if not batch:
            continue
        applied, part = await _apply_with_retry(
            session_factory, batch, run_id=run_id, cutoff=cutoff, tz=tz)
        _merge_stats(rollup_stats, part)
        for row in applied:
            acc.add(row)
    summary: dict[str, Any] = {
        "run_id": run_id, **acc.summary(), "scanned_rows": scanned,
        "rollup": rollup_stats, "budget_writes": 0, "redis_writes": 0,
    }
    async with session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text(_AUDIT_SQL), {
            "actor": SYSTEM_USER_ID, "action": ACTION, "rtype": "WsHistoryBackfill",
            "rid": f"{cutoff.isoformat()}:{run_id}", "changes": json.dumps(summary),
            "run_id": run_id})
        await session.commit()
    logger.warning("ws_history_backfill_applied", run_id=run_id, rows=acc.rows,
                   searches=acc.searches, cost_usd=str(acc.cost))
    return summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m worker.backfill_ws_history")
    p.add_argument("--cutoff", required=True,
                   help="wave-3 worker takeover time, ISO-8601 with offset (e.g. ...Z)")
    p.add_argument("--period", action="append",
                   help="YYYY-MM in the reporting timezone (repeatable); default: all history")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--wait-lock", type=float, default=60.0)
    p.add_argument("--out", help="also write the plan/result JSON here")
    return p.parse_args(argv)


async def run(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    tz = settings.reporting_timezone
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        print("--cutoff needs a UTC offset (e.g. 2026-11-25T11:07:47Z)", file=sys.stderr)
        return 1
    if cutoff > datetime.now(UTC):
        print("--cutoff is in the future", file=sys.stderr)
        return 1
    if args.batch_size < 1 or args.batch_size > 1000:
        print("--batch-size must be 1..1000", file=sys.stderr)
        return 1
    if args.wait_lock < 0:
        print("--wait-lock must be >= 0", file=sys.stderr)
        return 1
    if any(not _PERIOD_RE.match(p) for p in args.period or []):
        print("--period must be YYYY-MM", file=sys.stderr)
        return 1
    engine = None
    if session_factory is None:
        from worker.db import create_aggregator_engine, create_session_factory

        engine = create_aggregator_engine(settings)
        session_factory = create_session_factory(engine)
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run", "cutoff": cutoff.isoformat(),
        "tz": tz, "periods": args.period, "price_per_1k_queries": str(HISTORICAL_PRICE_PER_1K),
        "semantics": "usage records and rollups only; budget and Redis are never written",
    }
    try:
        if not args.apply:
            result["plan"] = await build_plan(
                session_factory, tz_name=tz, cutoff=cutoff, periods=args.period,
                batch_size=args.batch_size)
        else:
            bind = session_factory.kw["bind"]
            async with aggregation_lock(bind, args.wait_lock) as got_rollup:
                if not got_rollup:
                    print("could not take the rollup lock (727001) — rerun later", file=sys.stderr)
                    return 2
                async with aggregation_lock(
                    bind, args.wait_lock, key=RECONCILIATION_LOCK_KEY
                ) as got_recon:
                    if not got_recon:
                        print("could not take the reconciliation lock (727002) — rerun later",
                              file=sys.stderr)
                        return 2
                    result["applied"] = await apply_backfill(
                        session_factory, tz_name=tz, cutoff=cutoff, periods=args.period,
                        batch_size=args.batch_size)
    finally:
        if engine is not None:
            await engine.dispose()
    out = json.dumps(result, indent=2, default=str)
    print(out)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except SystemExit:
        raise
    except Exception:
        logger.exception("backfill_ws_history.failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
