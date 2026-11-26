# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""integration tokens (AICM /aicm/v1) — auth.integration_tokens

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-25

## 무엇
``auth.service_tokens`` 행 중 ``itg-`` 접두사 토큰(외부 통합용)에 붙는 권한 부속 행.
토큰 자체(해시·수명·rotate 계보·폐기)는 기존 ``auth.service_tokens`` 를 그대로 쓴다 —
발급/rotate/계보 연쇄 폐기/end-grace 구현을 하나로 유지하고, 사람 admin 의
``DELETE /admin/service-tokens/{id}`` 가 통합 토큰 계보도 즉시 끊는다(비상 정지).

- ``integration``      — 통합 이름(현재 ``aicm`` 하나).
- ``scopes``           — 최소 권한 범위 목록(``users:provision``, ``users:lifecycle`` …).
- ``allowed_cidrs``    — 호출 허용 원천 IP 대역(NULL = 제한 없음).
- ``rate_limit_per_min`` — write 등급 분당 상한 재정의(NULL = 기본값).
- ``lineage_id``       — rotate 계보 공통 id(최초 발급 토큰 id). 레이트리밋 키·목록 범위.

## 락 (lock-safe, AGENTS.md 규칙 6)
- 새 빈 테이블과 인덱스만 만든다. ``auth.service_tokens`` 는 **ALTER 하지 않는다**(ORM·기존
  쿼리 불변). FK 생성은 참조 테이블에 SHARE ROW EXCLUSIVE 를 잠깐 잡는다 — 작은 테이블이고
  ``SET lock_timeout = '5s'`` + 재시도(0063 과 같은 방식).
- ``usage.usage_logs`` 무관.
"""
from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

#: 문장당 잠금 대기 상한. AGENTS.md 규칙(≤5s).
LOCK_TIMEOUT = "5s"
MAX_ATTEMPTS = 6

_TABLES: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS auth.integration_tokens (
        token_id            UUID          PRIMARY KEY REFERENCES auth.service_tokens(id) ON DELETE CASCADE,
        integration         VARCHAR(32)   NOT NULL,
        scopes              TEXT[]        NOT NULL,
        allowed_cidrs       CIDR[],
        rate_limit_per_min  INTEGER,
        lineage_id          UUID          NOT NULL,
        created_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_integration_tokens_lineage "
    "ON auth.integration_tokens (lineage_id)",
]

_DOWNGRADE: list[str] = [
    "DROP TABLE IF EXISTS auth.integration_tokens",
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
            print(f"[0064] lock timeout (attempt {attempt}) — retrying: "
                  f"{' '.join(stmt.split())[:80]}", flush=True)
            time.sleep(min(2 ** attempt, 20))


def _apply(stmts: list[str]) -> None:
    bind = op.get_bind()
    previous = bind.execute(text("SHOW lock_timeout")).scalar()
    bind.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        for stmt in stmts:
            _exec_retry(bind, stmt)
    finally:
        bind.execute(text(f"SET lock_timeout = '{previous}'"))


def _run(fn) -> None:
    """autocommit 실행 — 문장마다 자기 트랜잭션(0062/0063 과 같은 규칙)."""
    if op.get_bind().get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        fn()
        return
    with op.get_context().autocommit_block():
        fn()


def upgrade() -> None:
    _run(lambda: _apply(_TABLES))


def downgrade() -> None:
    # ⚠️ 통합 토큰의 권한 행이 사라지면 itg- 토큰은 /aicm 에서 401 이 된다(토큰 행은 남는다).
    _run(lambda: _apply(_DOWNGRADE))
