# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""cost durability (wave 3b) — 요청 시작 원장(request_starts) + Redis 장애 시 비용 이벤트 DB 폴백(cost_pending_events)

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-25

## 무엇
1. ``usage.request_starts`` — 게이트웨이가 admission 에서 보내는 'start' 이벤트(B2)를 워커가 적는다.
   완료(end) 이벤트가 오면 지운다. 시작 후 일정 시간이 지나도 완료가 없고 그 요청을 처리하던
   게이트웨이 프로세스의 생존 신호(Redis ``cost:live:<instance>``)가 끊겼으면 워커 sweeper 가
   ``estimated_usage=true`` 행을 한 번 쓴다(``swept_at``, ``swept_charge_usd``). 늦게 온 실제 완료는 그
   행을 실제 값으로 바꾸고 예산 원장에 차액만 더한다. ``closed_at`` = 행 없이 끝난 요청(0 토큰 성공,
   상류 4xx 등)의 표지 — 순서가 뒤바뀐 'start' 가 다시 열지 못하게 한다(보존 후 정리).
2. ``usage.cost_pending_events`` — 게이트웨이 XADD 실패 시 비용 이벤트(완료/시작/종료) 페이로드를
   DB 에 넣는 폴백(B3). 워커가 같은 처리 경로(usage_logs request_id UNIQUE, 재처리 필터)로 소화하고
   지운다 — 스트림과 DB 양쪽에 같은 이벤트가 있어도 한 행만 남는다.

## 락 (lock-safe, AGENTS.md 규칙 6)
- 새 빈 테이블과 그 인덱스만 만든다. 기존 테이블(특히 usage.usage_logs)은 건드리지 않는다.
- 문장 단위 autocommit + ``SET lock_timeout = '5s'`` + 재시도(0062 와 같은 방식).

## 배포 순서
이 마이그레이션 → cost-recorder-worker → gateway-proxy. 구 워커는 'start'/'close' 페이로드를 디코드하지
못해 dead-letter 로 보낸다 — 워커를 먼저 올린다.
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

#: 문장당 잠금 대기 상한. AGENTS.md 규칙(≤5s).
LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS usage.request_starts (
        request_id        VARCHAR(128)  PRIMARY KEY,
        instance_id       VARCHAR(200)  NOT NULL DEFAULT '',
        started_at        TIMESTAMPTZ   NOT NULL,
        received_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
        payload           TEXT          NOT NULL,
        closed_at         TIMESTAMPTZ,
        swept_at          TIMESTAMPTZ,
        swept_charge_usd  NUMERIC(16,6)
    )
    """,
    # sweeper 후보(열린 시작) — 오래된 순.
    "CREATE INDEX IF NOT EXISTS idx_request_starts_open ON usage.request_starts (started_at) "
    "WHERE closed_at IS NULL AND swept_at IS NULL",
    # 보존 정리 — 닫힌 표지 / 추정 행을 쓴 시작.
    "CREATE INDEX IF NOT EXISTS idx_request_starts_closed ON usage.request_starts (closed_at) "
    "WHERE closed_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_request_starts_swept ON usage.request_starts (swept_at) "
    "WHERE swept_at IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS usage.cost_pending_events (
        id          BIGSERIAL     PRIMARY KEY,
        request_id  VARCHAR(128),
        kind        VARCHAR(8)    NOT NULL,
        payload     TEXT          NOT NULL,
        created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cost_pending_events_request "
    "ON usage.cost_pending_events (request_id)",
]

_DOWNGRADE: list[str] = [
    # ⚠️ cost_pending_events 에 남은 행은 아직 기록되지 않은 비용이다 — 비어 있을 때만 지운다
    #    (_downgrade_all 이 확인). request_starts 는 sweeper 상태라 지워도 청구 원장은 그대로다.
    "DROP TABLE IF EXISTS usage.cost_pending_events",
    "DROP TABLE IF EXISTS usage.request_starts",
]


def _is_lock_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code is None and orig is not None:
        code = getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return code == "55P03" or "lock timeout" in str(exc).lower()


def _exec_retry(bind, stmt: str) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            bind.execute(text(stmt))
            return
        except DBAPIError as exc:
            if not _is_lock_timeout(exc) or attempt == MAX_ATTEMPTS:
                raise
            print(f"[0063] lock timeout (attempt {attempt}) — retrying: "
                  f"{' '.join(stmt.split())[:80]}", flush=True)
            time.sleep(min(2 ** attempt, 20))


def _upgrade_all() -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in _TABLES:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _downgrade_all() -> None:
    bind = op.get_bind()
    pending = bind.execute(text(
        "SELECT CASE WHEN to_regclass('usage.cost_pending_events') IS NULL THEN 0 "
        "ELSE (SELECT count(*) FROM usage.cost_pending_events) END"
    )).scalar()
    if pending:
        raise RuntimeError(
            f"[0063] usage.cost_pending_events has {pending} unprocessed cost events — "
            "let cost-recorder-worker drain them before downgrading")
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in _DOWNGRADE:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(fn) -> None:
    """autocommit 실행 — 문장마다 자기 트랜잭션(0062 와 같은 규칙)."""
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        fn()
        return
    with op.get_context().autocommit_block():
        fn()


def upgrade() -> None:
    _run(_upgrade_all)


def downgrade() -> None:
    _run(_downgrade_all)
