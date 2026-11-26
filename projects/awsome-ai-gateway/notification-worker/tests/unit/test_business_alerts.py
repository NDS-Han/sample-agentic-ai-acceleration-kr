# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""business Slack alert — 설정 기본값, dedupe/repeat, 감사 로그, 데이터 해석."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import IntegrityError

from worker.config import Settings
from worker.handlers.business_alert_handler import BusinessAlertHandler, delivery_event_id
from worker.listeners.business_alert_task import (
    BusinessAlertTask,
    dead_letter_finding,
    pricing_missing_finding,
    reconciliation_finding,
)
from worker.schemas.events import EventType, NotificationEvent, ServiceSource


def test_business_alerts_are_off_by_default(monkeypatch) -> None:
    for key in (
        "BUSINESS_ALERTS_ENABLED",
        "BUSINESS_ALERT_RECONCILIATION_MISMATCH_ENABLED",
        "BUSINESS_ALERT_DEAD_LETTER_GROWTH_ENABLED",
        "BUSINESS_ALERT_PRICING_MISSING_ENABLED",
        "SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.business_alerts_enabled is False
    assert settings.business_alert_reconciliation_mismatch_enabled is False
    assert settings.business_alert_dead_letter_growth_enabled is False
    assert settings.business_alert_pricing_missing_enabled is False
    assert settings.slack_webhook_url is None
    assert settings.business_alert_poll_interval_seconds > 0
    assert settings.business_alert_repeat_interval_seconds > 0


def test_delivery_event_id_is_stable_per_check_period_and_repeat_window() -> None:
    key = "pricing_missing:2026-11"
    first = delivery_event_id(key, 100)
    assert first == delivery_event_id(key, 100)
    assert first != delivery_event_id(key, 101)
    assert first != delivery_event_id("pricing_missing:2026-12", 100)
    assert len(first) <= 100
    assert first.startswith("biz-")


class _Session:
    def __init__(self, commit_exc: Exception | None = None, existing_row=None):
        self._commit_exc = commit_exc
        self._existing_row = existing_row
        self.added: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1
        if self._commit_exc is not None:
            raise self._commit_exc

    async def rollback(self):
        pass

    async def execute(self, stmt, params=None):
        from sqlalchemy.sql.dml import Update

        result = MagicMock()
        if isinstance(stmt, Update):
            result.scalar_one_or_none.return_value = (
                getattr(self._existing_row, "id", None) if self._existing_row is not None else None
            )
            return result
        result.scalar_one_or_none.return_value = self._existing_row
        return result


def _factory(*, commit_exc: Exception | None = None, existing_row=None):
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(commit_exc=commit_exc, existing_row=existing_row)
        sessions.append(session)
        return session

    factory.sessions = sessions  # type: ignore[attr-defined]
    return factory


def _payload(check: str = "pricing_missing", period: str = "2026-11") -> dict:
    return {
        "check": check,
        "period": period,
        "dedupe_key": f"{check}:{period}",
        "environment": "dev",
        "severity": "warning",
        "title": "Pricing missing for usage rows",
        "summary": "12 usage rows are waiting for model pricing.",
        "facts": [
            {"label": "Open rows", "value": "12"},
            {"label": "Period", "value": period},
        ],
    }


def _event(payload: dict | None = None) -> NotificationEvent:
    return NotificationEvent(
        event_id="poll-event-1",
        type=EventType.PRICING_MISSING,
        timestamp=datetime.now(UTC),
        source=ServiceSource.NOTIFICATION_WORKER,
        payload=payload or _payload(),
    )


def _handler(*, commit_exc: Exception | None = None, existing_row=None, enabled: bool = True):
    config = MagicMock()
    config.enabled = enabled
    config_cache = MagicMock()
    config_cache.get.return_value = config

    sender = MagicMock()
    sender.send = AsyncMock()

    async def _execute(fn, event_type=None):
        return await fn()

    retry_executor = MagicMock()
    retry_executor.execute = AsyncMock(side_effect=_execute)

    factory = _factory(commit_exc=commit_exc, existing_row=existing_row)
    handler = BusinessAlertHandler(
        session_factory=factory,
        config_cache=config_cache,
        slack_sender=sender,
        retry_executor=retry_executor,
        metrics=None,
        repeat_interval_seconds=3600,
    )
    return handler, sender, factory


async def test_business_alert_handle_sends_slack_and_marks_audit_log_sent() -> None:
    handler, sender, factory = _handler()
    await handler.handle(_event())

    sender.send.assert_awaited_once()
    session = factory.sessions[0]
    assert session.added, "delivery audit row was not inserted before send"
    log = session.added[0]
    assert log.channel == "slack"
    assert log.recipient_email == "slack-business-alerts"
    assert log.event_type == "pricing_missing"
    assert log.event_id.startswith("biz-")
    assert log.status == "sent"
    assert log.resolved_at is not None


async def test_business_alert_duplicate_delivery_slot_is_suppressed() -> None:
    handler, sender, _ = _handler(commit_exc=IntegrityError("dup", {}, Exception()))
    await handler.handle(_event())
    sender.send.assert_not_awaited()


async def test_business_alert_config_disabled_is_suppressed_before_audit_write() -> None:
    handler, sender, factory = _handler(enabled=False)
    await handler.handle(_event())
    sender.send.assert_not_awaited()
    assert not factory.sessions


async def test_business_alert_send_failure_is_terminal_failed_for_sweeper_retry() -> None:
    handler, sender, factory = _handler()
    sender.send.side_effect = RuntimeError("slack unreachable")
    await handler.handle(_event())

    log = factory.sessions[0].added[0]
    assert log.status == "failed"
    assert "slack unreachable" in log.error_message


async def test_business_alert_resend_uses_stored_payload_and_marks_sent() -> None:
    payload = _payload()
    row = MagicMock(
        id="log-1",
        event_id="biz-stored",
        status="pending",
        channel="slack",
        event_type="pricing_missing",
        event_payload=payload,
        subject="Pricing missing for usage rows",
    )
    handler, sender, _ = _handler(existing_row=row)

    await handler.resend_log_entry("log-1", claimed_at=datetime.now(UTC))

    sender.send.assert_awaited_once()
    assert row.status == "sent"


def test_reconciliation_finding_uses_latest_mismatch_run_without_id_samples() -> None:
    row = {
        "id": "run-1",
        "status": "mismatch",
        "mismatch_count": 3,
        "report_date": date(2026, 11, 24),
        "period": "2026-11",
        "finished_at": datetime(2026, 11, 25, 1, 0, tzinfo=UTC),
        "checks": {
            "raw_vs_daily": {"mismatches": 0},
            "budget_vs_raw": {"mismatches": 2},
            "pricing": {"mismatches": 1},
        },
    }
    finding = reconciliation_finding(row)
    assert finding is not None
    assert finding.event_type is EventType.RECONCILIATION_MISMATCH
    assert finding.period == "2026-11"
    assert "budget_vs_raw=2" in finding.summary
    assert "pricing=1" in finding.summary
    assert "sample" not in str(finding.details).lower()


def test_reconciliation_finding_ignores_ok_and_error_runs() -> None:
    base = {"period": "2026-11", "checks": {}, "mismatch_count": 0}
    assert reconciliation_finding({**base, "status": "ok"}) is None
    assert reconciliation_finding({**base, "status": "error", "mismatch_count": 2}) is None


def test_dead_letter_finding_reports_growth_count_and_reasons() -> None:
    finding = dead_letter_finding(
        count=4,
        latest_at=datetime(2026, 11, 25, 2, 0, tzinfo=UTC),
        reasons=[{"reason": "numeric", "count": 3}, {"reason": "decode", "count": 1}],
        period="2026-11",
        lookback_seconds=600,
        capped=False,
    )
    assert finding is not None
    assert finding.event_type is EventType.DEAD_LETTER_GROWTH
    assert "4" in finding.summary
    assert "numeric=3" in finding.summary
    assert "request_id" not in str(finding.details).lower()


def test_pricing_missing_finding_reports_open_count_and_models() -> None:
    finding = pricing_missing_finding(
        count=12,
        latest_at=datetime(2026, 11, 25, 2, 0, tzinfo=UTC),
        models=[{"model_alias": "claude-x", "count": 10, "tokens": 12345}],
        period="2026-11",
        capped=False,
    )
    assert finding is not None
    assert finding.event_type is EventType.PRICING_MISSING
    assert "12" in finding.summary
    assert "claude-x" in finding.summary
    assert "user_id" not in str(finding.details).lower()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Rows(self._rows)


class _QuerySession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.statements: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return _ExecResult(self._responses.pop(0) if self._responses else [])


async def test_poll_once_hands_active_pricing_finding_to_handler() -> None:
    session = _QuerySession([
        [{"count": 2, "latest_at": datetime(2026, 11, 25, 2, 0, tzinfo=UTC)}],
        [{"model_alias": "claude-x", "count": 2, "tokens": 100}],
    ])
    handler = MagicMock()
    handler.handle = AsyncMock()
    settings = Settings(
        business_alerts_enabled=True,
        business_alert_pricing_missing_enabled=True,
        reporting_timezone="UTC",
    )
    task = BusinessAlertTask(
        session_factory=lambda: session,
        handler=handler,
        settings=settings,
        metrics=None,
    )

    count = await task.poll_once(now=datetime(2026, 11, 25, 3, 0, tzinfo=UTC))

    assert count == 1
    handler.handle.assert_awaited_once()
    event = handler.handle.await_args.args[0]
    assert event.type is EventType.PRICING_MISSING
    assert event.source is ServiceSource.NOTIFICATION_WORKER
    assert event.payload["check"] == "pricing_missing"
    assert event.payload["period"] == "2026-11"
    assert event.payload["environment"] == settings.app_env
    joined_sql = "\n".join(session.statements)
    assert "WHERE pricing_missing" in joined_sql
    assert "user_id" not in joined_sql
    assert "request_id" not in joined_sql


async def test_poll_once_master_switch_off_does_not_query_or_deliver() -> None:
    session = _QuerySession([])
    handler = MagicMock()
    handler.handle = AsyncMock()
    task = BusinessAlertTask(
        session_factory=lambda: session,
        handler=handler,
        settings=Settings(),
        metrics=None,
    )
    assert await task.poll_once() == 0
    assert not session.statements
    handler.handle.assert_not_awaited()


def test_pricing_missing_finding_marks_scan_cap_and_keeps_rows_bounded() -> None:
    finding = pricing_missing_finding(
        count=500,
        latest_at=None,
        models=[{"model_alias": "m", "count": 500, "tokens": 1000}],
        period="2026-11",
        capped=True,
    )
    assert finding is not None
    assert "500+" in finding.summary
