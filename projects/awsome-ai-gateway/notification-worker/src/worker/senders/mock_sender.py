# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.schemas.recipients import RenderedEmail

logger = structlog.get_logger(__name__)


from worker.services.email_mask import mask_email as _mask_email


class MockEmailSender:
    """MVP 기본 이메일 센더 — 실제 발송 없이 structlog에 기록 (BR-EMAIL-01).

    개발/테스트 환경에서 EMAIL_SENDER_TYPE=mock 으로 사용.
    이메일 본문은 로그에 포함하지 않음 (BR-SEC-03).
    """

    # 핸들러가 이 플래그를 보고 notification_logs.status 를 'sent' 가 아니라
    # 'mocked' 로 기록하게 한다 — 감사 추적에 "발송된 적 없는 메일"이
    # 발송됨으로 남지 않게 한다.
    mocked = True

    async def send(self, email: RenderedEmail) -> None:
        # 수신자 이메일 마스킹 (BR-SEC-03) — 단일 구현은 services/email_mask.py.
        masked = _mask_email(email.recipient.email)
        logger.info(
            "mock_email_sent",
            recipient=masked,
            subject=email.subject,
            role=email.recipient.role,
        )
