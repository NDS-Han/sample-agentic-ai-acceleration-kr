# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Periodic business-alert checks backed by the usage schema.

These checks detect user-facing billing correctness risk, unlike Alertmanager's infrastructure
alerts. Queries are deliberately small: latest reconciliation run only, bounded dead-letter
window, and capped partial-index-backed open pricing_missing rows. No per-user identifiers
or raw request payloads are copied into Slack or notification_logs.
"""
from __future__ import annotations

import asyncio
import json
import math
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
from worker.services.config_cache import ConfigCache

logger = structlog.get_logger(__name__)

_RECONCILIATION_SQL = """
SELECT status, mismatch_count, report_date, period, started_at, finished_at, checks
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


@dataclass(frozen=True)
class CheckOutcome:
    finding: AlertFinding | None
    result: str


def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value or "unknown")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def reconciliation_cost_drift_pct(checks: dict[str, Any]) -> float:
    """Largest absolute monetary drift percent exposed by reconciliation checks.

    The producer does not expose one uniform row denominator across all checks. It does expose
    monetary numerator/denominator pairs for the two value-drift checks:
    ``raw_vs_daily(cost_diff, raw_cost)`` and
    ``budget_vs_raw(ledger_minus_expected, raw_charged_total)``.
    Checks without such a pair contribute 0%. This deliberately measures money drift rather
    than the less meaningful fraction of reconciliation sub-checks that failed.
    """
    best = 0.0
    pairs = (
        ("cost_diff", "raw_cost"),
        ("ledger_minus_expected", "raw_charged_total"),
    )
    for detail in checks.values():
        if not isinstance(detail, dict):
            continue
        for numerator_key, denominator_key in pairs:
            numerator = _finite_float(detail.get(numerator_key))
            denominator = _finite_float(detail.get(denominator_key))
            if numerator is None or denominator in (None, 0.0):
                continue
            best = max(best, abs(numerator / denominator) * 100.0)
    return best


def reconciliation_finding(
    row: dict[str, Any] | None,
    *,
    min_count: int = 1,
    min_pct: float = 0.0,
) -> AlertFinding | None:
    """Interpret the latest fresh reconciliation row.

    A mismatch fires when either the absolute mismatch count reaches ``min_count`` or the
    monetary drift percentage reaches ``min_pct``. Defaults (1, 0) preserve the original
    any-mismatch behaviour; setting ``min_pct=0`` disables the percentage trigger.
    """
    if not row or row.get("status") != "mismatch":
        return None
    checks = row.get("checks") or {}
    if not isinstance(checks, dict):
        checks = {}
    count = int(row.get("mismatch_count") or 0)
    drift_pct = reconciliation_cost_drift_pct(checks)
    count_triggered = count >= min_count
    pct_triggered = min_pct > 0 and drift_pct >= min_pct
    if not (count_triggered or pct_triggered):
        return None

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
            {"label": "Cost drift", "value": f"{drift_pct:.4f}%"},
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
        config_cache: ConfigCache,
    ) -> None:
        self._session_factory = session_factory
        self._handler = handler
        self._settings = settings
        self._metrics = metrics
        self._config_cache = config_cache
        self._invalid_override_logged: set[tuple[str, str]] = set()

    def _metric(self, check: str, result: str) -> None:
        if self._metrics:
            self._metrics.business_alerts_checked_total.add(1, {"check": check, "result": result})

    def _period(self, now: datetime) -> str:
        tz = ZoneInfo(self._settings.reporting_timezone)
        return now.astimezone(tz).strftime("%Y-%m")

    def _config_row(self, event_type: str):
        if self._config_cache is None:
            return None
        return self._config_cache.get(event_type)

    def _config_enabled(self, event_type: str) -> bool:
        config = self._config_row(event_type)
        return bool(config is not None and config.enabled)

    def _config_overrides(self, event_type: str) -> dict[str, Any]:
        config = self._config_row(event_type)
        raw = getattr(config, "config", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        return raw if isinstance(raw, dict) else {}

    def _invalid_override(self, event_type: str, key: str) -> None:
        marker = (event_type, key)
        if marker not in self._invalid_override_logged:
            self._invalid_override_logged.add(marker)
            logger.warning(
                "business_alert_config_override_invalid",
                event_type=event_type,
                key=key,
            )

    def _int_override(self, event_type: str, key: str, fallback: int, *, minimum: int = 1) -> int:
        raw = self._config_overrides(event_type).get(key)
        if raw is None:
            return fallback
        try:
            if isinstance(raw, bool):
                raise ValueError("boolean is not a count")
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError("count must be an integer")
            value = int(raw)
            if value < minimum:
                raise ValueError("count below minimum")
        except (TypeError, ValueError):
            self._invalid_override(event_type, key)
            return fallback
        return value

    def _float_override(
        self,
        event_type: str,
        key: str,
        fallback: float,
        *,
        minimum: float = 0.0,
        maximum: float = 100.0,
    ) -> float:
        raw = self._config_overrides(event_type).get(key)
        if raw is None:
            return fallback
        try:
            if isinstance(raw, bool):
                raise ValueError("boolean is not a percentage")
            value = float(raw)
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("percentage outside range")
        except (TypeError, ValueError):
            self._invalid_override(event_type, key)
            return fallback
        return value

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
        checks = [
            (
                "reconciliation_mismatch",
                self._settings.business_alert_reconciliation_mismatch_enabled,
                lambda s: self._check_reconciliation(s, now),
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
        runnable: list[tuple[str, Any]] = []
        for name, env_enabled, fn in checks:
            # env switch와 notification_configs.enabled 는 usage DB를 읽기 **전에** 적용한다.
            # 꺼진 검사는 count 쿼리조차 실행하지 않는다(캐시 조회는 메모리만).
            if not env_enabled or not self._config_enabled(name):
                self._metric(name, "disabled")
                continue
            runnable.append((name, fn))
        if not runnable:
            return 0

        # 읽기 세션은 쿼리 동안만 잡는다 — Slack 네트워크 재시도(수 초) 동안 DB
        # 커넥션을 점유하지 않기 위해 delivery 는 session 밖에서 한다.
        async with self._session_factory() as session:
            for name, fn in runnable:
                try:
                    outcome = await fn(session)
                except Exception as exc:
                    self._metric(name, "error")
                    logger.error("business_alert_check_failed", check=name, error=str(exc)[:300])
                    continue
                if outcome.finding is None:
                    self._metric(name, outcome.result)
                    continue
                finding = outcome.finding
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

    async def _check_reconciliation(self, session: AsyncSession, now: datetime) -> CheckOutcome:
        result = await session.execute(text(_RECONCILIATION_SQL))
        row = result.mappings().first()
        if row is None:
            return CheckOutcome(None, "inactive")
        row_dict = dict(row)
        reference = row_dict.get("finished_at") or row_dict.get("started_at")
        if not isinstance(reference, datetime):
            reference_age = float("inf")
        else:
            reference_age = max(0.0, (_as_utc(now) - _as_utc(reference)).total_seconds())
        max_age = self._int_override(
            "reconciliation_mismatch",
            "max_age_seconds",
            int(self._settings.business_alert_reconciliation_mismatch_max_age_seconds),
        )
        if reference_age > max_age:
            logger.info(
                "business_alert_reconciliation_stale",
                age_seconds=int(reference_age) if math.isfinite(reference_age) else None,
                max_age_seconds=max_age,
            )
            return CheckOutcome(None, "stale")
        min_count = self._int_override(
            "reconciliation_mismatch",
            "min_count",
            int(self._settings.business_alert_reconciliation_mismatch_min_count),
        )
        min_pct = self._float_override(
            "reconciliation_mismatch",
            "min_pct",
            float(self._settings.business_alert_reconciliation_mismatch_min_pct),
        )
        finding = reconciliation_finding(row_dict, min_count=min_count, min_pct=min_pct)
        return CheckOutcome(finding, "active" if finding else "inactive")

    async def _check_dead_letters(
        self, session: AsyncSession, now: datetime, period: str
    ) -> CheckOutcome:
        # 2 poll periods catches rows inserted while one poll was delayed; 10 minutes is the floor.
        lookback = max(int(self._settings.business_alert_poll_interval_seconds) * 2, 600)
        since = now - timedelta(seconds=lookback)
        total = (
            await session.execute(text(_DEAD_LETTER_TOTAL_SQL), {"since": since})
        ).mappings().first() or {}
        count = int(total.get("count") or 0)
        min_count = self._int_override(
            "dead_letter_growth",
            "min_count",
            int(self._settings.business_alert_dead_letter_min_count),
        )
        if count < min_count:
            return CheckOutcome(None, "inactive")
        rows = (
            await session.execute(text(_DEAD_LETTER_REASON_SQL), {"since": since})
        ).mappings().all()
        reasons = [dict(r) for r in rows]
        accounted = sum(int(r.get("count") or 0) for r in reasons)
        finding = dead_letter_finding(
            count=count,
            latest_at=total.get("latest_at"),
            reasons=reasons,
            period=period,
            lookback_seconds=lookback,
            capped=accounted < count,
        )
        return CheckOutcome(finding, "active" if finding else "inactive")

    async def _check_pricing_missing(
        self, session: AsyncSession, period: str
    ) -> CheckOutcome:
        total = (await session.execute(text(_PRICING_TOTAL_SQL))).mappings().first() or {}
        count = int(total.get("count") or 0)
        min_count = self._int_override(
            "pricing_missing",
            "min_count",
            int(self._settings.business_alert_pricing_missing_min_count),
        )
        if count < min_count:
            return CheckOutcome(None, "inactive")
        rows = (await session.execute(text(_PRICING_MODEL_SQL))).mappings().all()
        models = [dict(r) for r in rows]
        finding = pricing_missing_finding(
            count=count,
            latest_at=total.get("latest_at"),
            models=models,
            period=period,
            capped=count >= _PRICING_SCAN_CAP,
        )
        return CheckOutcome(finding, "active" if finding else "inactive")
