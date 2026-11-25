# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage rollup v3 — 웹 검색 열 + 시간별 롤업 + 이벤트 방식 dirty 표시 + 실패 기록

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-25

## 왜
admin-api 의 Dashboard/Analytics/웹 검색/사용자×모델 읽기를 전부 롤업(하이브리드)으로 옮긴다.
그러려면 롤업이 raw 와 같은 질문에 같은 답을 해야 한다.
  - 웹 검색 수·비용이 롤업에 없다 → 대시보드 웹 검색 카드·KPI·한도 사용량이 raw 를 읽어야 했다.
  - "오늘" 이 늘 raw 다 → 오늘 하루 전체(최대 1.4M 행)를 매 요청마다 훑었다.
  - 늦은 로그 표시(batch_flusher)가 레지스트리 행을 UPDATE 해서, 그 날짜를 재집계 중인
    트랜잭션의 행 잠금(집계 전체 시간, cold 47~88s)을 flush 트랜잭션이 기다렸다.
  - 실패한 날짜가 실행 전체를 멈췄고, 그 사실이 어디에도 남지 않았다.

## 무엇
1. daily_aggregates / monthly_aggregates: web_search_count, web_search_cost_usd(NULL=추적 행 없음),
   web_search_untracked(비용 NULL 행의 검색 수). 롤업 입도는 그대로.
2. monthly_aggregate_months.grain_version — 웹 검색 열이 채워진 월(3)만 admin-api 가 믿는다.
   daily_aggregate_days.grain_version 도 워커가 3 으로 올린다(0060 의 2 는 웹 검색 열이 0).
3. daily_aggregate_days.failed_at / last_error — 실패한 날짜를 건너뛰고(skip-forward) 표시.
4. usage.hourly_aggregates + usage.hourly_aggregate_hours — 리포팅 tz 날짜의 시작부터 1시간
   단위 버킷(hour_start = 그 날 00:00 + k 시간, UTC 저장). 오늘(과 아직 일 집계가 없는 날)의
   완료된 시간만 담는다. 일 집계가 끝난 날짜의 시간 행은 워커가 지운다(작게 유지).
5. usage.rollup_dirty_marks — 늦게 도착한 로그의 표시. INSERT 전용·유니크 없음이라 flush 가
   어떤 잠금도 기다리지 않는다. hour_start IS NULL = 날짜 표시, NOT NULL = 시간 표시.
   집계 트랜잭션(REPEATABLE READ)은 자기 스냅샷이 본 표시만 지우므로, 스냅샷 뒤에 커밋된
   로그의 표시는 남는다(다음 시간 잡이 다시 집계).

## 락 (lock-safe)
- 전부 autocommit 블록에서 문장 단위로 실행한다 — 문장마다 자기 트랜잭션이라 잠금은 그 문장
  동안만(카탈로그 변경, ms) 잡힌다. 여러 테이블 잠금을 한 트랜잭션에 쌓지 않는다.
- lock_timeout 5s + 지수 백오프 재시도(최대 6회). 큐에 서서 뒤의 읽기를 막지 않는다.
- usage.usage_logs 는 건드리지 않는다(재작성·인덱스 없음).
- ADD COLUMN 은 NULL 또는 상수 DEFAULT 뿐이라 재작성이 없다(PG11+).
- 새 테이블은 비어 있으므로 인덱스는 일반 CREATE INDEX(즉시).

⚠️ 배포 순서: 이 마이그레이션 → cost-recorder-worker → admin-api. 새 admin-api 는 grain 3 날짜만
롤업에서 읽으므로 워커가 lookback 안의 날짜를 다시 집계하기 전에는 그 날짜를 raw(지연 표시)로
읽는다. lookback 밖의 과거는 `python -m worker.backfill_daily_aggregates` 로 채운다(HANDOFF).
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

#: 문장당 잠금 대기 상한. AGENTS.md 규칙(≤5s).
LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_WS_COLS = (
    ("web_search_count", "BIGINT NOT NULL DEFAULT 0"),
    ("web_search_cost_usd", "NUMERIC(16,6)"),
    ("web_search_untracked", "BIGINT NOT NULL DEFAULT 0"),
)

_UPGRADE: list[str] = [
    *(f"ALTER TABLE usage.daily_aggregates ADD COLUMN IF NOT EXISTS {c} {d}" for c, d in _WS_COLS),
    *(f"ALTER TABLE usage.monthly_aggregates ADD COLUMN IF NOT EXISTS {c} {d}" for c, d in _WS_COLS),
    "ALTER TABLE usage.monthly_aggregate_months "
    "ADD COLUMN IF NOT EXISTS grain_version SMALLINT NOT NULL DEFAULT 2",
    "ALTER TABLE usage.daily_aggregate_days ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ",
    "ALTER TABLE usage.daily_aggregate_days ADD COLUMN IF NOT EXISTS last_error TEXT",
    """
    CREATE TABLE IF NOT EXISTS usage.hourly_aggregates (
        hour_start             TIMESTAMPTZ   NOT NULL,
        date                   DATE          NOT NULL,
        user_id                UUID          NOT NULL,
        team_id                UUID          NOT NULL,
        dept_id                UUID          NOT NULL,
        model_alias            VARCHAR(128)  NOT NULL,
        client                 VARCHAR(32)   NOT NULL,
        status                 usage.usage_status NOT NULL,
        input_tokens           BIGINT        NOT NULL DEFAULT 0,
        output_tokens          BIGINT        NOT NULL DEFAULT 0,
        cache_creation_tokens  BIGINT        NOT NULL DEFAULT 0,
        cache_read_tokens      BIGINT        NOT NULL DEFAULT 0,
        total_tokens           BIGINT        NOT NULL DEFAULT 0,
        total_cost_usd         NUMERIC(16,6) NOT NULL DEFAULT 0,
        call_count             BIGINT        NOT NULL DEFAULT 0,
        request_count          BIGINT        NOT NULL DEFAULT 0,
        latency_ms_sum         BIGINT        NOT NULL DEFAULT 0,
        web_search_count       BIGINT        NOT NULL DEFAULT 0,
        web_search_cost_usd    NUMERIC(16,6),
        web_search_untracked   BIGINT        NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_hourly_aggregates_grain ON usage.hourly_aggregates "
    "(hour_start, user_id, team_id, dept_id, model_alias, client, status)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_aggregates_date ON usage.hourly_aggregates (date)",
    """
    CREATE TABLE IF NOT EXISTS usage.hourly_aggregate_hours (
        hour_start     TIMESTAMPTZ PRIMARY KEY,
        date           DATE        NOT NULL,
        tz             VARCHAR(64) NOT NULL,
        grain_version  SMALLINT    NOT NULL,
        aggregated_at  TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hourly_aggregate_hours_date "
    "ON usage.hourly_aggregate_hours (date)",
    """
    CREATE TABLE IF NOT EXISTS usage.rollup_dirty_marks (
        id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        date        DATE        NOT NULL,
        hour_start  TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rollup_dirty_marks_date ON usage.rollup_dirty_marks (date)",
    "CREATE INDEX IF NOT EXISTS idx_rollup_dirty_marks_hour ON usage.rollup_dirty_marks (hour_start) "
    "WHERE hour_start IS NOT NULL",
]

_DOWNGRADE: list[str] = [
    "DROP TABLE IF EXISTS usage.rollup_dirty_marks",
    "DROP TABLE IF EXISTS usage.hourly_aggregate_hours",
    "DROP TABLE IF EXISTS usage.hourly_aggregates",
    "ALTER TABLE usage.daily_aggregate_days DROP COLUMN IF EXISTS last_error",
    "ALTER TABLE usage.daily_aggregate_days DROP COLUMN IF EXISTS failed_at",
    "ALTER TABLE usage.monthly_aggregate_months DROP COLUMN IF EXISTS grain_version",
    *(f"ALTER TABLE usage.monthly_aggregates DROP COLUMN IF EXISTS {c}" for c, _ in _WS_COLS),
    *(f"ALTER TABLE usage.daily_aggregates DROP COLUMN IF EXISTS {c}" for c, _ in _WS_COLS),
    # 다운그레이드 후 구 워커/구 admin-api 는 grain 2 를 믿는다 — grain 3 표시를 2 로 되돌린다
    # (구 입도와 열 구성이 같으므로 행은 그대로 유효하다).
    "UPDATE usage.daily_aggregate_days SET grain_version = 2 WHERE grain_version > 2",
]


def _is_lock_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code is None and orig is not None:
        code = getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return code == "55P03" or "lock timeout" in str(exc).lower()


def _execute_all(statements: list[str]) -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in statements:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    bind.execute(text(stmt))
                    break
                except DBAPIError as exc:
                    if not _is_lock_timeout(exc) or attempt == MAX_ATTEMPTS:
                        raise
                    print(f"[0061] lock timeout (attempt {attempt}) — retrying: "
                          f"{' '.join(stmt.split())[:80]}", flush=True)
                    time.sleep(min(2 ** attempt, 20))
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(statements: list[str]) -> None:
    """문장 단위 autocommit 실행 + lock_timeout 5s + 재시도. 원래 세션 lock_timeout 을 복원한다.

    db/env.py 는 마이그레이션 커넥션을 이미 AUTOCOMMIT 으로 연다(RDS Proxy 때문에 SET 을
    세션에 거는 방식) — 그때는 문장마다 커밋되므로 그대로 실행한다. 트랜잭션 커넥션이면
    alembic autocommit_block 으로 같은 효과를 낸다.
    """
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        _execute_all(statements)
        return
    with op.get_context().autocommit_block():
        _execute_all(statements)


def upgrade() -> None:
    _run(_UPGRADE)


def downgrade() -> None:
    _run(_DOWNGRADE)
