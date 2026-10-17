# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.config import Settings
from worker.schemas.recipients import RenderedEmail
from worker.senders.base import EmailSendError
from worker.services.email_mask import mask_email

logger = structlog.get_logger(__name__)


# 핸들러/스위퍼 로그도 같은 형태로 마스킹 — 단일 구현은 services/email_mask.py
_mask_email = mask_email


class SESEmailSender:
    """AWS SES를 통한 이메일 전송 (Post-MVP, optional-deps: boto3).

    boto3는 optional-deps[ses]에 포함되어 있으며 런타임에 import한다.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for SES sender. Install with: uv sync --extra ses"
            ) from exc

        from botocore.config import Config

        # connect/read 상한 — 기본값(60s)은 무응답 엔드포인트에서 워커를 오래 붙잡는다.
        self._client = boto3.client(
            "ses",
            region_name=settings.aws_ses_region or "us-east-1",
            config=Config(
                connect_timeout=settings.email_send_connect_timeout,
                read_timeout=settings.email_send_read_timeout,
            ),
        )
        self._sender_address = settings.email_sender_address
        self._sender_name = settings.email_sender_name

    async def send(self, email: RenderedEmail) -> None:
        import asyncio

        source = f"{self._sender_name} <{self._sender_address}>"
        logger.info(
            "ses_send_started",
            recipient=_mask_email(email.recipient.email),
            subject=email.subject,
            sender=source,
        )
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.send_email(
                    Source=source,
                    Destination={"ToAddresses": [email.recipient.email]},
                    Message={
                        "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": email.html_body, "Charset": "UTF-8"}},
                    },
                ),
            )
            logger.info(
                "ses_send_succeeded",
                recipient=_mask_email(email.recipient.email),
                message_id=response.get("MessageId"),
            )
        except Exception as exc:
            error_code = getattr(getattr(exc, "response", {}).get("Error", {}), "get", lambda k: None)("Code")
            # 주소 오류 계열은 영구 실패. 계정 발송 정지(AccountSendingPaused)는
            # 지금 재시도해도 전부 실패하므로 즉시 터미널로 둔다 — 재개되면 다음
            # 이벤트부터 정상 발송되고, 재시도 3+6회 소모만 줄인다.
            permanent_codes = {
                "MessageRejected",
                "InvalidParameterValue",
                "MailFromDomainNotVerified",
                "AccountSendingPausedException",
                "SendingPausedException",
            }
            retryable = error_code not in permanent_codes
            raise EmailSendError(str(exc), retryable=retryable) from exc
