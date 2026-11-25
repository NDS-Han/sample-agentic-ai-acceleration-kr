# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""요청 시작 sweeper (wave 3b, B2) — 완료 없이 죽은 요청에 추정 행을 한 번 쓴다.

게이트웨이는 admission 통과 시 ``kind="start"`` 이벤트를 보내고(``usage.request_starts``), 완료
행(end)이
오면 워커가 그 시작을 지운다. 행 없이 끝난 요청은 ``close`` 가 닫는다. 남는 것은 **finalize 없이
끝난
요청** — 파드 SIGKILL/OOM, 종료 grace 초과로 잘린 스트림이다. 업스트림은 프롬프트를 이미 처리해
청구했으므로 행이 없으면 과소청구다.

후보 = 열린 시작 중
  * ``started_at`` 이 ``request_sweep_min_age_sec`` 보다 오래됐고, 그 요청을 처리하던 게이트웨이
    프로세스의 생존 신호(``cost:live:<instance_id>``, 게이트웨이가 10 s 마다 TTL 45 s 로 갱신)가
    없다, 또는
  * ``request_sweep_max_age_sec`` 보다 오래됐다(신호를 읽을 수 없거나 살아 있는 프로세스가 시작을
  흘림).
  그리고 그 request_id 의 이벤트가 DB 폴백 표(``usage.cost_pending_events``)에 대기 중이 아니다.
스트림 lag 이 ``request_sweep_max_lag`` 를 넘으면 쉰다(아직 소비하지 않은 완료가 있을 수 있다).

한 트랜잭션(시작 행 ``FOR UPDATE SKIP LOCKED`` — 완료 flush 는 같은 행을 ``FOR UPDATE`` 로 잠근다):
  1. 추정 행 INSERT(``ON CONFLICT (request_id) DO NOTHING RETURNING``) — 입력 = admission 추정 입력
  토큰,
     출력 0, 비용 = 게이트웨이가 admission 에 계산한 그 입력의 비용, ``estimated_usage=true``,
     ``status=ERROR``. 실제 행이 이미 있으면(경합) 시작만 지운다.
  2. budget_usages 가산(RETURNING 기준 — 실제 삽입분만) + 롤업 dirty 표시 — batch_flusher 와 같은
  함수.
  3. 시작 행에 ``swept_at`` / ``swept_charge_usd`` — 늦게 온 실제 완료는 그 행을 교체하고 예산에는
  차액만
     더한다(batch_flusher._write_ends), 늦게 온 close 는 행을 지우고 추정액을 뺀다(apply_lifecycle).
커밋 뒤: 게이트웨이의 월예산 예약(start 페이로드의 디스크립터)을 추정 청구액으로 정산한다 —
``budget_settle.lua`` 는 마커 기반 멱등이라 게이트웨이가 이미 정산했으면 no-op 이다. 예약을 그대로
두면
사용 카운터가 최악 추정치만큼 부푼 채 남는다.

daily Redis 카운터(``usage:daily:*``)는 올리지 않는다 — 추정 행이 실제 행으로 교체되면 그때 실제
값으로
올린다(이중 계상 방지). 다음날 daily_aggregates 가 정정한다.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text

from worker.config import get_settings
from worker.schemas.cost_stream import CostStreamEntry, RequestStartEvent

logger = structlog.get_logger(__name__)

HEARTBEAT_PREFIX = "cost:live:"

#: gateway-proxy/src/app/redis_scripts/budget_settle.lua 의 사본 — 두 파일이 같은지 테스트가
#: 확인한다
#: (tests/unit/test_wave3b_lifecycle.py). 워커 이미지 빌드 컨텍스트가 워커 디렉터리라 공유할 수
#: 없다.
_SETTLE_LUA = (Path(__file__).parent / "redis_scripts" / "budget_settle.lua").read_text(
    encoding="utf-8")

_SELECT_CANDIDATES = text(
    """
    SELECT request_id, instance_id,
           (started_at < now() - make_interval(secs => CAST(:max_age AS double precision))) AS
           expired
    FROM usage.request_starts
    WHERE closed_at IS NULL AND swept_at IS NULL
      AND started_at < now() - make_interval(secs => CAST(:min_age AS double precision))
    ORDER BY started_at
    LIMIT :lim
    """
)

_SELECT_PENDING = text(
    "SELECT DISTINCT request_id FROM usage.cost_pending_events "
    "WHERE request_id = ANY(CAST(:ids AS varchar[]))"
)

_LOCK_CANDIDATES = text(
    """
    SELECT request_id, payload
    FROM usage.request_starts
    WHERE request_id = ANY(CAST(:ids AS varchar[])) AND closed_at IS NULL AND swept_at IS NULL
    ORDER BY request_id
    FOR UPDATE SKIP LOCKED
    """
)

_MARK_SWEPT = text(
    "UPDATE usage.request_starts SET swept_at = now(), swept_charge_usd = CAST(:charge AS numeric) "
    "WHERE request_id = :request_id"
)

_DELETE_STARTS = text(
    "DELETE FROM usage.request_starts WHERE request_id = ANY(CAST(:ids AS varchar[]))"
)

_PURGE = text(
    """
    DELETE FROM usage.request_starts WHERE request_id IN (
        SELECT request_id FROM usage.request_starts
        WHERE closed_at < now() - make_interval(hours => CAST(:h AS integer))
        UNION ALL
        SELECT request_id FROM usage.request_starts
        WHERE swept_at < now() - make_interval(hours => CAST(:h AS integer))
        LIMIT 5000)
    """
)


def start_to_entry(st: RequestStartEvent, now: datetime | None = None) -> CostStreamEntry:
    """시작 이벤트 → 추정 완료 행."""
    now = now or datetime.now(UTC)
    return CostStreamEntry(
        request_id=st.request_id, user_id=st.user_id, team_id=st.team_id, dept_id=st.dept_id,
        model_alias=st.model_alias, provider=st.provider,
        input_tokens=st.estimated_input_tokens, output_tokens=0,
        cost_usd=st.estimated_cost_usd, web_search_count=0, web_search_cost_usd=Decimal("0"),
        pricing_missing=st.pricing_missing, latency_ms=0, ttft_ms=None,
        is_streaming=st.is_streaming, estimated_usage=True, status="ERROR",
        requested_at=st.requested_at, completed_at=now.isoformat(), period=st.period, date=st.date,
        sso_subject=st.sso_subject, key_prefix=st.key_prefix, client=st.client,
        teamless=st.teamless, session_id=st.session_id,
    )


async def _stream_lag(redis: Any, settings) -> int | None:
    try:
        for g in await redis.xinfo_groups(settings.cost_stream_key) or []:
            name = g.get("name")
            if isinstance(name, bytes):
                name = name.decode()
            if name == settings.cost_stream_group:
                lag = g.get("lag")
                return int(lag if lag is not None else g.get("pending", 0))
        return 0
    except Exception:
        return None


async def _alive(redis: Any, instance_ids: set[str]) -> dict[str, bool] | None:
    """instance → 생존 신호 있음. Redis 를 못 읽으면 None(모름)."""
    out: dict[str, bool] = {}
    try:
        for iid in instance_ids:
            out[iid] = bool(iid) and bool(await redis.exists(HEARTBEAT_PREFIX + iid))
    except Exception:
        logger.warning("request_sweep_heartbeat_read_failed")
        return None
    return out


def _settle_args(resv: dict, charge: Decimal) -> tuple | None:
    """gateway budget_service.settle_eval_args + cost_recorder 의 ARGV 와 같은 모양."""
    try:
        keys = [resv["usage_key"], resv["config_key"], resv["marker_key"], resv["resvsum_key"]]
    except KeyError:
        return None
    if resv.get("idx_key") and resv.get("idx_field"):
        keys.append(resv["idx_key"])
    return (len(keys), *keys, str(charge), resv.get("fallback") or "",
            resv.get("idx_field") or "", resv.get("reserved") or "",
            str(resv.get("marker_ttl") or ""))


async def settle_reservations(redis: Any, st: RequestStartEvent, charge: Decimal,
                              metrics: Any = None) -> int:
    """죽은 요청의 월예산 예약을 추정 청구액으로 정산한다(멱등 — 이미 정산됐으면 no-op)."""
    if redis is None or not st.budget_reservation:
        return 0
    settled = 0
    for resv in st.budget_reservation:
        args = _settle_args(resv, charge) if isinstance(resv, dict) else None
        if args is None:
            continue
        try:
            raw = await redis.eval(_SETTLE_LUA, *args)
            res = json.loads(raw) if raw else {}
            if res.get("settled"):
                settled += 1
        except Exception:
            logger.warning("request_sweep_settle_failed", request_id=st.request_id,
                           scope=resv.get("scope"))
            if metrics is not None and hasattr(metrics, "request_sweeps"):
                metrics.request_sweeps.add(1, {"outcome": "settle_failed"})
    return settled


async def _sweep_ids(session_factory, flusher, ids: list[str], ws_price: Decimal
                    ) -> tuple[list[tuple[RequestStartEvent, Decimal]], int]:
    swept: list[tuple[RequestStartEvent, Decimal]] = []
    raced = 0
    async with session_factory() as session:
        locked = (await session.execute(_LOCK_CANDIDATES, {"ids": ids})).all()
        starts: list[RequestStartEvent] = []
        bad: list[str] = []
        for r in locked:
            try:
                starts.append(RequestStartEvent.model_validate_json(r.payload))
            except Exception:
                bad.append(r.request_id)
        if bad:
            # 읽을 수 없는 시작 — 추정할 근거가 없다. 지운다.
            logger.warning("request_sweep_unreadable_start", count=len(bad))
            await session.execute(_DELETE_STARTS, {"ids": bad})
        entries = [start_to_entry(st) for st in starts]
        await flusher._resolve_depts(entries)
        inserted = (await flusher._insert_usage_logs(session, entries, ws_price)
                    if entries else set())
        written = [e for e in entries if e.request_id in inserted]
        await flusher._upsert_budget_usages(session, written, ws_price)
        await flusher._mark_late_days_dirty(session, written)
        by_id = {st.request_id: st for st in starts}
        for e in written:
            await session.execute(_MARK_SWEPT, {"request_id": e.request_id,
                                                "charge": str(e.cost_usd)})
            swept.append((by_id[e.request_id], e.cost_usd))
        lost_race = [e.request_id for e in entries if e.request_id not in inserted]
        if lost_race:
            # 실제 행이 이미 있다(완료가 시작보다 먼저/동시에 도착) — 이 시작은 끝난 요청이다.
            raced = len(lost_race)
            await session.execute(_DELETE_STARTS, {"ids": lost_race})
        await session.commit()
    return swept, raced


async def _dead_letter_start(session_factory, flusher, request_id: str, exc: BaseException) -> None:
    async with session_factory() as session:
        row = (await session.execute(
            text("SELECT payload FROM usage.request_starts WHERE request_id = :r"),
            {"r": request_id})).first()
        payload = row[0] if row else ""
    try:
        st = RequestStartEvent.model_validate_json(payload)
        entry = start_to_entry(st)
        entry._raw = payload
    except Exception:
        entry = None
    if entry is not None:
        await flusher._store_dead_letters([(entry, None, "sweep_permanent_error", exc)])
    async with session_factory() as session:
        await session.execute(_DELETE_STARTS, {"ids": [request_id]})
        await session.commit()
    logger.error("request_sweep_dead_lettered", request_id=request_id, error=str(exc)[:200])


async def sweep_once(session_factory, redis: Any, flusher, *, metrics: Any = None,
                     settings: Any = None) -> int:
    """한 배치를 쓸어 담는다. 추정 행을 쓴 수를 돌려준다."""
    settings = settings or get_settings()
    lag = await _stream_lag(redis, settings) if redis is not None else None
    if lag is not None and lag > int(settings.request_sweep_max_lag):
        logger.info("request_sweep_skipped_lag", lag=lag)
        return 0
    async with session_factory() as session:
        rows = (await session.execute(_SELECT_CANDIDATES, {
            "min_age": float(settings.request_sweep_min_age_sec),
            "max_age": float(settings.request_sweep_max_age_sec),
            "lim": int(settings.request_sweep_batch_size)})).all()
        if not rows:
            return 0
        alive = await _alive(redis, {r.instance_id for r in rows if not r.expired}) \
            if redis is not None else None
        orphan = [r.request_id for r in rows
                  if r.expired or (alive is not None and not alive.get(r.instance_id, False))]
        if not orphan:
            return 0
        pending = {r[0] for r in (await session.execute(_SELECT_PENDING, {"ids": orphan})).all()}
        orphan = [rid for rid in orphan if rid not in pending]
    if not orphan:
        return 0

    ws_price = await flusher._web_search_price()
    try:
        swept, raced = await _sweep_ids(session_factory, flusher, orphan, ws_price)
    except Exception as exc:
        from worker.batch_flusher import _is_permanent_flush_error

        if not _is_permanent_flush_error(exc):
            raise
        # 영구 오류(삭제된 사용자 FK 등) — 한 건씩, 실패한 시작은 dead-letter 로 보내고 지운다.
        swept, raced = [], 0
        for rid in orphan:
            try:
                s1, r1 = await _sweep_ids(session_factory, flusher, [rid], ws_price)
                swept += s1
                raced += r1
            except Exception as one:
                if not _is_permanent_flush_error(one):
                    raise
                await _dead_letter_start(session_factory, flusher, rid, one)

    for st, charge in swept:
        await settle_reservations(redis, st, charge, metrics)
    if swept:
        logger.warning("requests_swept_estimated", count=len(swept),
                       request_ids=[st.request_id for st, _ in swept][:20])
    if metrics is not None and hasattr(metrics, "request_sweeps"):
        if swept:
            metrics.request_sweeps.add(len(swept), {"outcome": "swept"})
        if raced:
            metrics.request_sweeps.add(raced, {"outcome": "raced"})
    return len(swept)


async def purge_once(session_factory, *, settings: Any = None) -> int:
    settings = settings or get_settings()
    async with session_factory() as session:
        res = await session.execute(_PURGE, {"h": int(settings.request_starts_retention_hours)})
        await session.commit()
    return int(getattr(res, "rowcount", 0) or 0)


async def run_sweep(session_factory, redis=None, flusher=None, metrics=None) -> None:
    """APScheduler 진입점 — 예외를 삼키고 로그만 남긴다(다음 주기에 다시)."""
    try:
        settings = get_settings()
        while await sweep_once(session_factory, redis, flusher, metrics=metrics,
                               settings=settings) >= int(settings.request_sweep_batch_size):
            pass
        await purge_once(session_factory, settings=settings)
    except Exception:
        logger.exception("request_sweep_failed")
