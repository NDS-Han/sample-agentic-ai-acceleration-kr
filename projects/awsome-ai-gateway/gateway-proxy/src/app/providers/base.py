# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.schemas.domain import TokenUsage

# 어댑터 → 폴백 루프 내부 신호(클라이언트에는 안 나간다 — 라우터는 응답 헤더를
# 별도로 만든다). 클라이언트측 read timeout 처럼 **상류가 요청을 이미 처리/과금했을
# 수 있는** 실패에 단다 — 폴백 루프는 이 표시가 있으면 다음 후보로 재시도하지 않는다
# (재시도하면 같은 추론을 두 모델에 이중 청구할 수 있다). 서버가 명시적으로 실패를
# 알린 응답(ModelTimeoutException 등)에는 달지 않는다.
ATTEMPT_UNSETTLED_HEADER = "x-gw-attempt-unsettled"


def classify_transport_error(exc: BaseException) -> tuple[int, dict, str]:
    """httpx 전송 오류 → (HTTP 상태, 내부 헤더, 메시지) 분류.

    - 연결 수립 실패(ConnectError/ConnectTimeout): 요청이 상류에 닿지 않았다 —
      과금 없음. 503 가용성 신호 → 폴백 안전.
    - 그 외 타임아웃(Read/Write/Pool): 상류가 요청을 받았을 수 있다 — 과금
      불명. 504 + ATTEMPT_UNSETTLED → 폴백 루프가 재시도를 멈춘다.
    - 기타: 502 (폴백 대상이지만 의미는 일반 실패).
    """
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return 503, {}, "upstream connection failed"
    if isinstance(exc, httpx.TimeoutException):
        return 504, {ATTEMPT_UNSETTLED_HEADER: "1"}, "upstream read timeout"
    return 502, {}, "upstream call failed"


class ProviderAdapter(ABC):
    """Provider Adapter 기본 클래스."""

    @abstractmethod
    async def invoke(
        self, request_body: bytes, model_id: str, **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        """비스트리밍 호출.
        Returns: (status_code, body, headers, token_usage)
        """
        ...

    @abstractmethod
    async def invoke_stream(
        self, request_body: bytes, model_id: str, **kwargs
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        """스트리밍 호출.
        Returns: (status_code, chunk_iterator, headers, request_id_or_none)
        TokenUsage는 스트림 종료 시 chunk_iterator에서 추출.
        """
        ...
