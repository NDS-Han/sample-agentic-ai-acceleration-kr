# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Keyset 페이지네이션 복합 커서 — ``(created_at, id)``.

배경: list_users/list_keys 는 ``ORDER BY created_at DESC`` 인데 커서 조건이
``id < cursor`` 였다. UUIDv4 id 순서는 created_at 순서와 무관하므로 2페이지부터
행을 건너뛰거나 반복했다(요약 경로는 이 결함을 알고 전수 로드로 우회해 왔다 —
user_repository.iter_all_users 주석 참조).

커서 형식: ``{epoch_micros}_{uuid}`` — 클라이언트에는 불투명 문자열이다.
구 형식(bare UUID) 커서는 ``decode_cursor`` 가 None 을 돌려주고, 호출자는
구 조건(id < cursor)으로 폴백한다 — 배포 중 스크롤이 끊기지 않게.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    ts = created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return f"{int(ts.timestamp() * 1_000_000)}_{row_id}"


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    try:
        ts_str, id_str = cursor.split("_", 1)
        ts = datetime.fromtimestamp(int(ts_str) / 1_000_000, tz=timezone.utc)
        return ts, uuid.UUID(id_str)
    except (ValueError, AttributeError, OverflowError):
        return None
