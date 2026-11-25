# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""cost exactness (wave 3) — CANCELLED 상태, 단가 누락 표시, dead-letter, 예산 정밀도, 대조 실행 기록

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-25

## 무엇
1. ``usage.usage_status`` 에 ``CANCELLED`` — 클라이언트가 끊은 스트림(B1). 업스트림이 이미 처리한
   토큰은 청구 대상이므로 행은 남고(비용 > 0), 상태만 구분된다.
2. ``usage.usage_logs.pricing_missing`` (BOOLEAN NOT NULL DEFAULT false) — 단가 행이 없어
   $0 으로 기록된 행(B7). 워커의 재가격 잡이 단가가 생기면 비용을 채우고 false 로 내린다.
   부분 인덱스 ``idx_usage_logs_pricing_missing`` (WHERE pricing_missing) — 재가격 잡 조회용.
3. ``usage.cost_dead_letters`` — 워커가 영구 오류(class 22/23)로 기록 못 한 행의 **원본 페이로드**
   (B5). 예전에는 ACK 후 버렸다.
4. ``budget.budget_usages.used_usd`` NUMERIC(12,4) → NUMERIC(16,6) — 행 비용(NUMERIC(10,6))과 같은
   소수 6자리. 배치 합산마다 4자리로 반올림되던 드리프트를 없앤다(B7).
5. ``usage.reconciliation_runs`` — 야간 대조 잡(B8)의 결과.

## 락 (lock-safe, AGENTS.md 규칙 6)
- 문장 단위 autocommit — 문장마다 자기 트랜잭션이라 잠금은 그 문장 동안만 잡힌다.
- ``SET lock_timeout = '5s'`` + 지수 백오프 재시도(최대 6회). 잠금 큐에 서서 뒤의 앱 쿼리를 막지 않는다.
- ``ALTER TYPE … ADD VALUE`` 는 트랜잭션 블록 밖에서 실행한다(autocommit). 새 값은 커밋 뒤에만 쓸
  수 있으므로 같은 마이그레이션 안에서 쓰지 않는다.
- ``usage_logs``: 상수 DEFAULT 의 ADD COLUMN 은 PG11+ 에서 카탈로그만 바뀐다(재작성 없음). 이미
  있으면 ALTER 자체를 건너뛴다 — ``ADD COLUMN IF NOT EXISTS`` 는 no-op 일 때도 ACCESS EXCLUSIVE 를
  잡는다(db/init 의 같은 주의). 인덱스는 ``CREATE INDEX CONCURRENTLY``(쓰기를 막지 않음). 이전
  시도가 남긴 INVALID 인덱스는 지우고 다시 만든다.
- ``budget_usages`` 형 변경은 **재작성**이다(scale 변경). 이 테이블은 (스코프 × 월 × 앱) 행만
  있어 작다(수천~수만 행, ms~1 s). lock_timeout 으로 큐잉을 막는다. 값은 넓어지기만 하므로
  손실이 없다.
- 새 테이블은 비어 있다 — 일반 CREATE INDEX.

## 배포 순서
이 마이그레이션 → cost-recorder-worker → gateway-proxy → admin-api. 구 게이트웨이는 CANCELLED 를
보내지 않고, 구 워커는 CANCELLED 를 ERROR 로 정규화한다(_VALID_USAGE_STATUSES) — 순서가 바뀌어도
INSERT 는 깨지지 않는다.
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

#: 문장당 잠금 대기 상한. AGENTS.md 규칙(≤5s).
LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

#: 트랜잭션 밖에서만 실행 가능한 enum 값 추가.
_ENUM_STMT = "ALTER TYPE usage.usage_status ADD VALUE IF NOT EXISTS 'CANCELLED'"

#: usage_logs 컬럼 — 존재 확인 뒤에만 ALTER 한다(_ensure_usage_logs_column).
_PRICING_MISSING_COL = "pricing_missing"
_PRICING_MISSING_DDL = (
    "ALTER TABLE usage.usage_logs "
    "ADD COLUMN pricing_missing BOOLEAN NOT NULL DEFAULT false"
)
_PRICING_MISSING_INDEX = "idx_usage_logs_pricing_missing"
_PRICING_MISSING_INDEX_DDL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_usage_logs_pricing_missing "
    "ON usage.usage_logs (requested_at) WHERE pricing_missing"
)

_BUDGET_PRECISION = (
    "ALTER TABLE budget.budget_usages ALTER COLUMN used_usd TYPE NUMERIC(16,6)"
)

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS usage.cost_dead_letters (
        id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        request_id   VARCHAR(128),
        stream_id    VARCHAR(64),
        reason       VARCHAR(32)  NOT NULL,
        sqlstate     VARCHAR(5),
        error        TEXT,
        payload      TEXT         NOT NULL,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        replayed_at  TIMESTAMPTZ
    )
    """,
    # 같은 행이 재전달(XAUTOCLAIM)돼도 한 번만 남긴다. request_id 를 못 읽은 행(NULL)은 유니크 밖.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_dead_letters_request "
    "ON usage.cost_dead_letters (request_id) WHERE request_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_cost_dead_letters_open "
    "ON usage.cost_dead_letters (created_at) WHERE replayed_at IS NULL",
    """
    CREATE TABLE IF NOT EXISTS usage.reconciliation_runs (
        id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        started_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        finished_at     TIMESTAMPTZ,
        report_date     DATE         NOT NULL,
        period          VARCHAR(7)   NOT NULL,
        tz              VARCHAR(64)  NOT NULL,
        status          VARCHAR(16)  NOT NULL,
        mismatch_count  INTEGER      NOT NULL DEFAULT 0,
        checks          JSONB        NOT NULL DEFAULT '{}'::jsonb,
        error           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_started "
    "ON usage.reconciliation_runs (started_at DESC)",
]

_DOWNGRADE: list[str] = [
    "DROP TABLE IF EXISTS usage.reconciliation_runs",
    "DROP TABLE IF EXISTS usage.cost_dead_letters",
    # 정밀도는 되돌리지 않는다 — 6자리 → 4자리는 값 손실(반올림)이고, 구 코드는 넓은 열을 그대로 쓴다.
    # pricing_missing 열·인덱스도 남긴다 — 구 코드는 이 열을 모르고 DEFAULT false 로 INSERT 된다.
    # 이 열을 지우는 것은 usage_logs ACCESS EXCLUSIVE 이므로 다운그레이드에서 하지 않는다.
    # enum 값(CANCELLED)은 PostgreSQL 이 제거를 지원하지 않는다 — 구 워커는 ERROR 로 정규화한다.
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
            print(f"[0062] lock timeout (attempt {attempt}) — retrying: "
                  f"{' '.join(stmt.split())[:80]}", flush=True)
            time.sleep(min(2 ** attempt, 20))


def _column_exists(bind, schema: str, table: str, column: str) -> bool:
    return bool(bind.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
    ), {"s": schema, "t": table, "c": column}).scalar())


def _numeric_scale(bind, schema: str, table: str, column: str) -> int | None:
    return bind.execute(text(
        "SELECT numeric_scale FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
    ), {"s": schema, "t": table, "c": column}).scalar()


def _ensure_concurrent_index(bind) -> None:
    """INVALID(이전 CONCURRENTLY 실패 잔해)면 지우고 다시 만든다."""
    valid = bind.execute(text(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'usage' AND c.relname = :name"
    ), {"name": _PRICING_MISSING_INDEX}).scalar()
    if valid is False:
        print("[0062] dropping INVALID idx_usage_logs_pricing_missing", flush=True)
        _exec_retry(bind, f"DROP INDEX CONCURRENTLY IF EXISTS usage.{_PRICING_MISSING_INDEX}")
    if valid is not True:
        _exec_retry(bind, _PRICING_MISSING_INDEX_DDL)


def _upgrade_all() -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        _exec_retry(bind, _ENUM_STMT)
        if not _column_exists(bind, "usage", "usage_logs", _PRICING_MISSING_COL):
            _exec_retry(bind, _PRICING_MISSING_DDL)
        _ensure_concurrent_index(bind)
        if (_numeric_scale(bind, "budget", "budget_usages", "used_usd") or 0) < 6:
            _exec_retry(bind, _BUDGET_PRECISION)
        for stmt in _TABLES:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _downgrade_all() -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in _DOWNGRADE:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(fn) -> None:
    """autocommit 실행 — ADD VALUE·CREATE INDEX CONCURRENTLY 는 트랜잭션 블록 밖이어야 한다.

    db/env.py 가 이미 AUTOCOMMIT 커넥션이면 그대로, 아니면 alembic autocommit_block 으로.
    """
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        fn()
        return
    with op.get_context().autocommit_block():
        fn()


def upgrade() -> None:
    _run(_upgrade_all)


def downgrade() -> None:
    _run(_downgrade_all)
