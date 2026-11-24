# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AgentCore Web Search 단가 스냅샷 + 추적 시작 표지 — **모니터링 전용**.

usage_logs.web_search_cost_usd(migration 0058)를 채운다. 이 값은 ``cost_usd`` 와
별개이며 budget_usages·일별 카운터·임계 알림 어디에도 들어가지 않는다 — 예산과
레이트리밋(CPM/CPH)은 ``cost_usd`` 만 본다.

## 단가
``public.system_settings['web_search_pricing'] = {"price_per_1k_queries": "7.00"}``.
admin-api(Model Management)가 편집하고 감사 로그를 남긴다. 행이 없거나 깨졌으면
AWS 정가 $7.00/1k 를 쓴다. 정가 그대로 계산한다 — 조직 공유 월 5,000건 무료분은
빼지 않는다(UI 가 "실제 청구는 더 낮을 수 있음" 안내를 붙인다).

단가는 flush 마다 읽지 않고 ``ttl`` 초 캐시한다. 관리자가 단가를 바꾸면 최대 ttl 초
뒤 기록분부터 새 단가가 붙고, 이미 기록된 행은 그대로다(행마다 고정 = 이력 보존).

## 추적 시작 표지
``system_settings['web_search_cost_tracking_since'] = {"since": <ISO>}`` — 이 워커가
처음으로 비용을 채운 배치의 가장 이른 requested_at. UI 는 그 이전을 "미추적" 으로
보여준다(과거 행은 NULL — 무료였다는 뜻이 아니다). 복제본이 여럿이면 가장 이른
값이 남는다. 실패해도 usage 기록에는 영향이 없다(best-effort, 다음 flush 에 재시도).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

PRICING_KEY = "web_search_pricing"
TRACKING_SINCE_KEY = "web_search_cost_tracking_since"
#: AWS 정가 — https://aws.amazon.com/bedrock/agentcore/pricing/ ($7 / 1,000 queries).
DEFAULT_PRICE_PER_1K = Decimal("7.00")
#: admin-api 의 입력 상한과 같다. 이를 넘는 값은 오염으로 보고 기본값을 쓴다.
MAX_PRICE_PER_1K = Decimal("1000")
#: usage_logs.web_search_cost_usd NUMERIC(14,6) 의 최댓값 — 넘으면 DataError 로
#: 행(=청구 원장)이 통째로 유실되므로 클램프한다(카운트는 1e12 까지 허용된다).
_MAX_COST = Decimal("99999999.999999")
_Q = Decimal("0.000001")

_SELECT_PRICE = text("SELECT value FROM public.system_settings WHERE key = :key")
_UPSERT_TRACKING_SINCE = text(
    """
    INSERT INTO public.system_settings (key, value, updated_at)
    VALUES (:key, CAST(:value AS jsonb), now())
    ON CONFLICT (key) DO UPDATE
       SET value = EXCLUDED.value, updated_at = now()
     WHERE (public.system_settings.value->>'since')::timestamptz
         > (EXCLUDED.value->>'since')::timestamptz
    """
)


def parse_price(raw: Any) -> Decimal | None:
    """system_settings 값 → 단가. 형상이 틀리면 None(호출자가 기본값)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        raw = raw.get("price_per_1k_queries")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price < 0 or price > MAX_PRICE_PER_1K:
        return None
    return price


def web_search_cost(count: int, price_per_1k: Decimal) -> Decimal:
    """``count × price/1000`` (6자리). 0회면 정확히 0 — 0 은 "추적 중·검색 없음" 이다."""
    if not count or count <= 0:
        return Decimal("0")
    cost = (Decimal(int(count)) * price_per_1k / Decimal(1000)).quantize(_Q)
    if cost > _MAX_COST:
        logger.warning("web_search_cost_clamped", count=count, price=str(price_per_1k))
        return _MAX_COST
    return cost


class WebSearchPricing:
    """단가 TTL 캐시 + 추적 시작 표지 기록. 별도 세션을 써서 usage 트랜잭션과 분리한다
    — 설정 조회가 실패해도 usage_logs/budget 쓰기 트랜잭션은 오염되지 않는다."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._clock = clock
        self._price: Decimal | None = None
        self._fetched_at: float | None = None
        self._tracking_marked = False

    async def price_per_1k(self) -> Decimal:
        now = self._clock()
        fresh = self._fetched_at is not None and now - self._fetched_at < self._ttl
        if self._price is not None and fresh:
            return self._price
        try:
            async with self._session_factory() as session:
                res = await session.execute(_SELECT_PRICE, {"key": PRICING_KEY})
                raw = res.scalar_one_or_none()
            price = parse_price(raw) if raw is not None else None
            if raw is not None and price is None:
                logger.warning("web_search_price_invalid_using_default", raw=str(raw)[:120])
            self._price = price if price is not None else DEFAULT_PRICE_PER_1K
            self._fetched_at = now
        except Exception:
            # 조회 실패 — 마지막으로 알던 단가(없으면 정가)로 기록을 계속한다. 비용 기록이
            # 설정 조회 때문에 막히면 usage_logs/예산 원장까지 멈춘다.
            logger.warning("web_search_price_fetch_failed", exc_info=True)
            if self._price is None:
                return DEFAULT_PRICE_PER_1K
        return self._price

    async def mark_tracking_since(self, earliest_requested_at: datetime) -> None:
        if self._tracking_marked:
            return
        try:
            async with self._session_factory() as session:
                await session.execute(
                    _UPSERT_TRACKING_SINCE,
                    {
                        "key": TRACKING_SINCE_KEY,
                        "value": json.dumps({"since": earliest_requested_at.isoformat()}),
                    },
                )
                await session.commit()
            self._tracking_marked = True
            logger.info("web_search_cost_tracking_marked", since=earliest_requested_at.isoformat())
        except Exception:
            logger.warning("web_search_cost_tracking_mark_failed", exc_info=True)
