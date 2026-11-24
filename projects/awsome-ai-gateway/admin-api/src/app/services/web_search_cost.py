# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AgentCore Web Search 비용 — **모니터링 전용** 읽기 헬퍼.

``usage_logs.web_search_cost_usd`` (migration 0058) 는 cost-recorder-worker 가 기록
시점 단가로 행마다 고정한다. 여기서는 그것을 집계해 응답에 **모델 비용 옆에**
(절대 안에가 아니라) 싣는다.

## 예산·한도와 분리
이 모듈의 값은 ``cost_usd``·``budget_usages``·rate-limit 어디에도 더하지 않는다.
응답 필드도 ``web_search`` 라는 별도 객체로만 나간다 — 기존 ``cost_usd``·
``total_cost_usd`` 의 의미(모델 비용)는 그대로다.

## NULL(미추적) 과 0
  - ``cost_usd``: 추적된 행의 합. 기간 안에 추적된 행이 하나도 없으면 ``None``
    (UI: "미추적" — $0 이라고 쓰면 그 기간이 무료였다는 거짓말이 된다).
  - ``untracked_queries``: 비용이 NULL 인(기능 배포 전) 행의 검색 수. 0 보다 크면 그
    기간 비용은 부분 합계이므로 UI 가 그 사실을 함께 표시한다.

## 상태(status) 필터를 걸지 않는 이유
검색은 모델 호출이 나중에 실패해도 이미 AgentCore 에서 실행·과금됐다. 게이트웨이는
실패 요청에도 누적된 web_search_count 를 싣는다. 그래서 검색 수·비용은 SUCCESS 뿐
아니라 ERROR/TIMEOUT 행까지 센다 — AWS 청구와 같은 기준.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import SystemSetting
from app.models.usage import UsageLog
from app.repositories.system_settings_repository import SystemSettingsRepository

#: system_settings 키 — cost-recorder-worker ``worker/web_search_pricing.py`` 와 같아야 한다.
PRICING_KEY = "web_search_pricing"
TRACKING_SINCE_KEY = "web_search_cost_tracking_since"
#: AWS 정가 $7 / 1,000 queries (https://aws.amazon.com/bedrock/agentcore/pricing/).
DEFAULT_PRICE_PER_1K = Decimal("7.00")
#: 입력 상한 — 워커도 이 값을 넘는 설정은 오염으로 보고 정가를 쓴다.
MAX_PRICE_PER_1K = Decimal("1000")


class WebSearchUsage(BaseModel):
    """기간·범위 안의 웹 검색 사용량. 모델 비용(``cost_usd``)과 별개."""

    queries: int = 0
    #: 추적된 행의 비용 합(USD). 추적된 행이 없으면 None — "미추적" 이지 $0 이 아니다.
    cost_usd: float | None = None
    #: 비용 추적 이전(NULL) 행의 검색 수 — >0 이면 cost_usd 는 부분 합계.
    untracked_queries: int = 0
    #: 비용 추적 시작 시각(워커가 처음 비용을 기록한 시점). 아직 없으면 None.
    tracking_since: datetime | None = None


def ws_columns() -> tuple[Any, Any, Any, Any]:
    """집계 SELECT 에 넣을 (queries, cost, untracked, since) — status 필터 없음(모듈 docstring).

    추적 시작 시각은 스칼라 서브쿼리(PK 조회)로 같은 SELECT 에 싣는다 — 화면마다
    왕복을 하나 더 늘리지 않는다.
    """
    return (
        func.coalesce(func.sum(UsageLog.web_search_count), 0).label("ws_queries"),
        # SUM 은 NULL 을 건너뛴다 — 추적 행이 없으면 NULL 이 그대로 나온다(의도).
        func.sum(UsageLog.web_search_cost_usd).label("ws_cost"),
        func.coalesce(
            func.sum(UsageLog.web_search_count).filter(UsageLog.web_search_cost_usd.is_(None)),
            0,
        ).label("ws_untracked"),
        tracking_since_subquery().label("ws_since"),
    )


def tracking_since_subquery() -> Any:
    return (
        select(SystemSetting.value)
        .where(SystemSetting.key == TRACKING_SINCE_KEY)
        .scalar_subquery()
    )


def ws_from_row(row: Any) -> WebSearchUsage:
    """``ws_columns()`` 를 담은 행 → 응답 객체."""
    cost = row.ws_cost
    return WebSearchUsage(
        queries=int(row.ws_queries or 0),
        cost_usd=round(float(cost), 6) if cost is not None else None,
        untracked_queries=int(row.ws_untracked or 0),
        tracking_since=_parse_since(row.ws_since),
    )


def parse_price(raw: Any) -> Decimal | None:
    """system_settings 값 → 단가. 형상이 틀리면 None(호출자가 정가)."""
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


def _parse_since(raw: Any) -> datetime | None:
    if isinstance(raw, dict):
        raw = raw.get("since")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def get_tracking_since(session: AsyncSession) -> datetime | None:
    row = await SystemSettingsRepository(session).get(TRACKING_SINCE_KEY)
    return _parse_since(row.value) if row is not None else None


async def get_price(session: AsyncSession) -> tuple[Decimal, bool, Any]:
    """(현재 단가, 기본값 여부, 설정 행). 깨진 값은 워커와 같이 정가로 읽는다."""
    row = await SystemSettingsRepository(session).get(PRICING_KEY)
    price = parse_price(row.value) if row is not None else None
    return (price if price is not None else DEFAULT_PRICE_PER_1K), price is None, row
