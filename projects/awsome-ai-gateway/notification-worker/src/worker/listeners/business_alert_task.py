# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Periodic business-alert checks backed by the usage schema.

These checks detect user-facing billing correctness risk, unlike Alertmanager's infrastructure
alerts. Queries are deliberately small: latest reconciliation run only, bounded dead-letter
window, and capped partial-index-backed open pricing_missing rows. No per-user identifiers
or raw request payloads are copied into Slack or notification_logs.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.handlers.business_alert_handler import BusinessAlertHandler
from worker.observability.metrics import WorkerMetrics
from worker.schemas.events import EventType, NotificationEvent, ServiceSource

logger = structlog.get_logger(__name__)

_RECONCILIATION_SQL = """
SELECT status, mismatch_count, report_date, period, finished_at, checks
FROM usage.reconciliation_runs
ORDER BY started_at DESC
LIMIT 1
"""

_DEAD_LETTER_TOTAL_SQL = """
SELECT count(*) AS count, max(created_at) AS latest_at
FROM usage.cost_dead_letters
WHERE created_at >= :since
"""

_DEAD_LETTER_REASON_SQL = """
SELECT reason, count(*) AS count
FROM usage.cost_dead_letters
WHERE created_at >= :since
GROUP BY reason
ORDER BY count DESC, reason ASC
LIMIT 5
"""

# pricing_missing 은 open backlog 라 시간 하한이 없다. 그래도 장애 최악case 에 무제한
# index scan 이 되지 않게 최신 N건만 본다 — 알림은 정확한 전수보다 빠른 신호가 목적이다.
_PRICING_SCAN_CAP = 500
_PRICING_TOTAL_SQL = f"""
WITH bounded AS (
    SELECT requested_at
    FROM usage.usage_logs
    WHERE pricing_missing
    ORDER BY requested_at DESC
    LIMIT {_PRICING_SCAN_CAP}
)
SELECT count(*) AS count, max(requested_at) AS latest_at
FROM bounded
"""

_PRICING_MODEL_SQL = f"""
WITH bounded AS (
    SELECT model_alias, input_tokens, output_tokens
    FROM usage.usage_logs
    WHERE pricing_missing
    ORDER BY requested_at DESC
    LIMIT {_PRICING_SCAN_CAP}
)
SELECT model_alias, count(*) AS count, sum(input_tokens + output_tokens) AS tokens
FROM bounded
GROUP BY model_alias
ORDER BY count DESC, model_alias ASC
LIMIT 5
"""


@dataclass(frozen=True)
class AlertFinding:
    event_type: EventType
    check: str
    period: str
    severity: str
    title: str
    summary: str
    details: list[dict[str, str]] = field(default_factory=list)


def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value or "unknown")


def reconciliation_finding(row: dict[str, Any] | None) -> AlertFinding | None:
    """Interpret the latest reconciliation row; only status=mismatch is actionable."""
    if not row or row.get("status") != "mismatch":
        return None
    checks = row.get("checks") or {}
    if not isinstance(checks, dict):
        checks = {}
    parts: list[str] = []
    for name in sorted(checks):
        detail = checks.get(name)
        if isinstance(detail, dict):
            try:
                mismatches = int(detail.get("mismatches") or 0)
            except (TypeError, ValueError):
                mismatches = 0
            if mismatches > 0:
                parts.append(f"{name}={mismatches}")
    breakdown = ", ".join(parts) if parts else "details in usage.reconciliation_runs"
    count = int(row.get("mismatch_count") or 0)
    period = str(row.get("period") or "unknown")
    return AlertFinding(
        event_type=EventType.RECONCILIATION_MISMATCH,
        check="reconciliation_mismatch",
        period=period,
        severity="critical" if count >= 10 else "warning",
        title="Usage reconciliation mismatch detected",
        summary=(
            f"Nightly reconciliation found {count} mismatches for {period} ({breakdown})."
        ),
        details=[
            {"label": "Mismatches", "value": str(count)},
            {"label": "Report date", "value": str(row.get("report_date") or "unknown")},
            {"label": "Run finished", "value": _fmt_dt(row.get("finished_at"))},
            {"label": "Breakdown", "value": breakdown},
        ],
    )


def dead_letter_finding(
    *,
    count: int,
    latest_at,
    reasons: list[dict[str, Any]],
    period: str,
    lookback_seconds: int,
    capped: bool,
) -> AlertFinding | None:
    if count <= 0:
        return None
    reason_text = ", ".join(
        f"{r.get('reason') or 'unknown'}={int(r.get('count') or 0)}" for r in reasons
    ) or "reason unavailable"
    if capped:
        reason_text += ", …"
    return AlertFinding(
        event_type=EventType.DEAD_LETTER_GROWTH,
        check="dead_letter_growth",
        period=period,
        severity="critical" if count >= 10 else "warning",
        title="Cost dead-letter growth detected",
        summary=(
            f"{count} cost events were written to dead letters in the last "
            f"{lookback_seconds} seconds ({reason_text})."
        ),
        details=[
            {"label": "New dead letters", "value": str(count)},
            {"label": "Latest at", "value": _fmt_dt(latest_at)},
            {"label": "Lookback", "value": f"{lookback_seconds}s"},
            {"label": "Reasons", "value": reason_text},
        ],
    )


def pricing_missing_finding(
    *,
    count: int,
    latest_at,
    models: list[dict[str, Any]],
    period: str,
    capped: bool,
) -> AlertFinding | None:
    if count <= 0:
        return None
    model_text = ", ".join(
        f"{m.get('model_alias') or 'unknown'}={int(m.get('count') or 0)}"
        for m in models
    ) or "model unavailable"
    accounted = sum(int(m.get("count") or 0) for m in models)
    if capped or accounted < count:
        model_text += ", …"
    tokens = sum(int(m.get("tokens") or 0) for m in models)
    count_text = f"{count}+" if capped else str(count)
    return AlertFinding(
        event_type=EventType.PRICING_MISSING,
        check="pricing_missing",
        period=period,
        severity="critical" if count >= 50 else "warning",
        title="Pricing missing for usage rows",
        summary=(
            f"{count_text} usage rows are waiting for model pricing ({model_text})."
        ),
        details=[
            {"label": "Open rows", "value": str(count)},
            {"label": "Latest usage", "value": _fmt_dt(latest_at)},
            {"label": "Models", "value": model_text},
            {"label": "Tokens in top models", "value": f"{tokens:,}"},
        ],
    )


class BusinessAlertTask:
    """Periodic bounded checks feeding Slack business alerts."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        handler: BusinessAlertHandler,
        settings: Settings,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handler = handler
        self._settings = settings
        self._metrics = metrics

    def _metric(self, check: str, result: str) -> None:
        if self._metrics:
            self._metrics.business_alerts_checked_total.add(1, {"check": check, "result": result})

    def _period(self, now: datetime) -> str:
        tz = ZoneInfo(self._settings.reporting_timezone)
        return now.astimezone(tz).strftime("%Y-%m")

    async def __call__(self) -> None:
        interval = float(self._settings.business_alert_poll_interval_seconds)
        logger.info("business_alert_task_started", interval_seconds=interval)
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as exc:
                logger.error("business_alert_poll_failed", error=str(exc)[:300], exc_info=True)
                if self._metrics:
                    self._metrics.errors_total.add(1, {"error_type": "business_alert_poll"})
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    async def poll_once(self, now: datetime | None = None) -> int:
        """Run enabled checks once. Returns the number of active findings handed to delivery."""
        now = now or datetime.now(UTC)
        if not self._settings.business_alerts_enabled:
            return 0
        findings = 0
        period = self._period(now)
        pending_events: list[tuple[str, NotificationEvent]] = []
        # 읽기 세션은 쿼리 동안만 잡는다 — Slack 네트워크 재시도(수 초) 동안 DB
        # 커넥션을 점유하지 않기 위해 delivery 는 session 밖에서 한다.
        async with self._session_factory() as session:
            checks = [
                (
                    "reconciliation_mismatch",
                    self._settings.business_alert_reconciliation_mismatch_enabled,
                    self._check_reconciliation,
                ),
                (
                    "dead_letter_growth",
                    self._settings.business_alert_dead_letter_growth_enabled,
                    lambda s: self._check_dead_letters(s, now, period),
                ),
                (
                    "pricing_missing",
                    self._settings.business_alert_pricing_missing_enabled,
                    lambda s: self._check_pricing_missing(s, period),
                ),
            ]
            for name, enabled, fn in checks:
                if not enabled:
                    continue
                try:
                    finding = await fn(session)
                except Exception as exc:
                    self._metric(name, "error")
                    logger.error("business_alert_check_failed", check=name, error=str(exc)[:300])
                    continue
                if finding is None:
                    self._metric(name, "inactive")
                    continue
                self._metric(name, "active")
                findings += 1
                pending_events.append((
                    name,
                    NotificationEvent(
                        event_id=str(uuid4()),
                        type=finding.event_type,
                        timestamp=now,
                        source=ServiceSource.NOTIFICATION_WORKER,
                        payload={
                            "check": finding.check,
                            "period": finding.period,
                            "dedupe_key": f"{finding.check}:{finding.period}",
                            "environment": self._settings.app_env,
                            "severity": finding.severity,
                            "title": finding.title,
                            "summary": finding.summary,
                            "facts": finding.details,
                        },
                    ),
                ))
        for name, event in pending_events:
            try:
                await self._handler.handle(event)
            except Exception as exc:
                logger.error(
                    "business_alert_delivery_failed",
                    check=name,
                    error=str(exc)[:300],
                    exc_info=True,
                )
        return findings

    async def _check_reconciliation(self, session: AsyncSession) -> AlertFinding | None:
        result = await session.execute(text(_RECONCILIATION_SQL))
        row = result.mappings().first()
        return reconciliation_finding(dict(row) if row else None)

    async def _check_dead_letters(
        self, session: AsyncSession, now: datetime, period: str
    ) -> AlertFinding | None:
        # 2 poll periods catches rows inserted while one poll was delayed; 10 minutes is the floor.
        lookback = max(int(self._settings.business_alert_poll_interval_seconds) * 2, 600)
        since = now - timedelta(seconds=lookback)
        total = (
            await session.execute(text(_DEAD_LETTER_TOTAL_SQL), {"since": since})
        ).mappings().first() or {}
        count = int(total.get("count") or 0)
        if count == 0:
            return None
        rows = (
            await session.execute(text(_DEAD_LETTER_REASON_SQL), {"since": since})
        ).mappings().all()
        reasons = [dict(r) for r in rows]
        accounted = sum(int(r.get("count") or 0) for r in reasons)
        return dead_letter_finding(
            count=count,
            latest_at=total.get("latest_at"),
            reasons=reasons,
            period=period,
            lookback_seconds=lookback,
            capped=accounted < count,
        )

    async def _check_pricing_missing(
        self, session: AsyncSession, period: str
    ) -> AlertFinding | None:
        total = (await session.execute(text(_PRICING_TOTAL_SQL))).mappings().first() or {}
        count = int(total.get("count") or 0)
        if count == 0:
            return None
        rows = (await session.execute(text(_PRICING_MODEL_SQL))).mappings().all()
        models = [dict(r) for r in rows]
        return pricing_missing_finding(
            count=count,
            latest_at=total.get("latest_at"),
            models=models,
            period=period,
            capped=count >= _PRICING_SCAN_CAP,
        )
