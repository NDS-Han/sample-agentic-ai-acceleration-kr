# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Slack incoming-webhook sender for business alerts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from pydantic import SecretStr

from worker.senders.base import NotificationSendError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SlackMessage:
    """Slack webhook payload — ``text`` is the notification fallback, blocks are optional."""

    text: str
    blocks: list[dict[str, Any]] | None = None


class SlackSendError(NotificationSendError):
    """Slack delivery error using the shared retryable/permanent contract."""


class SlackWebhookSender:
    """POST JSON to a Slack incoming webhook.

    The webhook URL is an operator-owned secret. It is never included in raised errors or logs.
    ``transport`` is injectable so unit tests use httpx.MockTransport rather than the network.
    """

    def __init__(
        self,
        webhook_url: SecretStr,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._webhook_url = webhook_url.get_secret_value().strip()
        parsed = urlparse(self._webhook_url)
        try:
            host = parsed.hostname
        except ValueError:
            host = None
        if (
            parsed.scheme.lower() != "https"
            or not host
            or any(ch.isspace() for ch in self._webhook_url)
        ):
            raise ValueError("SLACK_WEBHOOK_URL must be a valid https URL")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def send(self, message: SlackMessage) -> None:
        payload: dict[str, Any] = {"text": message.text}
        if message.blocks:
            payload["blocks"] = message.blocks
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(self._webhook_url, json=payload)
        except Exception as exc:
            raise SlackSendError(f"slack webhook request failed: {type(exc).__name__}") from None

        if response.status_code == 200 and response.text.strip().lower() == "ok":
            return
        retryable = response.status_code == 429 or response.status_code >= 500
        raise SlackSendError(
            f"slack webhook returned HTTP {response.status_code}",
            retryable=retryable,
        )


class MockSlackSender:
    """Local/dev sender — records and logs instead of posting."""

    mocked = True

    def __init__(self) -> None:
        self.messages: list[SlackMessage] = []

    async def send(self, message: SlackMessage) -> None:
        self.messages.append(message)
        logger.info("slack_message_mocked", text=message.text[:200])


def create_slack_sender(settings) -> SlackWebhookSender | MockSlackSender | None:
    """설정된 Slack sender 를 만든다. URL 이 없으면 None — business alerts 는 꺼진 것으로 본다.

    ``mock://slack`` 은 로컬/개발 검증용이다. 실제 webhook 과 달리 네트워크 전송은 없다.
    """
    secret = getattr(settings, "slack_webhook_url", None)
    if secret is None:
        logger.warning("slack_webhook_missing_channel_disabled")
        return None
    url = secret.get_secret_value().strip()
    if not url:
        logger.warning("slack_webhook_missing_channel_disabled")
        return None
    if url == "mock://slack":
        return MockSlackSender()
    try:
        return SlackWebhookSender(
            SecretStr(url), timeout_seconds=float(settings.slack_send_timeout_seconds)
        )
    except (TypeError, ValueError) as exc:
        # URL 은 secret 이므로 로그/에러에 절대 싣지 않는다. 잘못된 Slack 설정 하나가
        # 기존 email notification worker 기동을 막지 않고 Slack 채널만 비활성화한다.
        logger.warning("slack_webhook_invalid_channel_disabled", error_type=type(exc).__name__)
        return None
