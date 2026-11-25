# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""cost:stream XADD 실패 시의 DB 폴백 (wave 3b, B3).

``CostStreamSpool`` 은 메모리 버퍼다 — 파드가 끝나면 사라진다(Fargate 라 디스크 spool 도 파드 교체를
못 넘긴다). 이 저장소는 spool 에 쌓인 페이로드를 ``usage.cost_pending_events`` 에 넣고, 워커가
스트림과
**같은 처리 경로**로 소화한 뒤 지운다.

왜 게이트웨이가 usage_logs 에 직접 INSERT 하지 않는가:
  * 워커의 기록은 usage_logs INSERT 하나가 아니다 — budget_usages 가산(RETURNING 으로 실제
  삽입분만),
    dept 해석, 롤업 dirty 표시, dead-letter, 재처리 필터, 시작 원장(request_starts) 정리까지 한
    트랜잭션이다. 게이트웨이에 두 번째 사본을 두면 두 경로가 어긋나는 순간 이중청구/누락이 난다.
  * 같은 이벤트가 스트림과 이 표 양쪽에 있어도(XADD 가 서버에서는 성공했는데 응답만 잃은 경우) 워커
    한 곳에서 request_id UNIQUE + 재처리 필터로 한 행만 남긴다 — exactly-once 가 한 곳에서 성립한다.
  * 이 표는 append-only 짧은 INSERT 라 Redis 장애 중 DB 에 주는 부하가 작다(배치 1문장).
"""
from __future__ import annotations

import asyncio
import json

import structlog
from sqlalchemy import text

logger = structlog.get_logger(__name__)

_INSERT = text(
    "INSERT INTO usage.cost_pending_events (request_id, kind, payload) "
    "VALUES (:request_id, :kind, :payload)"
)


def _meta(payload_json: str) -> tuple[str | None, str]:
    """(request_id, kind) — 워커의 대상 조회·sweeper 제외 판정용. 파싱 실패해도 행은 남긴다."""
    try:
        d = json.loads(payload_json)
    except (TypeError, ValueError):
        return None, "end"
    if not isinstance(d, dict):
        return None, "end"
    rid = d.get("request_id")
    kind = d.get("kind") or "end"
    return (str(rid)[:128] if rid else None), str(kind)[:8]


class CostEventStore:
    """spool 페이로드를 ``usage.cost_pending_events`` 에 한 트랜잭션으로 넣는다."""

    def __init__(self, session_factory, *, timeout: float = 3.0, metrics=None) -> None:
        self._session_factory = session_factory
        self._timeout = timeout
        self._metrics = metrics

    def set_metrics(self, metrics) -> None:
        self._metrics = metrics

    def _count(self, outcome: str, n: int) -> None:
        m = self._metrics
        if m is not None and hasattr(m, "cost_events_db_fallback_total") and n:
            m.cost_events_db_fallback_total.add(n, {"outcome": outcome})

    async def write(self, payloads: list[str]) -> None:
        """전부 쓰거나 예외 — 부분 성공은 없다(한 트랜잭션). 호출자가 실패분을 spool 로 되돌린다."""
        if not payloads:
            return
        params = []
        for p in payloads:
            rid, kind = _meta(p)
            params.append({"request_id": rid, "kind": kind, "payload": p.replace("\x00", "")})
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session_factory() as session:
                    await session.execute(_INSERT, params)
                    await session.commit()
        except BaseException:
            self._count("failed", len(params))
            raise
        self._count("written", len(params))
        logger.warning("cost_events_written_to_db_fallback", count=len(params))
