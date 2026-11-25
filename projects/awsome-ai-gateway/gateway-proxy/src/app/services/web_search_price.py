# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AgentCore Web Search 단가 — 게이트웨이 프로세스 캐시 (wave 3, DECISION 2).

웹 검색 비용(``web_search_count × 단가/1000``)은 청구 총액의 일부다 — 예산(Redis 카운터 +
budget_usages)과 집행에 들어간다. 그래서 finalize 가 요청 1건의 검색 비용을 **한 번** 계산해
Redis 예산 정산과 cost:stream 페이로드(``web_search_cost_usd``)에 같은 값을 싣는다. 워커는 그 값을
usage_logs·budget_usages 에 그대로 쓴다(두 원장이 같은 숫자를 본다).

단가 원천은 cost-recorder-worker ``web_search_pricing.py`` 와 같다:
``public.system_settings['web_search_pricing'] = {"price_per_1k_queries": "7.00"}``, 없거나 깨졌으면
AWS 정가 $7.00/1k.

⚠️ 핫패스에서 DB 를 기다리지 않는다. ``current()`` 는 동기 — 캐시된 값(없으면 정가)을 돌려주고,
   ttl 이 지났으면 백그라운드 갱신을 한 번(single-flight) 띄운다. 갱신 실패는 마지막 값을 유지한다.
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import text

logger = structlog.get_logger(__name__)

DEFAULT_PRICE_PER_1K = Decimal("7.00")
MAX_PRICE_PER_1K = Decimal("1000")
_Q = Decimal("0.000001")
#: usage_logs.web_search_cost_usd NUMERIC(14,6) 상한 — 워커와 같은 클램프.
_MAX_COST = Decimal("99999999.999999")
_SELECT = text("SELECT value FROM public.system_settings WHERE key = 'web_search_pricing'")


def parse_price(raw: Any) -> Decimal | None:
    if isinstance(raw, (bytes, str)):
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


def web_search_cost(count: int | None, price_per_1k: Decimal) -> Decimal:
    """``count × price/1000`` (6자리) — 워커 ``web_search_pricing.web_search_cost`` 와 같은 식."""
    if not count or count <= 0:
        return Decimal("0")
    cost = (Decimal(int(count)) * price_per_1k / Decimal(1000)).quantize(_Q)
    return min(cost, _MAX_COST)


class WebSearchPriceCache:
    def __init__(self, ttl: float = 60.0, timeout: float = 1.0, clock=time.monotonic) -> None:
        self._ttl = ttl
        self._timeout = timeout
        self._clock = clock
        self._price: Decimal | None = None
        self._fetched_at: float | None = None
        self._session_factory = None
        self._refreshing: asyncio.Task | None = None

    def configure(self, session_factory) -> None:
        self._session_factory = session_factory

    def current(self) -> Decimal:
        """마지막으로 읽은 단가(없으면 정가). 오래됐으면 백그라운드 갱신을 띄운다."""
        stale = self._fetched_at is None or self._clock() - self._fetched_at >= self._ttl
        if stale and self._session_factory is not None:
            if self._refreshing is None or self._refreshing.done():
                try:
                    from app.services.streaming import _spawn

                    self._refreshing = _spawn(self.refresh())
                except RuntimeError:  # 이벤트 루프 밖(테스트 등)
                    pass
        return self._price if self._price is not None else DEFAULT_PRICE_PER_1K

    async def refresh(self) -> Decimal:
        if self._session_factory is None:
            return self.current()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session_factory() as session:
                    raw = (await session.execute(_SELECT)).scalar_one_or_none()
            price = parse_price(raw) if raw is not None else None
            if raw is not None and price is None:
                logger.warning("web_search_price_invalid_using_default", raw=str(raw)[:120])
            self._price = price if price is not None else DEFAULT_PRICE_PER_1K
        except Exception:
            # DB 장애 — 마지막 값(없으면 정가)을 유지한다. 다음 ttl 뒤에 다시 시도한다.
            logger.warning("web_search_price_refresh_failed")
        self._fetched_at = self._clock()
        return self._price if self._price is not None else DEFAULT_PRICE_PER_1K


#: 프로세스 싱글턴 — main.py 가 configure(session_factory) 한다.
PRICE = WebSearchPriceCache()
