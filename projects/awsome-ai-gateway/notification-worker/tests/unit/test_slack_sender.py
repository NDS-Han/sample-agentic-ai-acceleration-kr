# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Slack webhook sender 계약 — 비밀 URL 보호와 재시도 분류."""
from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from worker.config import Settings
from worker.senders.slack_sender import (
    MockSlackSender,
    SlackMessage,
    SlackSendError,
    SlackWebhookSender,
    create_slack_sender,
)

WebhookURL = "https://hooks.slack.test/services/T000/B000/secret-token"


def _sender(handler) -> SlackWebhookSender:
    transport = httpx.MockTransport(handler)
    return SlackWebhookSender(SecretStr(WebhookURL), transport=transport, timeout_seconds=1.0)


async def test_slack_sender_posts_text_and_blocks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    sender = _sender(handler)
    message = SlackMessage(
        text="fallback",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*hello*"}}],
    )
    await sender.send(message)

    assert len(seen) == 1
    body = seen[0].read().decode()
    assert '"text":"fallback"' in body or '"text": "fallback"' in body
    assert "mrkdwn" in body
    assert seen[0].url.scheme == "https"


async def test_slack_5xx_is_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    with pytest.raises(SlackSendError) as excinfo:
        await _sender(handler).send(SlackMessage(text="x"))
    assert excinfo.value.retryable is True


async def test_slack_4xx_is_not_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="channel not found")

    with pytest.raises(SlackSendError) as excinfo:
        await _sender(handler).send(SlackMessage(text="x"))
    assert excinfo.value.retryable is False


async def test_slack_errors_do_not_leak_webhook_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(SlackSendError) as excinfo:
        await _sender(handler).send(SlackMessage(text="x"))
    rendered = str(excinfo.value)
    assert WebhookURL not in rendered
    assert "secret-token" not in rendered


def test_slack_sender_rejects_non_http_url() -> None:
    with pytest.raises(ValueError):
        SlackWebhookSender(SecretStr("ftp://example.invalid/hook"))
    with pytest.raises(ValueError):
        SlackWebhookSender(SecretStr("https://"))


def test_slack_sender_factory_invalid_url_disables_slack_instead_of_raising() -> None:
    sender = create_slack_sender(Settings(slack_webhook_url=SecretStr("http://example.invalid/hook")))
    assert sender is None
    assert create_slack_sender(Settings(slack_webhook_url=SecretStr("https://"))) is None
    assert create_slack_sender(Settings(slack_webhook_url=SecretStr("not a url"))) is None


async def test_retry_executor_does_not_repeat_permanent_slack_failure(monkeypatch) -> None:
    import worker.services.retry_executor as retry_module

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="channel not found")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(retry_module.asyncio, "sleep", no_sleep)
    sender = _sender(handler)
    executor = retry_module.RetryExecutor()
    with pytest.raises(SlackSendError):
        await executor.execute(
            lambda: sender.send(SlackMessage(text="x")), event_type="pricing_missing"
        )
    assert calls == 1


def test_slack_sender_factory_absent_and_mock(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert create_slack_sender(Settings()) is None
    mocked = create_slack_sender(Settings(slack_webhook_url=SecretStr("mock://slack")))
    assert isinstance(mocked, MockSlackSender)
