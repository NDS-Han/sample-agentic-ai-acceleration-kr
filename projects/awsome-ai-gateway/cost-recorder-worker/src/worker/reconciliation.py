# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""야간 비용 대조 (wave 3, B8) — 리포팅 tz 01:00, 자기 advisory lock(727_002).

원장들이 서로 같은 숫자를 말하는지 본다(허용 오차 0 — NUMERIC 6자리 그대로).
결과는 ``usage.reconciliation_runs`` 한 행(검사별 JSON)과
지표(``cost_recorder_reconciliation_*``)다.

검사(report_date = 어제, 리포팅 tz):

R1 raw_vs_daily
    usage_logs 하루 vs daily_aggregates — 입도(user,team,dept,model,client,status)별
    토큰·비용·호출 수·웹 검색 수/비용. dirty 날짜는 48 h 까지 pending(불일치 아님).
    진행 중인 날(오늘)과, 일 집계가 아직 없는 최근 날(시간 다리 bridge_days 안)은 일 집계 대신
    **시간 롤업**(hourly_aggregates)과 시간 버킷별로 비교한다(wave 3c — state=hourly). 아직 끝나지
    않은 시간은 빼고, 끝났지만 시간 롤업이 없는(또는 dirty) 시간은 pending —
    reconciliation_hourly_stale_sec 보다 오래되면 불일치로 센다.
R2 monthly_vs_daily
    monthly_aggregates vs Σ daily_aggregates [월초, covered_through] (월 누적이 있을 때).
R3 budget_vs_raw
    budget_usages(period) vs usage_logs 청구액 Σ(cost_usd + web_search_cost_usd).
    USER 총합 / TEAM(팀 없는 사용자의 센티널 팀 제외) / 앱별.
    SEED_BUDGET_SPENT 감사 델타(after−before)를 기대값에 더한다.
    두 원장은 같은 워커 트랜잭션에서 쓰이므로 같은 스냅샷에서 정확히 같아야 한다.
R4 redis_vs_db
    Redis 예산 카운터(usage − resvsum = 예약 제외 실지출) vs budget_usages.
    스트림에 미처리분이 있으면(lag>0) 건너뛴다(in-flight).
    Redis 에 키가 없으면 불일치가 아니다(다음 요청이 DB 에서 재구축한다).
    허용 오차 reconciliation_redis_epsilon_usd(기본 $0.0001, wave 3c) — Redis 는 float64 이고,
    0062 이전(NUMERIC(12,4)) 원장에서 재구축된 카운터는 4자리 반올림 잔차를 안고 있다.
retro_not_charged
    BACKFILL_WS_HISTORY 감사로 usage/rollup 에만 소급된 웹 검색 비용(option B). 정보 카테고리라
    mismatches 는 항상 0 이고, 같은 양은 budget_vs_raw 기대값에서 빠진다.
web_search_cost_gap
    web_search_cost_tracking_since 이후(report_date 끝까지)인데 web_search_count>0 인 행의
    web_search_cost_usd 가 NULL/0 — 새 기록 경로의 구멍. 행 수가 곧 mismatch 다.
R6 pricing
    report_date 의 토큰>0·비용 0·pricing_missing=false 행(조용한 $0).
    단가로 다시 계산한 비용 ≠ 기록 비용 — 캐시 쓰기·웹 검색 행은 제외(행에 분해가 없다).
    추정 행 비율(> reconciliation_estimated_warn_pct 면 warn, 불일치로 세지 않음).
    열린 pricing_missing 행 수(정보).
dirty_stale
    48 h 넘게 dirty 로 남은 날짜.
dead_letters_open
    usage.cost_dead_letters 의 미처리 행(원장에 없는 청구 행).

R5(게이트웨이 finalize/XADD/spool 카운터 vs 워커 기록)는 프로세스 지표 간 대조라
이 잡이 아니라 대시보드 식으로 둔다(REPORT). R7(Bedrock 호출 로그·CUR 대조)은 설계만 —
``docs/NDS/cost-reconciliation-r7.md``.

CLI: ``python -m worker.reconciliation [--date YYYY-MM-DD]`` — 결과 JSON 출력,
불일치가 있으면 exit 1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.batch_flusher import _PER_APP_CLIENTS, DEFAULT_TEAM_ID
from worker.config import get_settings
from worker.daily_aggregator import (
    GRAIN_VERSION,
    _day_window,
    _month_end,
    aggregation_lock,
    hour_buckets,
)

logger = structlog.get_logger(__name__)

RECONCILIATION_LOCK_KEY = 727_002

#: 마지막 실행의 검사별 불일치 수 — 지표 게이지 콜백이 읽는다.
LAST: dict[str, int] = {}
#: 마지막 성공 실행(오류 없음) 의 unix 시각 — ``reconciliation_last_success_timestamp`` 게이지.
#: 키 하나("ts")만 쓴다. 기동 시 load_last_success 가 표에서 채운다.
LAST_SUCCESS: dict[str, float] = {}

_GRAIN = "user_id, team_id, dept_id, model_alias, client, status"
_VALS = ("i, o, cw, cr, c, n, wq, wc")

_R1_SQL = f"""
WITH raw AS (
  SELECT user_id, team_id, dept_id, model_alias, COALESCE(client, 'other') AS client, status,
         SUM(input_tokens)::bigint AS i, SUM(output_tokens)::bigint AS o,
         SUM(cache_creation_tokens)::bigint AS cw, SUM(cache_read_tokens)::bigint AS cr,
         SUM(cost_usd) AS c, COUNT(*)::bigint AS n,
         SUM(web_search_count)::bigint AS wq, SUM(web_search_cost_usd) AS wc
  FROM usage.usage_logs WHERE requested_at >= :start AND requested_at < :end
  GROUP BY 1, 2, 3, 4, 5, 6
), agg AS (
  SELECT user_id, team_id, dept_id, model_alias, client::text AS client, status,
         input_tokens AS i, output_tokens AS o, cache_creation_tokens AS cw,
         cache_read_tokens AS cr, total_cost_usd AS c, call_count AS n,
         web_search_count AS wq, web_search_cost_usd AS wc
  FROM usage.daily_aggregates WHERE date = :day AND status IS NOT NULL
)
SELECT COUNT(*) AS mismatches,
       COALESCE(SUM(COALESCE(raw.c, 0) - COALESCE(agg.c, 0)), 0) AS cost_diff,
       (SELECT COUNT(*) FROM raw) AS raw_keys, (SELECT COALESCE(SUM(c), 0) FROM raw) AS raw_cost,
       (SELECT COALESCE(SUM(n), 0) FROM raw) AS raw_rows
FROM raw FULL OUTER JOIN agg USING ({_GRAIN})
WHERE (raw.i, raw.o, raw.cw, raw.cr, raw.c, raw.n, raw.wq, raw.wc)
      IS DISTINCT FROM (agg.i, agg.o, agg.cw, agg.cr, agg.c, agg.n, agg.wq, agg.wc)
"""

#: R1 시간 모드 — 신뢰 가능한 시간 버킷(:hs/:he 쌍)별 raw vs hourly_aggregates.
_R1_HOURLY_SQL = f"""
WITH h AS (
  SELECT * FROM unnest(CAST(:hs AS timestamptz[]), CAST(:he AS timestamptz[])) AS h(hs, he)
), raw AS (
  SELECT h.hs, user_id, team_id, dept_id, model_alias, COALESCE(client, 'other') AS client, status,
         SUM(input_tokens)::bigint AS i, SUM(output_tokens)::bigint AS o,
         SUM(cache_creation_tokens)::bigint AS cw, SUM(cache_read_tokens)::bigint AS cr,
         SUM(cost_usd) AS c, COUNT(*)::bigint AS n,
         SUM(web_search_count)::bigint AS wq, SUM(web_search_cost_usd) AS wc
  FROM usage.usage_logs l JOIN h ON l.requested_at >= h.hs AND l.requested_at < h.he
  WHERE l.requested_at >= :start AND l.requested_at < :end
  GROUP BY 1, 2, 3, 4, 5, 6, 7
), agg AS (
  SELECT hour_start AS hs, user_id, team_id, dept_id, model_alias, client::text AS client, status,
         input_tokens AS i, output_tokens AS o, cache_creation_tokens AS cw,
         cache_read_tokens AS cr, total_cost_usd AS c, call_count AS n,
         web_search_count AS wq, web_search_cost_usd AS wc
  FROM usage.hourly_aggregates WHERE hour_start = ANY(CAST(:hs AS timestamptz[]))
)
SELECT COUNT(*) AS mismatches,
       COALESCE(SUM(COALESCE(raw.c, 0) - COALESCE(agg.c, 0)), 0) AS cost_diff
FROM raw FULL OUTER JOIN agg USING (hs, {_GRAIN})
WHERE (raw.i, raw.o, raw.cw, raw.cr, raw.c, raw.n, raw.wq, raw.wc)
      IS DISTINCT FROM (agg.i, agg.o, agg.cw, agg.cr, agg.c, agg.n, agg.wq, agg.wc)
"""

#: 그날 시간 버킷별 raw 행 수·비용(끝난 버킷과 진행 중 버킷 모두).
_R1_HOUR_ROWS_SQL = """
SELECT h.hs, COUNT(l.id)::bigint AS n, COALESCE(SUM(l.cost_usd), 0) AS c
FROM unnest(CAST(:hs AS timestamptz[]), CAST(:he AS timestamptz[])) AS h(hs, he)
LEFT JOIN usage.usage_logs l ON l.requested_at >= h.hs AND l.requested_at < h.he
GROUP BY h.hs
"""

#: 신뢰 가능한 시간 = 레지스트리 v3·같은 tz, 그리고 그 시간의 dirty 표시가 없다.
_R1_GOOD_HOURS_SQL = """
SELECT r.hour_start FROM usage.hourly_aggregate_hours r
WHERE r.date = :day AND r.tz = :tz AND r.grain_version >= :grain
  AND NOT EXISTS (SELECT 1 FROM usage.rollup_dirty_marks m WHERE m.hour_start = r.hour_start)
"""

_DAY_STATE_SQL = """
SELECT r.date IS NOT NULL AS registered,
       COALESCE(r.grain_version >= :grain AND NOT r.dirty AND r.tz = :tz
                AND r.aggregated_at IS NOT NULL, false) AS clean,
       (SELECT min(created_at) FROM usage.rollup_dirty_marks m
         WHERE m.date = :day AND m.hour_start IS NULL) AS mark_since,
       r.aggregated_at
FROM (SELECT 1) one LEFT JOIN usage.daily_aggregate_days r ON r.date = :day
"""

_R2_SQL = f"""
WITH mon AS (
  SELECT {_GRAIN}, input_tokens AS i, output_tokens AS o, cache_creation_tokens AS cw,
         cache_read_tokens AS cr, total_cost_usd AS c, call_count AS n,
         web_search_count AS wq, web_search_cost_usd AS wc
  FROM usage.monthly_aggregates WHERE period = :period
), days AS (
  SELECT {_GRAIN}, SUM(input_tokens) AS i, SUM(output_tokens) AS o,
         SUM(cache_creation_tokens) AS cw, SUM(cache_read_tokens) AS cr,
         SUM(total_cost_usd) AS c, SUM(call_count) AS n, SUM(web_search_count) AS wq,
         SUM(web_search_cost_usd) AS wc
  FROM usage.daily_aggregates
  WHERE date >= :period AND date <= :through AND status IS NOT NULL
  GROUP BY {_GRAIN}
)
SELECT COUNT(*) FROM mon FULL OUTER JOIN days USING ({_GRAIN})
WHERE (mon.i, mon.o, mon.cw, mon.cr, mon.c, mon.n, mon.wq, mon.wc)
      IS DISTINCT FROM (days.i, days.o, days.cw, days.cr, days.c, days.n, days.wq, days.wc)
"""

_MONTH_REG_SQL = """
SELECT covered_through FROM usage.monthly_aggregate_months
WHERE period = :period AND tz = :tz AND grain_version >= :grain
"""

#: 청구액(모델 + 웹 검색) 원장 대조. 키 = (scope, scope_id, client).
#: BACKFILL_WS_HISTORY 로 usage 에만 소급 기록된 웹 검색 비용은 예산에 청구하지 않기로 한
#: 값(option B)이다. 기대값에서는 **감사 행의 delta**(현재 usage 값이 아니라 소급 당시 값)를
#: 빼고, 같은 양을 retro_not_charged 정보 카테고리로 보고한다. 그래서 소급 이후 누군가 usage
#: 행을 따로 고치면 정책상 미청구분이 아니라 실제 불일치로 남는다.
_R3_SQL = """
WITH raw AS (
  SELECT user_id, team_id, client, SUM(cost_usd + COALESCE(web_search_cost_usd, 0)) AS amt
  FROM usage.usage_logs WHERE requested_at >= :start AND requested_at < :end
  GROUP BY 1, 2, 3
), expected AS (
  SELECT 'USER'::text AS scope, user_id AS scope_id, NULL::text AS client, SUM(amt) AS amt
    FROM raw GROUP BY user_id
  UNION ALL
  SELECT 'TEAM', team_id, NULL, SUM(amt) FROM raw
    WHERE team_id <> CAST(:default_team AS uuid) GROUP BY team_id
  UNION ALL
  SELECT 'USER', user_id, client, SUM(amt) FROM raw
    WHERE client = ANY(CAST(:apps AS text[])) GROUP BY user_id, client
), seeds AS (
  SELECT split_part(resource_id, ':', 1) AS scope,
         CAST(split_part(resource_id, ':', 2) AS uuid) AS scope_id,
         NULLIF(split_part(resource_id, ':', 3), '') AS client,
         SUM(CAST(changes->>'after' AS numeric) - CAST(changes->>'before' AS numeric)) AS amt
  FROM audit.audit_logs
  WHERE action = 'SEED_BUDGET_SPENT' AND result = 'SUCCESS'
    AND split_part(resource_id, ':', 4) = :period
  GROUP BY 1, 2, 3
), exp2 AS (
  SELECT scope, scope_id, client, SUM(amt) AS amt FROM (
    SELECT * FROM expected UNION ALL SELECT * FROM seeds) t GROUP BY 1, 2, 3
), retro_rows AS (
  SELECT l.user_id, l.team_id, l.client,
         CAST(a.changes->>'web_search_count' AS bigint) AS queries,
         CAST(a.changes->>'delta' AS numeric) AS amt,
         COALESCE(a.changes->>'run_id', a.request_id) AS run_id
  FROM audit.audit_logs a
  JOIN usage.usage_logs l ON l.request_id = a.changes->>'request_id'
  WHERE a.action = 'BACKFILL_WS_HISTORY' AND a.resource_type = 'UsageLog'
    AND a.result = 'SUCCESS'
    AND l.requested_at >= :start AND l.requested_at < :end
), retro_scopes AS (
  SELECT 'USER'::text AS scope, user_id AS scope_id, NULL::text AS client,
         SUM(amt) AS amt
    FROM retro_rows GROUP BY user_id
  UNION ALL
  SELECT 'TEAM', team_id, NULL, SUM(amt) FROM retro_rows
    WHERE team_id <> CAST(:default_team AS uuid) GROUP BY team_id
  UNION ALL
  SELECT 'USER', user_id, client, SUM(amt) FROM retro_rows
    WHERE client = ANY(CAST(:apps AS text[])) GROUP BY user_id, client
), adjusted AS (
  SELECT e.scope, e.scope_id, e.client, e.amt - COALESCE(r.amt, 0) AS amt
  FROM exp2 e LEFT JOIN retro_scopes r
    ON r.scope = e.scope AND r.scope_id = e.scope_id
   AND r.client IS NOT DISTINCT FROM e.client
), retro_total AS (
  SELECT COUNT(*)::bigint AS rows,
         COALESCE(SUM(queries), 0)::bigint AS queries,
         COALESCE(SUM(amt), 0) AS amount_usd,
         COALESCE(array_agg(DISTINCT run_id) FILTER (WHERE run_id IS NOT NULL),
                  ARRAY[]::text[]) AS run_ids
  FROM retro_rows
), ledger AS (
  SELECT scope::text AS scope, scope_id, client::text AS client, used_usd AS amt
  FROM budget.budget_usages
  WHERE period = :period
    AND NOT (scope = 'TEAM' AND scope_id = CAST(:default_team AS uuid))
)
SELECT COUNT(*) AS mismatches,
       COALESCE(SUM(COALESCE(ledger.amt, 0) - COALESCE(adjusted.amt, 0)), 0) AS diff,
       (SELECT COALESCE(SUM(amt), 0) FROM expected WHERE scope = 'USER' AND client IS NULL)
         AS raw_total,
       (SELECT json_build_object('rows', rows, 'queries', queries,
                                 'amount_usd', amount_usd::text, 'run_ids', run_ids)
          FROM retro_total) AS retro,
       json_agg(json_build_array(COALESCE(ledger.scope, adjusted.scope),
                                 COALESCE(ledger.scope_id, adjusted.scope_id),
                                 COALESCE(ledger.client, adjusted.client),
                                 adjusted.amt, ledger.amt))
         FILTER (WHERE true) AS sample
FROM adjusted FULL OUTER JOIN ledger
  ON ledger.scope = adjusted.scope AND ledger.scope_id = adjusted.scope_id
 AND ledger.client IS NOT DISTINCT FROM adjusted.client
WHERE COALESCE(ledger.amt, 0) <> COALESCE(adjusted.amt, 0)
"""

_LEDGER_ROWS_SQL = """
SELECT scope::text AS scope, scope_id::text AS scope_id, client, used_usd
FROM budget.budget_usages WHERE period = :period
"""

_R6_SQL = """
SELECT
  COUNT(*) FILTER (WHERE cost_usd = 0 AND NOT pricing_missing
                   AND input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens > 0)
    AS zero_cost_with_tokens,
  COUNT(*) FILTER (WHERE estimated_usage) AS estimated_rows,
  COUNT(*) AS rows
FROM usage.usage_logs WHERE requested_at >= :start AND requested_at < :end
"""

_PRICING_OPEN_SQL = "SELECT COUNT(*) FROM usage.usage_logs WHERE pricing_missing"

_TRACKING_SINCE_SQL = text(
    "SELECT value FROM public.system_settings WHERE key = 'web_search_cost_tracking_since'"
)

#: wave-3 비용 추적 시작 이후인데 검색 수 > 0 · 비용 NULL/0 — 새 기록 경로의 구멍.
#: report_date 하루의 끝(리포팅 tz)까지 본다. usage_logs 에 없는 in-flight 이벤트는
#: 애초에 SELECT 되지 않으므로, 당일을 대조해도 flush 지연 자체가 거짓 양성을 만들지는 않는다.
_WS_GAP_SQL = """
WITH gaps AS (
  SELECT request_id, requested_at, web_search_count
  FROM usage.usage_logs
  WHERE requested_at >= :since AND requested_at < :end
    AND web_search_count > 0
    AND (web_search_cost_usd IS NULL OR web_search_cost_usd = 0)
)
SELECT COUNT(*)::bigint AS rows,
       COALESCE(SUM(web_search_count), 0)::bigint AS searches,
       (SELECT array_agg(request_id) FROM (
         SELECT request_id FROM gaps ORDER BY requested_at, request_id LIMIT 20) s) AS sample
FROM gaps
"""

#: 단가로 다시 계산 — 분해가 행에 없는 경우(캐시 쓰기·웹 검색 멀티턴·장문 티어)는 제외한다.
_RECOMPUTE_SQL = """
SELECT COUNT(*) AS checked,
       COUNT(*) FILTER (WHERE l.cost_usd <> round(
           l.input_tokens / 1000.0 * (CASE WHEN p.lt IS NOT NULL
                AND l.input_tokens + l.cache_read_tokens > p.lt
                THEN COALESCE(p.lpi, p.pi) ELSE p.pi END)
         + l.output_tokens / 1000.0 * (CASE WHEN p.lt IS NOT NULL
                AND l.input_tokens + l.cache_read_tokens > p.lt
                THEN COALESCE(p.lpo, p.po) ELSE p.po END)
         + l.cache_read_tokens / 1000.0 * (CASE WHEN p.lt IS NOT NULL
                AND l.input_tokens + l.cache_read_tokens > p.lt
                THEN COALESCE(p.lpcr, p.pcr) ELSE p.pcr END), 6)) AS mismatches
FROM usage.usage_logs l
CROSS JOIN LATERAL (
  SELECT input_price_per_1k_tokens AS pi, output_price_per_1k_tokens AS po,
         cache_read_price_per_1k_tokens AS pcr, long_context_threshold_tokens AS lt,
         long_context_input_price_per_1k_tokens AS lpi,
         long_context_output_price_per_1k_tokens AS lpo,
         long_context_cache_read_price_per_1k_tokens AS lpcr
  FROM model.model_pricings mp
  WHERE mp.model_alias = l.model_alias AND mp.effective_from <= l.requested_at
    AND (mp.effective_until IS NULL OR mp.effective_until > l.requested_at)
  ORDER BY mp.effective_from DESC LIMIT 1
) p
WHERE l.requested_at >= :start AND l.requested_at < :end
  AND NOT l.pricing_missing AND l.cache_creation_tokens = 0 AND l.web_search_count = 0
  -- 장문 티어 경계 행은 게이트웨이 판정(캐시 쓰기 포함 프롬프트)과 같게 다시 판정할 수 있지만
  -- 웹 검색 멀티턴은 턴별이라 제외했다. 여기서는 단일 턴 행만 본다.
"""

_DIRTY_STALE_SQL = """
SELECT COUNT(DISTINCT d) FROM (
  SELECT date AS d FROM usage.rollup_dirty_marks
   WHERE hour_start IS NULL AND created_at < clock_timestamp() - make_interval(hours => :h)
  UNION
  SELECT date FROM usage.daily_aggregate_days
   WHERE dirty
     AND COALESCE(aggregated_at, '-infinity') < clock_timestamp() - make_interval(hours => :h)
) t
"""

_DEAD_OPEN_SQL = "SELECT COUNT(*) FROM usage.cost_dead_letters WHERE replayed_at IS NULL"

_INSERT_RUN_SQL = """
INSERT INTO usage.reconciliation_runs
  (started_at, finished_at, report_date, period, tz, status, mismatch_count, checks, error)
VALUES (:started, clock_timestamp(), :day, :period, :tz, :status, :mm, CAST(:checks AS jsonb), :err)
RETURNING id
"""


def _f(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


async def _r1_hourly(session: AsyncSession, tz: str, day: date, *, now: datetime,
                     stale_sec: int, hourly_enabled: bool, reason: str) -> dict:
    """진행 중·일 집계 전 날 — 끝난 시간 버킷을 시간 롤업과 비교한다(모듈 docstring R1)."""
    start, end = _day_window(ZoneInfo(tz), day)
    buckets = hour_buckets(ZoneInfo(tz), day)
    p = {"hs": [b[0] for b in buckets], "he": [b[1] for b in buckets]}
    per_hour = {r.hs: r for r in await session.execute(text(_R1_HOUR_ROWS_SQL), p)}
    raw_rows = sum(int(r.n) for r in per_hour.values())
    raw_cost = sum((Decimal(r.c) for r in per_hour.values()), Decimal(0))
    out: dict[str, Any] = {"state": "hourly", "reason": reason, "raw_rows": raw_rows,
                           "raw_cost": _f(raw_cost)}
    done = [(s, e) for s, e in buckets if e <= now]
    open_rows = sum(int(per_hour[s].n) for s, e in buckets if e > now and s in per_hour)
    out["hours_completed"] = len(done)
    out["rows_in_open_hour"] = open_rows  # 아직 끝나지 않은 시간 — 비교하지 않는다
    if not hourly_enabled:
        out["state"] = "in_progress_excluded"
        out["mismatches"] = 0
        return out
    good = {r[0] for r in await session.execute(text(_R1_GOOD_HOURS_SQL), {
        "day": day, "tz": tz, "grain": GRAIN_VERSION})}
    checked = [(s, e) for s, e in done if s in good]
    pending = [(s, e) for s, e in done if s not in good and int(per_hour[s].n) > 0]
    stale = [(s, e) for s, e in pending if (now - e).total_seconds() > stale_sec]
    mm, diff = 0, Decimal(0)
    if checked:
        row = (await session.execute(text(_R1_HOURLY_SQL), {
            "hs": [c[0] for c in checked], "he": [c[1] for c in checked],
            "start": start, "end": end})).one()
        mm, diff = int(row.mismatches), Decimal(row.cost_diff)
    out.update({"hours_checked": len(checked), "keys_differing": mm, "cost_diff": _f(diff),
                "hours_pending": len(pending), "hours_stale": len(stale),
                "stale_sample": [s.isoformat() for s, _ in stale[:5]],
                "mismatches": mm + len(stale)})
    return out


async def _r1(session: AsyncSession, tz: str, day: date, stale_h: int, *,
              now: datetime | None = None, bridge_days: int = 2,
              hourly_stale_sec: int = 7200, hourly_enabled: bool = True) -> dict:
    now = now or datetime.now(UTC)
    today = now.astimezone(ZoneInfo(tz)).date()
    hourly = dict(now=now, stale_sec=hourly_stale_sec, hourly_enabled=hourly_enabled)
    if day >= today:
        # 진행 중인 날 — 일 집계는 아직 없다(있어도 계속 자라는 날이라 비교 대상이 아니다).
        return await _r1_hourly(session, tz, day, reason="in_progress", **hourly)
    start, end = _day_window(ZoneInfo(tz), day)
    st = (await session.execute(text(_DAY_STATE_SQL),
                                {"day": day, "tz": tz, "grain": GRAIN_VERSION})).one()
    if not st.registered and day >= today - timedelta(days=bridge_days):
        # 일 집계 전(야간 잡 00:10 전 등) — 시간 다리 창 안이면 시간 롤업으로 본다.
        return await _r1_hourly(session, tz, day, reason="not_aggregated_yet", **hourly)
    row = (await session.execute(text(_R1_SQL), {"day": day, "start": start, "end": end})).one()
    out = {"raw_rows": int(row.raw_rows), "raw_cost": _f(row.raw_cost),
           "keys_differing": int(row.mismatches), "cost_diff": _f(row.cost_diff)}
    if not st.registered:
        # 집계가 한 번도 안 된 날 — 사용량이 없으면 정상(빈 날), 있으면 불일치.
        out["state"] = "not_aggregated"
        out["mismatches"] = 1 if row.raw_rows else 0
        return out
    pending = (not st.clean) or st.mark_since is not None
    if pending:
        since = st.mark_since or st.aggregated_at
        age_h = ((now - since).total_seconds() / 3600) if since else None
        out["state"] = "dirty"
        out["dirty_age_hours"] = round(age_h, 2) if age_h is not None else None
        # 48 h 안의 dirty 는 다음 시간 잡이 다시 집계한다 — 불일치로 세지 않는다.
        out["mismatches"] = 1 if (age_h is None or age_h > stale_h) else 0
        return out
    out["state"] = "clean"
    out["mismatches"] = int(row.mismatches)
    return out


async def _r2(session: AsyncSession, tz: str, period_day: date) -> dict:
    through = (await session.execute(text(_MONTH_REG_SQL), {
        "period": period_day, "tz": tz, "grain": GRAIN_VERSION})).scalar_one_or_none()
    if through is None:
        return {"state": "absent", "mismatches": 0}
    n = (await session.execute(text(_R2_SQL),
                               {"period": period_day, "through": through})).scalar_one()
    return {"state": "present", "covered_through": through.isoformat(), "mismatches": int(n)}


async def _r3(session: AsyncSession, tz: str, period: str) -> dict:
    y, m = int(period[:4]), int(period[5:7])
    first = date(y, m, 1)
    start = _day_window(ZoneInfo(tz), first)[0]
    end = _day_window(ZoneInfo(tz), _month_end(first) + timedelta(days=1))[0]
    row = (await session.execute(text(_R3_SQL), {
        "start": start, "end": end, "period": period, "default_team": DEFAULT_TEAM_ID,
        "apps": list(_PER_APP_CLIENTS)})).one()
    sample = row.sample if isinstance(row.sample, list) else json.loads(row.sample or "[]")
    retro = row.retro if isinstance(row.retro, dict) else json.loads(row.retro or "{}")
    retro.setdefault("rows", 0)
    retro.setdefault("queries", 0)
    retro.setdefault("amount_usd", "0")
    retro.setdefault("run_ids", [])
    return {"mismatches": int(row.mismatches), "ledger_minus_expected": _f(row.diff),
            "raw_charged_total": _f(row.raw_total), "retro_not_charged": retro,
            "sample": sample[:20]}


async def _stream_lag(redis, settings) -> int | None:
    try:
        for g in await redis.xinfo_groups(settings.cost_stream_key) or []:
            name = g.get("name")
            if isinstance(name, bytes):
                name = name.decode()
            if name == settings.cost_stream_group:
                lag = g.get("lag")
                pend = int(g.get("pending", 0) or 0)
                return int(lag if lag is not None else 0) + pend
        return 0
    except Exception:
        return None


def _redis_keys(scope: str, sid: str, client: str | None, period: str) -> tuple[str, str]:
    s = "user" if scope == "USER" else "team"
    if client:
        return (f"budget:user:{{{sid}}}:{client}:{period}",
                f"budget:resvsum:user:{{{sid}}}:{client}:{period}")
    return f"budget:{s}:{{{sid}}}:{period}", f"budget:resvsum:{s}:{{{sid}}}:{period}"


async def _r4(session: AsyncSession, redis, settings, period: str) -> dict:
    eps = Decimal(str(getattr(settings, "reconciliation_redis_epsilon_usd", 0) or 0))
    if redis is None:
        return {"state": "no_redis", "mismatches": 0}
    lag = await _stream_lag(redis, settings)
    if lag is None or lag > 0:
        return {"state": "skipped_in_flight", "stream_lag": lag, "mismatches": 0}
    rows = (await session.execute(text(_LEDGER_ROWS_SQL), {"period": period})).all()
    checked = absent = lost = 0
    bad: list = []
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        keys: list[str] = []
        for r in chunk:
            uk, rk = _redis_keys(r.scope, r.scope_id, r.client, period)
            keys += [uk, rk, f"{uk}:lost"]
        vals = await redis.mget(keys)
        for j, r in enumerate(chunk):
            u, rv, lv = vals[3 * j], vals[3 * j + 1], vals[3 * j + 2]
            if u is None:
                absent += 1
                continue
            if lv is not None:
                lost += 1  # 재구축 대기 — 다음 admission 이 원장 기준으로 고친다
                continue
            checked += 1
            net = Decimal(u.decode() if isinstance(u, bytes) else str(u)) - Decimal(
                (rv.decode() if isinstance(rv, bytes) else str(rv)) if rv is not None else "0")
            # Redis 는 INCRBYFLOAT(float64) — 6자리 반올림 뒤 비교(원장 정밀도와 같다).
            # 차이가 eps 이하면 같다고 본다(0 이면 정확 일치 — wave 3 동작).
            q = Decimal("0.000001")
            if abs(net.quantize(q) - Decimal(r.used_usd).quantize(q)) > eps:
                bad.append([r.scope, r.scope_id, r.client, str(r.used_usd), str(net)])
    return {"state": "checked", "epsilon_usd": str(eps), "checked": checked,
            "absent_in_redis": absent,
            "lost_sentinel": lost, "mismatches": len(bad), "sample": bad[:20]}


def _parse_tracking_since(raw: Any) -> datetime | None:
    """system_settings JSONB/문자열 → timezone-aware datetime. 형상이 틀리면 None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        raw = raw.get("since")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


async def _ws_cost_gap(session: AsyncSession, tz: str, day: date) -> dict:
    """post-cutover 웹 검색 비용 구멍 — 정보가 아니라 mismatch 로 보고한다."""
    raw = (await session.execute(_TRACKING_SINCE_SQL)).scalar_one_or_none()
    since = _parse_tracking_since(raw)
    if since is None:
        return {"state": "no_tracking_marker", "rows": 0, "searches": 0,
                "mismatches": 0, "sample": []}
    end = _day_window(ZoneInfo(tz), day + timedelta(days=1))[0]
    r = (await session.execute(text(_WS_GAP_SQL), {"since": since, "end": end})).one()
    sample = r.sample if isinstance(r.sample, list) else []
    return {"state": "checked", "tracking_since": since.isoformat(),
            "rows": int(r.rows), "searches": int(r.searches), "sample": sample,
            "mismatches": int(r.rows)}


async def _r6(session: AsyncSession, tz: str, day: date, warn_pct: float) -> dict:
    start, end = _day_window(ZoneInfo(tz), day)
    p = {"start": start, "end": end}
    r = (await session.execute(text(_R6_SQL), p)).one()
    rc = (await session.execute(text(_RECOMPUTE_SQL), p)).one()
    open_missing = (await session.execute(text(_PRICING_OPEN_SQL))).scalar_one()
    share = (float(r.estimated_rows) / float(r.rows) * 100) if r.rows else 0.0
    return {"zero_cost_with_tokens": int(r.zero_cost_with_tokens),
            "recompute_checked": int(rc.checked), "recompute_mismatches": int(rc.mismatches),
            "estimated_rows": int(r.estimated_rows), "rows": int(r.rows),
            "estimated_share_pct": round(share, 4), "estimated_warn": share > warn_pct,
            "pricing_missing_open": int(open_missing),
            "mismatches": int(r.zero_cost_with_tokens) + int(rc.mismatches)}


async def reconcile(session_factory: async_sessionmaker[AsyncSession], redis=None, *,
                    report_date: date | None = None, metrics=None) -> dict:
    """검사를 돌리고 reconciliation_runs 에 한 행을 남긴다. 결과 dict 를 돌려준다."""
    settings = get_settings()
    tz = settings.reporting_timezone
    day = report_date or (datetime.now(ZoneInfo(tz)).date() - timedelta(days=1))
    period = f"{day.year}-{day.month:02d}"
    started = datetime.now(UTC)
    t0 = time.monotonic()
    checks: dict[str, Any] = {}
    err: str | None = None
    try:
        async with session_factory() as session:
            # 한 스냅샷 — R1~R3·R6 이 같은 시점의 원장을 본다(워커 flush 가 동시에 커밋돼도).
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            secs = int(max(1.0, float(settings.daily_usage_agg_command_timeout)))
            await session.execute(text(f"SET LOCAL statement_timeout = '{secs}s'"))
            checks["raw_vs_daily"] = await _r1(
                session, tz, day, settings.reconciliation_dirty_alert_hours,
                bridge_days=int(settings.daily_usage_agg_hourly_bridge_days),
                hourly_stale_sec=int(settings.reconciliation_hourly_stale_sec),
                hourly_enabled=bool(settings.hourly_rollup_enabled))
            checks["monthly_vs_daily"] = await _r2(session, tz, day.replace(day=1))
            checks["budget_vs_raw"] = await _r3(session, tz, period)
            retro = dict(checks["budget_vs_raw"].get("retro_not_charged") or {})
            retro["mismatches"] = 0  # option B 정보 카테고리 — 미청구는 정책이지 불일치가 아니다
            checks["retro_not_charged"] = retro
            checks["web_search_cost_gap"] = await _ws_cost_gap(session, tz, day)
            checks["pricing"] = await _r6(session, tz, day,
                                          settings.reconciliation_estimated_warn_pct)
            checks["dirty_stale"] = {"mismatches": int((await session.execute(
                text(_DIRTY_STALE_SQL),
                {"h": settings.reconciliation_dirty_alert_hours})).scalar_one())}
            checks["dead_letters_open"] = {"mismatches": int(
                (await session.execute(text(_DEAD_OPEN_SQL))).scalar_one())}
            await session.commit()
        async with session_factory() as session:
            checks["redis_vs_db"] = await _r4(session, redis, settings, period)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — 실패도 실행 기록으로 남긴다
        err = f"{type(exc).__name__}: {exc}"[:2000]
        logger.exception("reconciliation_failed")
    mm = sum(int(c.get("mismatches", 0)) for c in checks.values())
    status = "error" if err else ("mismatch" if mm else "ok")
    result = {"report_date": day.isoformat(), "period": period, "tz": tz, "status": status,
              "mismatch_count": mm, "checks": checks, "error": err,
              "seconds": round(time.monotonic() - t0, 2)}
    try:
        async with session_factory() as session:
            rid = (await session.execute(text(_INSERT_RUN_SQL), {
                "started": started, "day": day, "period": period, "tz": tz, "status": status,
                "mm": mm, "checks": json.dumps(checks, default=_f), "err": err})).scalar_one()
            await session.commit()
        result["run_id"] = str(rid)
    except Exception:
        logger.exception("reconciliation_record_failed")
    LAST.clear()
    LAST.update({k: int(v.get("mismatches", 0)) for k, v in checks.items()})
    if status != "error" and "run_id" in result:
        LAST_SUCCESS["ts"] = time.time()
    if metrics is not None:
        metrics.reconciliation_runs.add(1, {"status": status})
        for k, v in LAST.items():
            metrics.reconciliation_mismatch_total.add(v, {"check": k})
    log = logger.error if status != "ok" else logger.info
    log("reconciliation_done", status=status, mismatches=mm, report_date=day.isoformat(),
        by_check=dict(LAST))
    return result


_LAST_SUCCESS_SQL = """
SELECT extract(epoch FROM max(finished_at)) FROM usage.reconciliation_runs WHERE status <> 'error'
"""


async def load_last_success(session_factory) -> None:
    """기동 시 마지막 성공 시각을 표에서 읽는다 — 재시작이 게이지를 지우지 않게. 실패는 무시."""
    try:
        async with asyncio.timeout(10):
            async with session_factory() as session:
                ts = (await session.execute(text(_LAST_SUCCESS_SQL))).scalar_one_or_none()
                await session.commit()
        if ts is not None:
            LAST_SUCCESS["ts"] = float(ts)
    except Exception:  # noqa: BLE001 — 기동을 막지 않는다(다음 실행이 채운다)
        logger.warning("reconciliation_last_success_load_failed")


async def run_reconciliation(session_factory, redis=None, metrics=None) -> None:
    """APScheduler 진입점 — 자기 advisory lock(727_002). 롤업 락과 독립이다."""
    try:
        async with aggregation_lock(session_factory.kw["bind"],
                                    key=RECONCILIATION_LOCK_KEY) as got:
            if not got:
                logger.info("reconciliation_skipped_lock_held")
                return
            await reconcile(session_factory, redis, metrics=metrics)
    except Exception:
        logger.exception("reconciliation_job_failed")


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m worker.reconciliation")
    ap.add_argument("--date", help="report date YYYY-MM-DD (reporting tz); default yesterday")
    args = ap.parse_args(argv)
    from worker.db import create_aggregator_engine, create_session_factory
    from worker.redis_client import create_redis_client

    settings = get_settings()
    engine = create_aggregator_engine(settings)
    sf = create_session_factory(engine)
    redis = None
    try:
        redis = await create_redis_client(settings)
    except Exception:
        logger.warning("reconciliation_cli_no_redis")
    try:
        async with aggregation_lock(engine, key=RECONCILIATION_LOCK_KEY) as got:
            if not got:
                print(json.dumps({"status": "skipped_lock_held"}))
                return 2
            res = await reconcile(sf, redis, report_date=(
                date.fromisoformat(args.date) if args.date else None))
    finally:
        if redis is not None:
            await redis.aclose()
        await engine.dispose()
    print(json.dumps(res, default=_f, indent=2))
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
