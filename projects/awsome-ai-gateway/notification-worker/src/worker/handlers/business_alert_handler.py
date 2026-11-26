# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Business Slack alert delivery through notification_configs + notification_logs.

The email handlers resolve per-user recipients and render HTML. Business alerts are different:
they go to one operator Slack webhook, but they deliberately reuse the same event-type config
(enabled switch), audit table, terminal statuses, unique delivery slot, retry executor, and
pending sweeper recovery path.
"""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.models.notification import NotificationLog
from worker.observability.metrics import WorkerMetrics
from worker.schemas.events import Channel, EventType, NotificationEvent, ServiceSource
from worker.senders.slack_sender import SlackMessage, SlackSendError
from worker.services.config_cache import ConfigCache
from worker.services.retry_executor import RetryExecutor

logger = structlog.get_logger(__name__)

BUSINESS_ALERT_EVENT_TYPES = (
    EventType.RECONCILIATION_MISMATCH,
    EventType.DEAD_LETTER_GROWTH,
    EventType.PRICING_MISSING,
)
SLACK_RECIPIENT_SLOT = "slack-business-alerts"


def delivery_event_id(dedupe_key: str, repeat_window: int) -> str:
    """Deterministic delivery slot for one (check, period) in one repeat window."""
    digest = hashlib.sha256(f"{dedupe_key}|{repeat_window}".encode()).hexdigest()[:32]
    return f"biz-{digest}"


def _mrkdwn(value: object, limit: int = 240) -> str:
    text = str(value)[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_business_alert_message(payload: dict[str, Any]) -> SlackMessage:
    """Build a compact Slack message from the self-contained business-alert payload."""
    try:
        check = str(payload["check"])
        period = str(payload["period"])
        title = str(payload["title"])
        summary = str(payload["summary"])
    except (KeyError, TypeError) as exc:
        raise ValueError("business alert payload misses check/period/title/summary") from exc
    severity = str(payload.get("severity") or "warning").upper()
    environment = str(payload.get("environment") or "dev")
    facts = payload.get("facts") or []
    if not isinstance(facts, list):
        facts = []

    fallback = f"[{environment}] {title} — {summary}"[:2000]
    header = f"*{_mrkdwn(title, 180)}*\n{_mrkdwn(summary, 700)}"
    fields = [
        {"type": "mrkdwn", "text": f"*Severity*\n{_mrkdwn(severity, 30)}"},
        {"type": "mrkdwn", "text": f"*Check*\n`{_mrkdwn(check, 80)}`"},
        {"type": "mrkdwn", "text": f"*Period*\n`{_mrkdwn(period, 80)}`"},
        {"type": "mrkdwn", "text": f"*Environment*\n`{_mrkdwn(environment, 80)}`"},
    ]
    for fact in facts[:6]:
        if not isinstance(fact, dict):
            continue
        label = _mrkdwn(fact.get("label", ""), 60)
        value = _mrkdwn(fact.get("value", ""), 180)
        if label and value:
            fields.append({"type": "mrkdwn", "text": f"*{label}*\n{value}"})
    return SlackMessage(
        text=fallback,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "section", "fields": fields[:10]},
        ],
    )


class BusinessAlertHandler:
    """Slack delivery handler with DB-backed dedupe and sweeper-compatible resend."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config_cache: ConfigCache,
        slack_sender,
        retry_executor: RetryExecutor,
        metrics: WorkerMetrics | None = None,
        repeat_interval_seconds: int = 6 * 3600,
    ) -> None:
        self._session_factory = session_factory
        self._config_cache = config_cache
        self._slack_sender = slack_sender
        self._retry_executor = retry_executor
        self._metrics = metrics
        self._repeat_interval_seconds = repeat_interval_seconds

    def _delivery_metric(self, event: NotificationEvent, status: str) -> None:
        if self._metrics:
            self._metrics.business_alert_deliveries_total.add(
                1,
                {
                    "check": str(event.payload.get("check") or event.type.value),
                    "status": status,
                },
            )

    def _enabled(self, event_type: str) -> bool:
        config = self._config_cache.get(event_type)
        return bool(config is not None and config.enabled)

    async def handle(self, event: NotificationEvent) -> None:
        """Deliver one active alert if this repeat window has no delivery slot yet."""
        if not self._enabled(event.type.value):
            logger.info("business_alert_disabled", event_type=event.type.value)
            self._delivery_metric(event, "disabled")
            return
        if self._slack_sender is None:
            logger.warning("business_alert_slack_not_configured", event_type=event.type.value)
            self._delivery_metric(event, "no_webhook")
            return

        payload = dict(event.payload)
        check = str(payload.get("check") or event.type.value)
        period = str(payload.get("period") or "unknown")
        dedupe_key = str(payload.get("dedupe_key") or f"{check}:{period}")
        window = int(time.time() // self._repeat_interval_seconds)
        delivery_id = delivery_event_id(dedupe_key, window)
        payload.update({
            "check": check,
            "period": period,
            "dedupe_key": dedupe_key,
            "repeat_window": window,
        })
        delivery_event = event.model_copy(update={"event_id": delivery_id, "payload": payload})

        async with self._session_factory() as session:
            log = NotificationLog(
                event_id=delivery_id,
                event_type=event.type.value,
                channel=Channel.SLACK.value,
                recipient_email=SLACK_RECIPIENT_SLOT,
                recipient_user_id=None,
                subject=str(payload.get("title") or event.type.value)[:500],
                status="pending",
                attempt_count=0,
                event_payload=payload,
            )
            try:
                session.add(log)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "business_alert_repeat_suppressed",
                    event_type=event.type.value,
                    dedupe_key=dedupe_key,
                    repeat_window=window,
                )
                self._delivery_metric(delivery_event, "suppressed")
                return
            await self._send_and_finalize(session, delivery_event, log)

    async def resend_log_entry(self, log_id: str, claimed_at: datetime | None = None) -> None:
        """Resend a claimed pending Slack row; mirrors BaseHandler's fencing contract."""
        async with self._session_factory() as session:
            if claimed_at is not None:
                fenced = (
                    await session.execute(
                        update(NotificationLog)
                        .where(
                            NotificationLog.id == log_id,
                            NotificationLog.status == "pending",
                            NotificationLog.last_attempt_at == claimed_at,
                        )
                        .values(last_attempt_at=datetime.now(UTC))
                        .returning(NotificationLog.id)
                    )
                ).scalar_one_or_none()
                if fenced is None:
                    logger.info("business_alert_resend_claim_lost", log_id=log_id)
                    return
                await session.commit()

            log = (
                await session.execute(
                    select(NotificationLog).where(NotificationLog.id == log_id)
                )
            ).scalar_one_or_none()
            if log is None or log.status != "pending":
                return

            def skip(reason: str) -> None:
                log.status = "skipped"
                log.error_message = f"resend_skipped_{reason}"
                log.resolved_at = datetime.now(UTC)

            if log.channel != Channel.SLACK.value:
                skip("not_slack")
                await session.commit()
                return
            try:
                event_type = EventType(log.event_type)
            except ValueError:
                skip("unknown_event_type")
                await session.commit()
                return
            if event_type not in BUSINESS_ALERT_EVENT_TYPES:
                skip("not_business_alert")
                await session.commit()
                return
            if not self._enabled(log.event_type):
                skip("config_disabled")
                await session.commit()
                return
            if self._slack_sender is None:
                # 인프라/설정 일시 상태로 보고 pending 을 유지한다 — 다음 sweeper 가 재시도.
                logger.warning("business_alert_resend_no_webhook", log_id=log_id)
                return

            payload = dict(log.event_payload or {})
            try:
                render_business_alert_message(payload)
            except ValueError:
                skip("invalid_payload")
                await session.commit()
                return
            event = NotificationEvent(
                event_id=log.event_id,
                type=event_type,
                timestamp=datetime.now(UTC),
                source=ServiceSource.NOTIFICATION_WORKER,
                payload=payload,
            )
            await self._send_and_finalize(session, event, log)

    async def _send_and_finalize(
        self,
        session: AsyncSession,
        event: NotificationEvent,
        log: NotificationLog,
    ) -> None:
        """Send the self-contained Slack message and persist the terminal delivery state."""
        try:
            message = render_business_alert_message(event.payload)
        except ValueError as exc:
            log.status = "rejected"
            log.error_message = str(exc)
            log.resolved_at = datetime.now(UTC)
            self._delivery_metric(event, "rejected")
        else:
            try:
                await self._retry_executor.execute(
                    fn=lambda m=message: self._slack_sender.send(m),
                    event_type=event.type.value,
                )
                log.status = (
                    "mocked" if getattr(self._slack_sender, "mocked", False) is True else "sent"
                )
                log.error_message = None
                log.resolved_at = datetime.now(UTC)
                self._delivery_metric(event, log.status)
            except Exception as exc:
                log.status = (
                    "failed"
                    if not isinstance(exc, SlackSendError) or exc.retryable
                    else "rejected"
                )
                log.error_message = str(exc)[:1000]
                log.resolved_at = datetime.now(UTC)
                self._delivery_metric(event, log.status)
                logger.error(
                    "business_alert_slack_send_failed",
                    event_type=event.type.value,
                    error=str(exc)[:300],
                )

        log.last_attempt_at = datetime.now(UTC)
        try:
            await session.commit()
        except Exception as exc:
            logger.error(
                "business_alert_log_write_failed",
                event_type=event.type.value,
                error=str(exc)[:300],
            )
            if self._metrics:
                self._metrics.errors_total.add(1, {"error_type": "db"})
