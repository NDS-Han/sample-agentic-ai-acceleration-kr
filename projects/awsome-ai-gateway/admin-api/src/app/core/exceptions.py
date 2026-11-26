# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations


class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str, code: str = "internal_error"):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(
            message=f"{resource} not found: {identifier}",
            code="not_found",
        )


class ConflictError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, code="conflict")


class ForbiddenError(AppError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message=message, code="forbidden")


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, code="validation_error")


class DateRangeTooLongError(ValidationError):
    """custom 일자 구간이 상한(MAX_RANGE_DAYS)을 넘음 — **422**.

    형식·달력·순서 오류(ValidationError → 400)와 달리 입력은 해석 가능하지만 처리
    정책상 받지 않는 값이라 422 로 구분한다. ValidationError 의 하위형이라 기존
    `except ValidationError` 호출부는 그대로 잡는다.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "date_range_too_long"


class RateLimitedError(AppError):
    """호출 빈도 상한 초과 — **429** + ``Retry-After``(초, 있으면)."""

    def __init__(self, message: str, code: str = "rate_limited", retry_after: int | None = None):
        super().__init__(message=message, code=code)
        self.retry_after = retry_after


class BudgetExceededError(AppError):
    def __init__(self, message: str = "Budget limit exceeded"):
        super().__init__(message=message, code="budget_exceeded")


class BudgetRuleError(AppError):
    """Budget 도메인 규칙 위반 — spec 코드(user_not_in_team 등) + HTTP 상태를 함께 운반.

    budget-rules.md §6-3 의 에러 코드를 error.code 로 그대로 내보내기 위한 타입.
    ValidationError/ForbiddenError 는 code 가 고정이라 spec 코드를 실을 수 없다.
    """

    def __init__(self, message: str, code: str, status_code: int = 422):
        super().__init__(message=message, code=code)
        self.status_code = status_code


class ConfirmationRequiredError(AppError):
    """불변식은 지키지만 저장 즉시 요청 차단을 일으키는 변경 — confirm=true 재요청 필요.

    409 로 반환하고 details 에 차단 영향(누가·얼마만큼)을 실는다
    (budget-rules.md §3-0, D-6/D-16).
    """

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message=message, code="confirmation_required")
        self.details = details or {}


class STSVerificationError(AppError):
    def __init__(self, message: str = "STS identity verification failed"):
        super().__init__(message=message, code="sts_verification_error")


class IntegrationError(AppError):
    """외부 통합(/aicm/v1) 경로의 오류 — 임의 HTTP 상태 + 기계 판독용 code + 선택 Retry-After.

    Cognito 상류 오류(502/503/504)·스코프 부족(403)·멱등 키 충돌(409/422)처럼 기존 타입으로
    상태와 code 를 함께 실을 수 없는 경우에 쓴다. ``AICM_API_ENABLED=false`` 면 아무도
    던지지 않는다.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        retry_after: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(message=message, code=code)
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details
