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
