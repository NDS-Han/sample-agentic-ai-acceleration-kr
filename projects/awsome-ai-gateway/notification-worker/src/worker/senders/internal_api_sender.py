# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.config import Settings
from worker.schemas.recipients import RenderedEmail
from worker.senders.base import EmailSendError

logger = structlog.get_logger(__name__)


class InternalAPIEmailSender:
    """사내 메일 API를 통한 이메일 전송 (Post-MVP, optional-deps: httpx)."""

    def __init__(self, settings: Settings) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "httpx is required for internal_api sender. Install with: uv sync --extra http"
            ) from exc

        if not settings.email_api_url:
            raise ValueError("EMAIL_API_URL is required for internal_api sender type")

        self._api_url = settings.email_api_url
        self._sender_address = settings.email_sender_address
        self._sender_name = settings.email_sender_name

    async def send(self, email: RenderedEmail) -> None:
        import httpx

        payload = {
            "from": {"address": self._sender_address, "name": self._sender_name},
            "to": [{"address": email.recipient.email, "name": email.recipient.name}],
            "subject": email.subject,
            "html": email.html_body,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._api_url, json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 5xx 외에 408(타임아웃)·429(레이트리밋)도 재시도 대상 — 두 코드를
            # terminal 로 분류하면 임계치 알림 팬아웃 버스트에서 알림이 재시도
            # 없이 영구 rejected 가 된다(R27). Retry-After 는 sweeper 의 다음
            # 주기가 자연 지연 역할을 한다.
            retryable = exc.response.status_code >= 500 or exc.response.status_code in (408, 429)
            raise EmailSendError(
                f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                retryable=retryable,
            ) from exc
        except Exception as exc:
            raise EmailSendError(str(exc), retryable=True) from exc
