# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from typing import Protocol, runtime_checkable

from worker.schemas.recipients import RenderedEmail


class NotificationSendError(Exception):
    """채널 공통 전송 오류.

    retryable=True:  네트워크 오류, 타임아웃, 5xx/429 — 재시도 가능
    retryable=False: 잘못된 주소/URL, 인증 실패, 대부분의 4xx — 영구 오류
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailSendError(NotificationSendError):
    """이메일 전송 오류."""


@runtime_checkable
class EmailSender(Protocol):
    """이메일 전송 인터페이스 (BR-EMAIL-01)."""

    async def send(self, email: RenderedEmail) -> None:
        """이메일을 전송한다.

        실패 시 EmailSendError를 raise한다.
        성공 시 None을 반환한다.
        """
        ...
