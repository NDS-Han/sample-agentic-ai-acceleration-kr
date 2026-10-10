# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Anthropic /v1/messages 오류 응답 스키마 헬퍼.

Anthropic API 오류 본문은 ``{"type": "error", "error": {"type": ..., "message": ...}}``
이고 ``error.type`` 은 정해진 enum이다. 게이트웨이가 합성하는 오류가 표준 밖 타입
(``provider_error``, ``service_unavailable``, ``connection_error``)이나 최상위
``"type": "error"`` 필드 누락 상태로 나가면 Anthropic SDK 클라이언트(Claude Code,
Cowork)의 오류 분류가 제네릭 APIError 로 떨어져 재시도/백오프 분기가 어긋난다.

주의 — /v1/responses(OpenAI 호환) 경로는 이 형식을 쓰지 않는다. OpenAI 오류 스키마는
최상위 ``type`` 필드가 없고 ``provider_error`` 가 그쪽에서는 유효한 값이다.
"""

from __future__ import annotations

# 비표준 → Anthropic canonical error.type 매핑
_CANONICAL = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "billing_error": "billing_error",
    "permission_error": "permission_error",
    "not_found_error": "not_found_error",
    "rate_limit_error": "rate_limit_error",
    "timeout_error": "timeout_error",
    "api_error": "api_error",
    "overloaded_error": "overloaded_error",
    # 내부 타입 → 가장 가까운 canonical 값
    "provider_error": "api_error",
    "connection_error": "api_error",
    "service_unavailable": "overloaded_error",
}


def anthropic_error(error_type: str, message: str, **extra) -> dict:
    """Anthropic 표준 오류 봉투를 만든다. 비표준 타입은 canonical 값으로 정규화.

    extra 는 ``error`` 객체 안에 병합된다(code, retry_after 등 도메인 확장 필드).
    ``budget_exceeded`` 같이 canonical enum 에 없는 도메인 타입은 유지된다 — SDK 는
    status code 로 재시도를 분기하므로, 세부 분류는 ``code`` 필드로 전달한다.
    """
    canonical = _CANONICAL.get(error_type, error_type)
    return {
        "type": "error",
        "error": {"type": canonical, "message": message, **extra},
    }
