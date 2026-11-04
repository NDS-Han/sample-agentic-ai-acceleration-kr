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
from datetime import datetime, timedelta, timezone


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    ts = created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    # float 곱셈(ts.timestamp() * 1e6)은 µs 자리에서 1µs 어긋날 수 있다 —
    # 경계 행이 페이지 사이로 빠진다. 정수 나눗셈으로 정확히 내린다.
    micros = (ts - _EPOCH) // timedelta(microseconds=1)
    return f"{micros}_{row_id}"


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    try:
        ts_str, id_str = cursor.split("_", 1)
        # decode 도 float 나눗셈을 쓰면 encode 와 같은 1µs 드리프트가 생긴다.
        ts = _EPOCH + timedelta(microseconds=int(ts_str))
        return ts, uuid.UUID(id_str)
    except (ValueError, AttributeError, OverflowError):
        return None
