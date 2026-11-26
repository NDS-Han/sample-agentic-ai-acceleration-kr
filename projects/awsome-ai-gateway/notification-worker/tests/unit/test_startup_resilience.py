# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Startup resilience — a bad Slack webhook must not take down email notifications."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from worker.config import Settings
from worker.handlers.budget_handler import BudgetHandler
from worker.main import create_delivery_senders
from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.schemas.recipients import Recipient, RecipientRole


class _Session:
    def __init__(self) -> None:
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

    async def rollback(self):
        pass


async def _assert_budget_notification_still_sent(email_sender) -> None:
    email_sender.send = AsyncMock()
    config = MagicMock()
    config.enabled = True
    config.recipient_roles = [RecipientRole.ADMIN]
    config_cache = MagicMock()
    config_cache.get.return_value = config

    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=[
        Recipient(
            email="admin@example.com",
            name="Admin",
            user_id="u-admin",
            role=RecipientRole.ADMIN,
        )
    ])
    template_engine = MagicMock()
    template_engine.render.return_value = ("budget subject", "<html>budget</html>")

    async def _execute(fn, event_type=None):
        return await fn()

    retry_executor = MagicMock()
    retry_executor.execute = AsyncMock(side_effect=_execute)

    handler = BudgetHandler(
        session_factory=lambda: _Session(),
        config_cache=config_cache,
        recipient_resolver=resolver,
        template_engine=template_engine,
        email_sender=email_sender,
        retry_executor=retry_executor,
        metrics=None,
    )
    await handler.handle(NotificationEvent(
        event_id="budget-startup-proof",
        type=EventType.BUDGET_THRESHOLD,
        timestamp=datetime.now(UTC),
        source=ServiceSource.COST_RECORDER_WORKER,
        payload={"user_id": "u1", "threshold_pct": 80},
    ))
    email_sender.send.assert_awaited_once()


@pytest.mark.parametrize("bad_url", [
    "http://hooks.slack.test/services/not-https",
    "https://",
    "not a url",
])
async def test_worker_startup_with_invalid_webhook_still_sends_budget_email(bad_url: str) -> None:
    settings = Settings(
        business_alerts_enabled=True,
        slack_webhook_url=SecretStr(bad_url),
        email_sender_type="mock",
    )

    email_sender, slack_sender = create_delivery_senders(settings)

    assert slack_sender is None
    await _assert_budget_notification_still_sent(email_sender)


async def test_worker_startup_with_missing_slack_webhook_still_sends_budget_email() -> None:
    settings = Settings(
        business_alerts_enabled=True,
        slack_webhook_url=None,
        email_sender_type="mock",
    )

    email_sender, slack_sender = create_delivery_senders(settings)

    assert slack_sender is None
    await _assert_budget_notification_still_sent(email_sender)


def test_invalid_slack_webhook_is_logged_once_without_url(monkeypatch) -> None:
    import worker.senders.slack_sender as slack_module

    bad_url = "http://hooks.slack.test/services/secret-token"
    warning = MagicMock()
    monkeypatch.setattr(slack_module.logger, "warning", warning)

    sender = slack_module.create_slack_sender(Settings(
        business_alerts_enabled=True,
        slack_webhook_url=SecretStr(bad_url),
    ))

    assert sender is None
    warning.assert_called_once()
    rendered = str(warning.call_args)
    assert bad_url not in rendered
    assert "secret-token" not in rendered


def test_missing_slack_webhook_is_logged_without_url(monkeypatch) -> None:
    import worker.senders.slack_sender as slack_module

    warning = MagicMock()
    monkeypatch.setattr(slack_module.logger, "warning", warning)

    sender = slack_module.create_slack_sender(Settings(business_alerts_enabled=True))

    assert sender is None
    warning.assert_called_once()
