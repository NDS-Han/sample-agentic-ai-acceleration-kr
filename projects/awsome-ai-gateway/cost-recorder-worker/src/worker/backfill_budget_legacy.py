# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""일회성 예산 원장 보정(wave 3c) — wave 3 이전 행의 웹 검색 비용을 budget_usages 에 더한다.

    python -m worker.backfill_budget_legacy --cutoff 2026-11-25T11:07:47Z
        [--period YYYY-MM ...] [--apply] [--kinds web_search,rounding]
        [--batch-size 100] [--boundary-sec 600] [--allow-boundary]
        [--wait-lock 60] [--no-redis] [--resync-redis] [--out plan.json]

왜: wave 3(DECISION 2)부터 예산 청구액 = 모델 비용 + 웹 검색 비용이다. 그 전에 기록된 행은
``usage_logs.web_search_cost_usd`` 는 있지만 budget_usages(와 Redis 카운터)에는 들어가지 않았다.
야간 대조 R3(budget_vs_raw)은 그 차이를 그 달 내내 불일치로 보고한다(dev 실측, wave 3 SMOKE).

``--cutoff`` = wave 3 워커가 기록을 넘겨받은 시각(이전 워커 파드가 끝난 롤아웃 완료 시각 —
``helm history`` 의 해당 revision 시각). ``requested_at < cutoff`` 인 행이 \"wave 3 이전\"이다.

보정 종류(``--kinds``, 기본 둘 다):

web_search
    스코프별 Σ web_search_cost_usd (cutoff 이전 행). 워커와 같은 스코프 규칙 — USER 총합,
    TEAM(팀 없는 사용자의 센티널 팀 제외), 앱별(claude-code/cowork/codex). NULL(0058 이전 미추적)은
    0 — 대조 R3 과 같은 정의다.
rounding
    0062 이전 budget_usages.used_usd 는 NUMERIC(12,4) 였다 — flush 마다 소수 4자리로 반올림돼
    스코프마다 µ$ 단위 잔차가 남았다(dev: $0.000024 / $0.000078 / −$0.000085). 대조 R3 이 비교하는
    스코프에서 ``잔차 = 기대값 − (원장 + web_search 보정)`` 이 **반올림만으로 설명되는 범위**
    (|잔차| ≤ $0.00005 × cutoff 이전 행 수 — upsert 1회당 최대 반올림 오차)일 때만 맞춘다.
    그보다 큰 차이는 고치지 않고 ``unexplained`` 로 보고한다 — 진짜 불일치는 대조가 계속 잡는다.

안전장치:
- 기본은 dry-run(계획만 출력, 아무것도 쓰지 않는다). ``--apply`` 로만 쓴다.
- 멱등: 보정마다 ``audit.audit_logs`` 한 행(action=BACKFILL_BUDGET_LEGACY,
  resource_id=``<kind>:<scope>:<id>:<client>:<period>``)을 같은 트랜잭션에 남기고, 이미 있는 보정은
  건너뛴다. 다시 돌려도 0 건이다. 실행 요약도 audit 한 행(resource_type=BudgetBackfill).
- 동시성: 비용 대조 advisory lock(727_002)을 잡는다 — 대조가 반쯤 적용된 원장을 보지 않는다.
  계획은 REPEATABLE READ 한 스냅샷에서 만들고, 적용은 상대 가산(used_usd + delta)이라 그 사이
  워커 flush(행과 원장을 한 트랜잭션에 같이 더한다)와 섞여도 잔차가 변하지 않는다.
- 잠금: 배치(``--batch-size`` 보정)마다 한 트랜잭션, ``lock_timeout 5s`` + 재시도(최대 5회),
  ``statement_timeout 30s``. budget_usages 행 잠금만 잡는다(테이블 잠금·재작성 없음).
- 경계: cutoff 직전 행 중 완료가 cutoff−``--boundary-sec`` 이후인 웹 검색 행은 새 워커가 이미 더했을
  수 있다 — 있으면 적용을 거부한다(exit 3). 확인 뒤 ``--allow-boundary`` 로 포함한다.
- Redis: 커밋 뒤 web_search 보정분을 카운터에 더한다(키가 있을 때만 — 없으면 게이트웨이가 DB 에서
  재구축한다, repricer 와 같은 규칙). 실패하면 ``<key>:lost`` 센티넬을 세워 다음 요청이 DB 로
  재구축한다. ``--resync-redis`` 는 이미 감사된 보정 스코프 전부에 센티넬을 다시 세운다(복구용).
  rounding 은 Redis 에 적용하지 않는다(Redis 는 4자리 반올림을 겪지 않았다).

워커 pod 안에서 워커와 같은 env(DB_URL·REDIS·REPORTING_TIMEZONE)로 실행한다.
종료 코드: 0 성공(dry-run 포함) · 1 오류 · 2 lock 을 못 잡음 · 3 경계 행 확인 필요.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from worker.batch_flusher import _PER_APP_CLIENTS, _UPSERT_BUDGET_USAGE, DEFAULT_TEAM_ID
from worker.config import get_settings
from worker.daily_aggregator import _day_window, _month_end, aggregation_lock
from worker.reconciliation import RECONCILIATION_LOCK_KEY

logger = structlog.get_logger(__name__)

ACTION = "BACKFILL_BUDGET_LEGACY"
SYSTEM_USER_ID = "00000000-0000-4000-a000-000000000010"
KINDS = ("web_search", "rounding")
#: NUMERIC(12,4) upsert 1회의 최대 반올림 오차.
ROUNDING_STEP = Decimal("0.00005")
_LOST_TTL = 3_888_000  # 45 d — 게이트웨이 센티넬과 같다

#: 스코프별 기대값(R3 과 같은 정의) + cutoff 이전 웹 검색 비용·행 수 + 원장 + 기존 감사.
_PLAN_SQL = """
WITH raw AS (
  SELECT l.user_id, l.team_id, l.client, (u.team_id IS NULL) AS teamless,
         l.requested_at < :cutoff AS pre,
         l.cost_usd + COALESCE(l.web_search_cost_usd, 0) AS amt,
         COALESCE(l.web_search_cost_usd, 0) AS ws
  FROM usage.usage_logs l LEFT JOIN auth.users u ON u.id = l.user_id
  WHERE l.requested_at >= :start AND l.requested_at < :end
), scopes AS (
  SELECT 'USER'::text AS scope, user_id AS scope_id, NULL::text AS client, true AS comparable,
         SUM(amt) AS amt, COALESCE(SUM(ws) FILTER (WHERE pre), 0) AS ws_pre,
         COUNT(*) FILTER (WHERE pre) AS n_pre
    FROM raw GROUP BY user_id
  UNION ALL
  -- 워커 규칙: 팀 없는 사용자의 센티널 팀만 뺀다. 대조 R3 은 센티널 팀 전체를 비교하지 않는다.
  SELECT 'TEAM', team_id, NULL, team_id <> CAST(:default_team AS uuid),
         SUM(amt), COALESCE(SUM(ws) FILTER (WHERE pre), 0), COUNT(*) FILTER (WHERE pre)
    FROM raw WHERE NOT (teamless AND team_id = CAST(:default_team AS uuid)) GROUP BY team_id
  UNION ALL
  SELECT 'USER', user_id, client, true,
         SUM(amt), COALESCE(SUM(ws) FILTER (WHERE pre), 0), COUNT(*) FILTER (WHERE pre)
    FROM raw WHERE client = ANY(CAST(:apps AS text[])) GROUP BY user_id, client
), seeds AS (
  SELECT split_part(resource_id, ':', 1) AS scope,
         CAST(split_part(resource_id, ':', 2) AS uuid) AS scope_id,
         NULLIF(split_part(resource_id, ':', 3), '') AS client,
         SUM(CAST(changes->>'after' AS numeric) - CAST(changes->>'before' AS numeric)) AS amt
  FROM audit.audit_logs
  WHERE action = 'SEED_BUDGET_SPENT' AND result = 'SUCCESS'
    AND split_part(resource_id, ':', 4) = :period
  GROUP BY 1, 2, 3
), done AS (
  SELECT resource_id FROM audit.audit_logs
  WHERE action = :action AND resource_type = 'BudgetUsage' AND result = 'SUCCESS'
    AND resource_id LIKE '%:' || :period
)
SELECT s.scope, s.scope_id::text AS scope_id, s.client, s.comparable,
       s.amt + COALESCE(sd.amt, 0) AS expected, s.ws_pre, s.n_pre,
       b.used_usd AS ledger,
       EXISTS (SELECT 1 FROM done WHERE resource_id = 'web_search:' || s.scope || ':'
               || s.scope_id::text || ':' || COALESCE(s.client, '') || ':' || :period) AS ws_done,
       EXISTS (SELECT 1 FROM done WHERE resource_id = 'rounding:' || s.scope || ':'
               || s.scope_id::text || ':' || COALESCE(s.client, '') || ':' || :period) AS rd_done
FROM scopes s
LEFT JOIN seeds sd ON sd.scope = s.scope AND sd.scope_id = s.scope_id
                  AND sd.client IS NOT DISTINCT FROM s.client
LEFT JOIN budget.budget_usages b ON b.period = :period AND b.scope::text = s.scope
                                AND b.scope_id = s.scope_id
                                AND b.client IS NOT DISTINCT FROM s.client
ORDER BY 1, 2, 3 NULLS FIRST
"""

#: cutoff 직전에 끝난 웹 검색 행 — 새 워커가 이미 예산에 더했을 수 있다.
_BOUNDARY_SQL = """
SELECT request_id, requested_at, completed_at, web_search_cost_usd
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :cutoff
  AND completed_at >= :edge AND COALESCE(web_search_cost_usd, 0) > 0
ORDER BY completed_at LIMIT 50
"""

_DONE_ONE_SQL = """
SELECT 1 FROM audit.audit_logs
WHERE action = :action AND resource_type = 'BudgetUsage' AND resource_id = :rid
  AND result = 'SUCCESS' LIMIT 1
"""

_LOCK_ROW_SQL = """
SELECT used_usd FROM budget.budget_usages
WHERE scope = CAST(:scope AS budget.budget_scope) AND scope_id = CAST(:scope_id AS uuid)
  AND period = :period AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
FOR UPDATE
"""

_AUDIT_SQL = """
INSERT INTO audit.audit_logs
  (actor_user_id, actor_role, action, resource_type, resource_id, changes, result,
   ip_address, request_id)
VALUES (CAST(:actor AS uuid), 'SYSTEM', :action, :rtype, :rid, CAST(:changes AS jsonb),
        'SUCCESS', '127.0.0.1', :run_id)
"""

_AUDITED_WS_SQL = """
SELECT resource_id FROM audit.audit_logs
WHERE action = :action AND resource_type = 'BudgetUsage' AND result = 'SUCCESS'
  AND resource_id LIKE 'web_search:%:' || :period
"""

_REDIS_INCR_IF_EXISTS = (
    "if redis.call('EXISTS', KEYS[1]) == 1 "
    "then return redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]) else return false end"
)
_REDIS_LOST_IF_EXISTS = (
    "if redis.call('EXISTS', KEYS[1]) == 1 "
    "then redis.call('SET', KEYS[1] .. ':lost', '1', 'EX', ARGV[1]) return 1 else return 0 end"
)


@dataclass
class Fix:
    kind: str
    scope: str
    scope_id: str
    client: str | None
    period: str
    delta: Decimal
    detail: dict = field(default_factory=dict)

    @property
    def rid(self) -> str:
        return f"{self.kind}:{self.scope}:{self.scope_id}:{self.client or ''}:{self.period}"

    def redis_key(self) -> str:
        if self.client:
            return f"budget:user:{{{self.scope_id}}}:{self.client}:{self.period}"
        s = "user" if self.scope == "USER" else "team"
        return f"budget:{s}:{{{self.scope_id}}}:{self.period}"


@dataclass
class Plan:
    period: str
    cutoff: str
    fixes: list[Fix] = field(default_factory=list)
    already_done: int = 0
    unexplained: list[dict] = field(default_factory=list)
    boundary_rows: list[dict] = field(default_factory=list)
    scopes: int = 0

    def summary(self) -> dict:
        by: dict[str, dict[str, Any]] = {}
        for f in self.fixes:
            b = by.setdefault(f.kind, {"count": 0, "sum_usd": Decimal(0)})
            b["count"] += 1
            b["sum_usd"] += f.delta
        return {"period": self.period, "cutoff": self.cutoff, "scopes": self.scopes,
                "fixes": {k: {"count": v["count"], "sum_usd": str(v["sum_usd"])}
                          for k, v in by.items()},
                "already_done": self.already_done, "unexplained": self.unexplained,
                "boundary_rows": self.boundary_rows,
                "detail": [{**asdict(f), "delta": str(f.delta)} for f in self.fixes]}


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def _period_window(tz: str, period: str) -> tuple[datetime, datetime]:
    first = date(int(period[:4]), int(period[5:7]), 1)
    zone = ZoneInfo(tz)
    return (_day_window(zone, first)[0],
            _day_window(zone, _month_end(first) + timedelta(days=1))[0])


async def build_plan(session_factory, *, tz: str, period: str, cutoff: datetime,
                     kinds: tuple[str, ...] = KINDS, boundary_sec: float = 600.0) -> Plan:
    """한 스냅샷(REPEATABLE READ, 읽기 전용)에서 보정 계획을 만든다. 아무것도 쓰지 않는다."""
    start, end = _period_window(tz, period)
    plan = Plan(period=period, cutoff=cutoff.isoformat())
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        rows = (await session.execute(text(_PLAN_SQL), {
            "start": start, "end": end, "cutoff": cutoff, "period": period,
            "default_team": DEFAULT_TEAM_ID, "apps": list(_PER_APP_CLIENTS),
            "action": ACTION})).all()
        edge = cutoff - timedelta(seconds=boundary_sec)
        plan.boundary_rows = [
            {"request_id": r.request_id, "requested_at": r.requested_at.isoformat(),
             "completed_at": r.completed_at.isoformat(), "web_search_cost_usd": str(r[3])}
            for r in await session.execute(text(_BOUNDARY_SQL), {
                "start": start, "cutoff": cutoff, "edge": edge})]
        await session.commit()
    plan.scopes = len(rows)
    for r in rows:
        key = dict(scope=r.scope, scope_id=r.scope_id, client=r.client, period=period)
        ws_pre, ledger = _d(r.ws_pre), r.ledger
        ws_counted = bool(r.ws_done)  # 원장에 이미 들어간 웹 검색 보정
        if r.ws_done:
            plan.already_done += 1
        elif ws_pre > 0 and "web_search" in kinds:
            plan.fixes.append(Fix("web_search", delta=ws_pre,
                                  detail={"rows_pre": int(r.n_pre)}, **key))
            ws_counted = True
        if "rounding" not in kinds or not r.comparable:
            continue
        if r.rd_done:
            plan.already_done += 1
            continue
        if ws_pre > 0 and not ws_counted:
            continue  # 웹 검색 몫을 모르면 잔차를 나눌 수 없다
        residue = _d(r.expected) - (_d(ledger) + (ws_pre if (ws_pre > 0 and not r.ws_done)
                                                  else Decimal(0)))
        if residue == 0:
            continue
        cap = ROUNDING_STEP * int(r.n_pre)
        info = {"expected": str(_d(r.expected)), "ledger": None if ledger is None else str(ledger),
                "residue": str(residue), "cap": str(cap), "rows_pre": int(r.n_pre)}
        if ledger is not None and abs(residue) <= cap:
            plan.fixes.append(Fix("rounding", delta=residue, detail=info, **key))
        else:
            plan.unexplained.append({**key, **info})
    return plan


def _is_lock_timeout(exc: BaseException) -> bool:
    code = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
        getattr(getattr(exc, "orig", None), "__cause__", None), "sqlstate", None)
    return code in ("55P03", "40001", "40P01")


async def _apply_batch(session_factory, batch: list[Fix], *, run_id: str, cutoff: str
                       ) -> list[Fix]:
    """한 트랜잭션 — 이미 감사된 보정은 건너뛴다. 적용한 보정 목록을 돌려준다."""
    applied: list[Fix] = []
    async with session_factory() as session:
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
        for f in batch:
            if (await session.execute(text(_DONE_ONE_SQL),
                                      {"action": ACTION, "rid": f.rid})).first():
                continue
            p = {"scope": f.scope, "scope_id": f.scope_id, "period": f.period,
                 "client": f.client}
            before = (await session.execute(text(_LOCK_ROW_SQL), p)).scalar_one_or_none()
            if before is None and f.delta < 0:
                continue  # 없는 원장 행을 음수로 만들지 않는다
            await session.execute(_UPSERT_BUDGET_USAGE, {**p, "cost": str(f.delta)})
            changes = {"kind": f.kind, "before": str(_d(before)),
                       "after": str(_d(before) + f.delta), "delta": str(f.delta),
                       "cutoff": cutoff, "run_id": run_id, **f.detail}
            await session.execute(text(_AUDIT_SQL), {
                "actor": SYSTEM_USER_ID, "action": ACTION, "rtype": "BudgetUsage",
                "rid": f.rid, "changes": json.dumps(changes), "run_id": run_id})
            applied.append(f)
        await session.commit()
    return applied


async def _apply_with_retry(session_factory, batch, **kw) -> list[Fix]:
    for attempt in range(5):
        try:
            return await _apply_batch(session_factory, batch, **kw)
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == 4:
                raise
            logger.warning("budget_backfill_lock_retry", attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


async def _redis_add(redis, fixes: list[Fix]) -> dict:
    out = {"incremented": 0, "absent": 0, "sentinel": 0, "failed": 0}
    for f in fixes:
        if f.kind != "web_search":
            continue
        key = f.redis_key()
        try:
            r = await redis.eval(_REDIS_INCR_IF_EXISTS, 1, key, str(f.delta))
            out["incremented" if r is not None else "absent"] += 1
        except Exception:  # noqa: BLE001 — 센티넬로 넘긴다(다음 요청이 DB 로 재구축)
            try:
                await redis.set(f"{key}:lost", "1", ex=_LOST_TTL)
                out["sentinel"] += 1
            except Exception:  # noqa: BLE001
                out["failed"] += 1
                logger.error("budget_backfill_redis_failed", key=key)
    return out


async def resync_redis(session_factory, redis, period: str) -> dict:
    """감사된 web_search 보정 스코프 전부에 :lost 센티넬(키가 있을 때) — 멱등 복구 경로."""
    async with session_factory() as session:
        rids = [r[0] for r in await session.execute(text(_AUDITED_WS_SQL),
                                                    {"action": ACTION, "period": period})]
        await session.commit()
    marked = 0
    for rid in rids:
        _, scope, sid, client, per = rid.split(":", 4)
        key = Fix("web_search", scope, sid, client or None, per, Decimal(0)).redis_key()
        marked += int(await redis.eval(_REDIS_LOST_IF_EXISTS, 1, key, str(_LOST_TTL)) or 0)
    return {"audited_scopes": len(rids), "sentinels_set": marked}


async def apply_plan(session_factory, plan: Plan, *, redis=None, batch_size: int = 100,
                     run_id: str | None = None) -> dict:
    run_id = run_id or str(uuid.uuid4())
    applied: list[Fix] = []
    redis_out = {"incremented": 0, "absent": 0, "sentinel": 0, "failed": 0}
    for i in range(0, len(plan.fixes), max(1, batch_size)):
        done = await _apply_with_retry(session_factory, plan.fixes[i:i + batch_size],
                                       run_id=run_id, cutoff=plan.cutoff)
        applied += done
        if redis is not None and done:
            for k, v in (await _redis_add(redis, done)).items():
                redis_out[k] += v
    summary = {"run_id": run_id, "period": plan.period, "applied": len(applied),
               "applied_sum_usd": {k: str(sum((f.delta for f in applied if f.kind == k),
                                              Decimal(0))) for k in KINDS},
               "skipped_already_done": len(plan.fixes) - len(applied) + plan.already_done,
               "unexplained": len(plan.unexplained), "redis": redis_out}
    async with session_factory() as session:
        await session.execute(text(_AUDIT_SQL), {
            "actor": SYSTEM_USER_ID, "action": ACTION, "rtype": "BudgetBackfill",
            "rid": f"{plan.period}:{run_id}", "changes": json.dumps(summary),
            "run_id": run_id})
        await session.commit()
    logger.warning("budget_backfill_applied", **{k: v for k, v in summary.items()
                                                 if k != "applied_sum_usd"})
    return summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m worker.backfill_budget_legacy")
    p.add_argument("--cutoff", required=True,
                   help="wave-3 worker takeover time, ISO-8601 with offset (e.g. ...Z)")
    p.add_argument("--period", action="append",
                   help="YYYY-MM (repeatable); default: current period (reporting tz)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--kinds", default=",".join(KINDS))
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--boundary-sec", type=float, default=600.0)
    p.add_argument("--allow-boundary", action="store_true")
    p.add_argument("--wait-lock", type=float, default=60.0)
    p.add_argument("--no-redis", action="store_true")
    p.add_argument("--resync-redis", action="store_true")
    p.add_argument("--out", help="also write the plan/result JSON here")
    return p.parse_args(argv)


async def run(argv: list[str] | None = None, *, session_factory=None, redis=None) -> int:
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
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if not kinds or any(k not in KINDS for k in kinds):
        print(f"--kinds must be a subset of {','.join(KINDS)}", file=sys.stderr)
        return 1
    now_local = datetime.now(ZoneInfo(tz))
    periods = args.period or [f"{now_local.year}-{now_local.month:02d}"]
    engine = None
    own_redis = False
    if session_factory is None:
        from worker.db import create_aggregator_engine, create_session_factory

        engine = create_aggregator_engine(settings)
        session_factory = create_session_factory(engine)
    if redis is None and not args.no_redis and (args.apply or args.resync_redis):
        from worker.redis_client import create_redis_client

        try:
            redis = await create_redis_client(settings)
            own_redis = True
        except Exception:  # noqa: BLE001
            print("redis unavailable — run again with --resync-redis later", file=sys.stderr)
    if args.no_redis:
        redis = None
    result: dict[str, Any] = {"mode": "apply" if args.apply else "dry-run", "tz": tz,
                              "kinds": list(kinds), "periods": {}}
    rc = 0
    try:
        async with aggregation_lock(session_factory.kw["bind"], args.wait_lock,
                                    key=RECONCILIATION_LOCK_KEY) as got:
            if not got:
                print("could not take the reconciliation lock (727002) — rerun later",
                      file=sys.stderr)
                return 2
            for period in periods:
                if args.resync_redis:
                    if redis is None:
                        print("--resync-redis needs Redis", file=sys.stderr)
                        return 1
                    result["periods"][period] = {
                        "resync_redis": await resync_redis(session_factory, redis, period)}
                    continue
                plan = await build_plan(session_factory, tz=tz, period=period, cutoff=cutoff,
                                        kinds=kinds, boundary_sec=args.boundary_sec)
                entry: dict[str, Any] = {"plan": plan.summary()}
                if args.apply:
                    if plan.boundary_rows and not args.allow_boundary:
                        entry["refused"] = ("web-search rows completed near the cutoff — the "
                                            "wave-3 worker may already have added them; check "
                                            "and rerun with --allow-boundary")
                        rc = 3
                    else:
                        entry["applied"] = await apply_plan(
                            session_factory, plan, redis=redis, batch_size=args.batch_size)
                result["periods"][period] = entry
    finally:
        if own_redis and redis is not None:
            await redis.aclose()
        if engine is not None:
            await engine.dispose()
    out = json.dumps(result, indent=2, default=str)
    print(out)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out)
    return rc


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except SystemExit:
        raise
    except Exception:
        logger.exception("backfill_budget_legacy.failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
