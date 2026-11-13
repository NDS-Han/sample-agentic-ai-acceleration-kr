# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""R27 F1 — internal_api sender 의 4xx 분류 회귀.

429/408 을 terminal(rejected)로 분류하면 임계치 팬아웃 버스트에서 알림이
재시도 없이 영구 드롭된다. 5xx·408·429 는 retryable, 나머지 4xx 는 terminal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("httpx")

import httpx  # noqa: E402

from worker.config import Settings  # noqa: E402
from worker.schemas.recipients import Recipient, RenderedEmail  # noqa: E402
from worker.senders.base import EmailSendError  # noqa: E402
from worker.senders.internal_api_sender import InternalAPIEmailSender  # noqa: E402


def _settings() -> Settings:
    return Settings(email_api_url="https://mail.internal/send")


def _email() -> RenderedEmail:
    return RenderedEmail(
        subject="s",
        html_body="<p>b</p>",
        recipient=Recipient(email="u@x.io", name="U", role="affected_user"),
    )


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = "err"

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            "e", request=MagicMock(), response=MagicMock(status_code=self.status_code, text=self.text)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(429, True), (408, True), (500, True), (503, True), (400, False), (401, False), (403, False), (422, False)])
async def test_status_classification(status: int, retryable: bool) -> None:
    sender = InternalAPIEmailSender(_settings())
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Resp(status))
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=cm):
        with pytest.raises(EmailSendError) as ei:
            await sender.send(_email())
    assert ei.value.retryable is retryable
