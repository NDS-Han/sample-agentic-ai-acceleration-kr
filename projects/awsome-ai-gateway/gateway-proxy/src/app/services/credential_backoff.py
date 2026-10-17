# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""자격증명 갱신 실패의 재시도 백오프 분류기 (R22 r2 S2).

AssumeRole/SigV4 갱신 실패를 단일 30s 백오프로 캐시하면, 일시 스로틀이나
타임아웃 한 번이 fallback 없는 Mantle 경로(codex/cowork)를 파드 단위로 30초
간 블랙홀화한다. 비재시도 오류(AccessDenied/ExternalId 불일치 등)만 길게
억제하고, 전이성 오류는 짧게 재시도한다.
"""
from __future__ import annotations

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)

#: 영구 오류(자격/권한/설정 문제) 백오프 — 같은 실패 재시도를 억제한다.
FAILURE_BACKOFF_S = 30
#: 전이성 오류(스로틀·타임아웃·연결 실패) 백오프 — 빠른 회복을 허용한다.
TRANSIENT_BACKOFF_S = 2

#: 재시도해도 결과가 같지 않은 ClientError 코드.
_NONRETRYABLE_CODES = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "ExpiredTokenException",
    "ValidationError",
    "MalformedPolicyDocument",
})

#: 네트워크 계열 전이성 예외 — STS 응답 코드 없이 발생한다.
_TRANSIENT_EXC = (
    ConnectTimeoutError,
    ReadTimeoutError,
    EndpointConnectionError,
    ConnectionClosedError,
    ProxyConnectionError,
    SSLError,
)


def failure_backoff_s(exc: Exception) -> float:
    """예외 분류별 백오프 초. 비-botocore 예외(설정/코드 결함)는 영구 취급."""
    if isinstance(exc, _TRANSIENT_EXC):
        return TRANSIENT_BACKOFF_S
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _NONRETRYABLE_CODES:
            return FAILURE_BACKOFF_S
        # Throttling*/ServiceUnavailable* 등 서버측 일시 오류
        return TRANSIENT_BACKOFF_S
    return FAILURE_BACKOFF_S


def reraise_fresh(exc: Exception) -> None:
    """캐시된 예외 인스턴스의 누적 __traceback__ 없이 다시 던진다."""
    raise exc.with_traceback(None)
