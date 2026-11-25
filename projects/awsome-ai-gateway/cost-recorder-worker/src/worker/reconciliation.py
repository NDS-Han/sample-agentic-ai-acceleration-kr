# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""야간 비용 대조 (wave 3, B8) — 리포팅 tz 01:00, 자기 advisory lock(727_002).

원장들이 서로 같은 숫자를 말하는지 본다(허용 오차 0 — NUMERIC 6자리 그대로).
결과는 ``usage.reconciliation_runs`` 한 행(검사별 JSON)과
지표(``cost_recorder_reconciliation_*``)다.

검사(report_date = 어제, 리포팅 tz):

R1 raw_vs_daily
    usage_logs 하루 vs daily_aggregates — 입도(user,team,dept,model,client,status)별
    토큰·비용·호출 수·웹 검색 수/비용. dirty 날짜는 48 h 까지 pending(불일치 아님).
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
from worker.daily_aggregator import GRAIN_VERSION, _day_window, _month_end, aggregation_lock

logger = structlog.get_logger(__name__)

RECONCILIATION_LOCK_KEY = 727_002

#: 마지막 실행의 검사별 불일치 수 — 지표 게이지 콜백이 읽는다.
LAST: dict[str, int] = {}

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
), ledger AS (
  SELECT scope::text AS scope, scope_id, client::text AS client, used_usd AS amt
  FROM budget.budget_usages
  WHERE period = :period
    AND NOT (scope = 'TEAM' AND scope_id = CAST(:default_team AS uuid))
)
SELECT COUNT(*) AS mismatches,
       COALESCE(SUM(COALESCE(ledger.amt, 0) - COALESCE(exp2.amt, 0)), 0) AS diff,
       (SELECT COALESCE(SUM(amt), 0) FROM expected WHERE scope = 'USER' AND client IS NULL)
         AS raw_total,
       json_agg(json_build_array(COALESCE(ledger.scope, exp2.scope),
                                 COALESCE(ledger.scope_id, exp2.scope_id),
                                 COALESCE(ledger.client, exp2.client),
                                 exp2.amt, ledger.amt))
         FILTER (WHERE true) AS sample
FROM exp2 FULL OUTER JOIN ledger
  ON ledger.scope = exp2.scope AND ledger.scope_id = exp2.scope_id
 AND ledger.client IS NOT DISTINCT FROM exp2.client
WHERE COALESCE(ledger.amt, 0) <> COALESCE(exp2.amt, 0)
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


async def _r1(session: AsyncSession, tz: str, day: date, stale_h: int) -> dict:
    start, end = _day_window(ZoneInfo(tz), day)
    st = (await session.execute(text(_DAY_STATE_SQL),
                                {"day": day, "tz": tz, "grain": GRAIN_VERSION})).one()
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
        age_h = ((datetime.now(UTC) - since).total_seconds() / 3600) if since else None
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
    return {"mismatches": int(row.mismatches), "ledger_minus_expected": _f(row.diff),
            "raw_charged_total": _f(row.raw_total), "sample": sample[:20]}


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
            q = Decimal("0.000001")
            if net.quantize(q) != Decimal(r.used_usd).quantize(q):
                bad.append([r.scope, r.scope_id, r.client, str(r.used_usd), str(net)])
    return {"state": "checked", "checked": checked, "absent_in_redis": absent,
            "lost_sentinel": lost, "mismatches": len(bad), "sample": bad[:20]}


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
            checks["raw_vs_daily"] = await _r1(session, tz, day,
                                               settings.reconciliation_dirty_alert_hours)
            checks["monthly_vs_daily"] = await _r2(session, tz, day.replace(day=1))
            checks["budget_vs_raw"] = await _r3(session, tz, period)
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
    if metrics is not None:
        metrics.reconciliation_runs.add(1, {"status": status})
    log = logger.error if status != "ok" else logger.info
    log("reconciliation_done", status=status, mismatches=mm, report_date=day.isoformat(),
        by_check=dict(LAST))
    return result


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
