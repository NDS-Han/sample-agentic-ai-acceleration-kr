# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""단가 누락 행 재가격 (wave 3, B7).

``usage_logs.pricing_missing`` 행에 단가가 생기면 비용을 채운다.

게이트웨이는 단가 행이 없는 모델을 $0 으로 서빙하면서 행에 ``pricing_missing=true`` 를 싣는다
("조용한 $0" 금지). 관리자가 단가를 등록하면 이 잡이 **요청 시각에 유효했던 단가**(없으면 그 뒤
가장 이른 단가)로 비용을 계산해 채운다.

한 트랜잭션(행 잠금 FOR UPDATE SKIP LOCKED — 다중 replica 안전):
  1. usage_logs.cost_usd 갱신 + pricing_missing=false
  2. budget_usages 에 같은 델타 가산(USER 총합 / TEAM — 팀 없는 사용자 제외 / 앱별)
  3. 롤업 dirty 표시 — 이미 집계된 시간·날짜가 다시 집계된다(batch_flusher 와 같은 규칙)
커밋 뒤 Redis 예산 카운터에도 같은 델타(키가 있을 때만 — 없으면 게이트웨이가 DB 에서 재구축).

비용 식은 gateway ``cost_recorder.calculate_cost`` 의 단일 턴 판(long-context 티어 포함). 행에는
1h/5m 캐시 쓰기 분해와 웹 검색 턴별 프롬프트가 없으므로 캐시 쓰기는 5m 단가, 티어는 행 합계로
판정한다 — 그 근사는 이 잡이 채운 행에만 해당하고 로그(``repriced``)로 남는다.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.batch_flusher import (
    _INSERT_DIRTY_MARKS,
    _PER_APP_CLIENTS,
    _UPSERT_BUDGET_USAGE,
    DEFAULT_TEAM_ID,
    _late_marks,
)
from worker.config import get_settings

logger = structlog.get_logger(__name__)

_Q = Decimal("0.000001")

#: 단가 누락 행 + 요청 시각의 유효 단가(없으면 그 뒤 가장 이른 단가). 단가가 아직 없으면 제외.
_SELECT_SQL = text(
    """
    SELECT l.id, l.request_id, l.user_id::text AS user_id, l.team_id::text AS team_id,
           l.client, l.requested_at, l.input_tokens, l.output_tokens,
           l.cache_creation_tokens, l.cache_read_tokens,
           (u.team_id IS NULL) AS teamless,
           p.input_price_per_1k_tokens AS pi, p.output_price_per_1k_tokens AS po,
           p.cache_creation_5m_price_per_1k_tokens AS pcw,
           p.cache_read_price_per_1k_tokens AS pcr,
           p.long_context_threshold_tokens AS lt,
           p.long_context_input_price_per_1k_tokens AS lpi,
           p.long_context_output_price_per_1k_tokens AS lpo,
           p.long_context_cache_creation_5m_price_per_1k_tokens AS lpcw,
           p.long_context_cache_read_price_per_1k_tokens AS lpcr
    FROM usage.usage_logs l
    LEFT JOIN auth.users u ON u.id = l.user_id
    CROSS JOIN LATERAL (
        SELECT * FROM model.model_pricings mp
        WHERE mp.model_alias = l.model_alias
        ORDER BY (mp.effective_from <= l.requested_at
                  AND (mp.effective_until IS NULL OR mp.effective_until > l.requested_at)) DESC,
                 CASE WHEN mp.effective_from <= l.requested_at THEN mp.effective_from END DESC,
                 mp.effective_from ASC
        LIMIT 1
    ) p
    WHERE l.pricing_missing
    ORDER BY l.requested_at
    LIMIT :lim
    FOR UPDATE OF l SKIP LOCKED
    """
)

_UPDATE_SQL = text(
    "UPDATE usage.usage_logs SET cost_usd = CAST(:cost AS numeric), pricing_missing = false "
    "WHERE id = CAST(:id AS uuid) AND pricing_missing"
)

_REDIS_INCR_IF_EXISTS = (
    "if redis.call('EXISTS', KEYS[1]) == 1 "
    "then return redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]) else return false end"
)


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def row_cost(r: Any) -> Decimal:
    """gateway calculate_cost 의 단일 턴 판 — 위 모듈 docstring 의 근사 참고."""
    prompt = int(r.input_tokens) + int(r.cache_read_tokens) + int(r.cache_creation_tokens)
    long_ctx = r.lt is not None and prompt > int(r.lt)

    def rate(short, long_):
        return _d(long_) if (long_ctx and long_ is not None) else _d(short)

    cost = (
        Decimal(int(r.input_tokens)) / 1000 * rate(r.pi, r.lpi)
        + Decimal(int(r.output_tokens)) / 1000 * rate(r.po, r.lpo)
        + Decimal(int(r.cache_creation_tokens)) / 1000 * rate(r.pcw, r.lpcw)
        + Decimal(int(r.cache_read_tokens)) / 1000 * rate(r.pcr, r.lpcr)
    )
    return cost.quantize(_Q, rounding=ROUND_HALF_UP)


class _Mark:
    """_late_marks 가 읽는 최소 형태(requested_at ISO)."""

    def __init__(self, ts: datetime) -> None:
        self.requested_at = ts.isoformat()


async def reprice_once(session_factory: async_sessionmaker[AsyncSession], redis: Any = None,
                       *, limit: int | None = None, metrics: Any = None) -> int:
    """한 배치를 재가격한다. 채운 행 수를 돌려준다."""
    settings = get_settings()
    tz = settings.reporting_timezone
    zone = ZoneInfo(tz)
    lim = int(limit or settings.reprice_batch_size)
    user_d: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    team_d: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    app_d: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    marks: list[_Mark] = []
    done = 0
    async with session_factory() as session:
        rows = (await session.execute(_SELECT_SQL, {"lim": lim})).all()
        for r in rows:
            cost = row_cost(r)
            await session.execute(_UPDATE_SQL, {"cost": str(cost), "id": str(r.id)})
            done += 1
            marks.append(_Mark(r.requested_at))
            if cost <= 0:
                continue
            local = r.requested_at.astimezone(zone)
            period = f"{local.year}-{local.month:02d}"
            user_d[(r.user_id, period)] += cost
            if not (r.teamless and r.team_id == DEFAULT_TEAM_ID):
                team_d[(r.team_id, period)] += cost
            if r.client in _PER_APP_CLIENTS:
                app_d[(r.user_id, period, r.client)] += cost
        params = (
            [{"scope": "USER", "scope_id": u, "period": p, "cost": str(c), "client": None}
             for (u, p), c in sorted(user_d.items())]
            + [{"scope": "TEAM", "scope_id": t, "period": p, "cost": str(c), "client": None}
               for (t, p), c in sorted(team_d.items())]
            + [{"scope": "USER", "scope_id": u, "period": p, "cost": str(c), "client": cl}
               for (u, p, cl), c in sorted(app_d.items())]
        )
        if params:
            await session.execute(_UPSERT_BUDGET_USAGE, params)
        late = _late_marks(marks, tz, margin_sec=float(settings.rollup_dirty_margin_sec))
        if late:
            await session.execute(_INSERT_DIRTY_MARKS,
                                  {"days": [m[0] for m in late], "hours": [m[1] for m in late]})
        await session.commit()
    if done:
        logger.warning("repriced", rows=done, budget_scopes=len(params))
        if metrics is not None:
            metrics.repriced.add(done, {"outcome": "repriced"})
    # 커밋 뒤 Redis 카운터 — 키가 있을 때만(없으면 게이트웨이가 DB 에서 재구축한다).
    if redis is not None and done:
        keys = (
            [(f"budget:user:{{{u}}}:{p}", c) for (u, p), c in user_d.items()]
            + [(f"budget:team:{{{t}}}:{p}", c) for (t, p), c in team_d.items()]
            + [(f"budget:user:{{{u}}}:{cl}:{p}", c) for (u, p, cl), c in app_d.items()]
        )
        for key, c in keys:
            try:
                await redis.eval(_REDIS_INCR_IF_EXISTS, 1, key, str(c))
            except Exception:
                # 카운터가 얇아진 채 남는다 — 다음 :lost 재구축/대조(R4)가 잡는다.
                logger.warning("reprice_redis_counter_failed", key=key)
    return done


async def run_reprice(session_factory, redis=None, metrics=None) -> None:
    """APScheduler 진입점 — 예외를 삼키고 로그만 남긴다(다음 주기에 다시)."""
    try:
        while await reprice_once(session_factory, redis, metrics=metrics) >= int(
                get_settings().reprice_batch_size):
            pass
    except Exception:
        logger.exception("reprice_failed")
