# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""게이트웨이 DB 폴백(``usage.cost_pending_events``) 소화 (wave 3b, B3).

게이트웨이는 ``cost:stream`` XADD 가 실패하면 페이로드를 메모리 spool 에 넣고, 백그라운드 writer 가
곧바로
이 표에 옮긴다. 이 잡은 그 행을 **스트림 소비와 같은 경로**(``BatchFlusher.flush`` — 재처리 필터,
RETURNING 기준 예산 가산, 시작 원장)로 기록한 뒤 지운다.

* 대상 행은 ``FOR UPDATE SKIP LOCKED`` 로 잡는다 — replica 여럿이 같은 행을 동시에 소화하지 않는다.
  잠금 트랜잭션은 flush(별도 세션·커밋)가 끝난 뒤 DELETE 와 함께 커밋한다. flush 가 실패하면 롤백 →
  행이 남아 다음 주기에 다시.
* flush 는 커밋했는데 DELETE 전에 죽으면 같은 행을 다시 소화한다 — 완료는 재처리 필터가, start 는
  ``NOT EXISTS usage_logs`` + ON CONFLICT 가, close 는 멱등 UPSERT 가 흡수한다(이중청구 없음).
* Redis 가 죽어 있어도 돈다(DB 만 쓴다) — Redis 장애 중에도 usage_logs·budget_usages 가 따라온다.
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import text

from worker.config import get_settings
from worker.schemas.cost_stream import decode_event

logger = structlog.get_logger(__name__)

_LOCK = text(
    "SELECT id, payload FROM usage.cost_pending_events ORDER BY id LIMIT :lim "
    "FOR UPDATE SKIP LOCKED"
)
_DELETE = text("DELETE FROM usage.cost_pending_events WHERE id = ANY(CAST(:ids AS bigint[]))")


async def drain_once(session_factory, flusher, *, limit: int | None = None,
                     metrics: Any = None) -> int:
    """한 배치를 소화한다. 처리(삭제)한 행 수를 돌려준다."""
    lim = int(limit or get_settings().pending_events_batch_size)
    async with session_factory() as lock_session:
        rows = (await lock_session.execute(_LOCK, {"lim": lim})).all()
        if not rows:
            await lock_session.rollback()
            return 0
        items: list = []
        bad: list[tuple[str, str | None, str]] = []
        for r in rows:
            sid = f"pending:{r.id}"
            try:
                item = decode_event(json.loads(r.payload))
                item._raw = r.payload
                item._stream_id = sid
                items.append(item)
            except Exception as exc:
                bad.append((sid, r.payload, str(exc)[:2000]))
        if bad:
            # 디코드 불가 — 원본을 dead-letter 표에 남긴다(실패하면 예외 → 롤백 → 행 보존).
            await flusher.store_undecodable(bad)
        if items:
            await flusher.flush(items)
        await lock_session.execute(_DELETE, {"ids": [int(r.id) for r in rows]})
        await lock_session.commit()
    if metrics is not None and hasattr(metrics, "pending_events"):
        if items:
            metrics.pending_events.add(len(items), {"outcome": "processed"})
        if bad:
            metrics.pending_events.add(len(bad), {"outcome": "undecodable"})
    logger.info("cost_pending_events_drained", count=len(rows), undecodable=len(bad))
    return len(rows)


async def run_pending_drain(session_factory, flusher=None, metrics=None) -> None:
    """APScheduler 진입점 — 배치가 가득 차면 이어서 비운다. 예외는 로그만(다음 주기에 다시)."""
    try:
        lim = int(get_settings().pending_events_batch_size)
        while await drain_once(session_factory, flusher, limit=lim, metrics=metrics) >= lim:
            pass
    except Exception:
        logger.exception("cost_pending_events_drain_failed")
