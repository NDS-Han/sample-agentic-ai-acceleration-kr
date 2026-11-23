"""/admin/monitoring/events — 본문 로그 By request 목록이 쓰는 비용·토큰 필드."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.usage import UsageStatus
from app.routers.monitoring import monitoring_events


def _row(**over) -> SimpleNamespace:
    base = dict(
        request_id="r-1",
        requested_at=datetime(2026, 11, 6, 0, 7, tzinfo=timezone.utc),
        user_id=uuid.uuid4(),
        user_email="dev@example.com",
        model_alias="claude-sonnet",
        status=UsageStatus.SUCCESS,
        latency_ms=900,
        ttft_ms=300,
        cost_usd=Decimal("0.081234"),
        downgraded_from=None,
        client="claude-code",
        input_tokens=2,
        output_tokens=14,
        cache_read_tokens=30000,
        cache_creation_tokens=12000,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _session(rows) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_events_include_cost_and_cache_tokens():
    out = await monitoring_events(
        MagicMock(), limit=50, event_type="all", days=1, admin=MagicMock(), session=_session([_row()])
    )
    ev = out["events"][0]
    assert ev["cost_usd"] == pytest.approx(0.081234)
    assert (ev["input_tokens"], ev["output_tokens"]) == (2, 14)
    # 'Tokens 2→14 인데 $0.08' 오독 방지 — 캐시 토큰이 함께 내려간다.
    assert ev["cache_read_tokens"] == 30000
    assert ev["cache_write_tokens"] == 12000
    assert ev["client"] == "claude-code"
