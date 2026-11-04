# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""audit.audit_logs (timestamp DESC, id DESC) — 감사 뷰어 기본 목록 인덱스

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-04

감사 뷰어의 기본 목록은 필터 없이 `timestamp DESC, id DESC` keyset 으로 읽는데,
기존 인덱스 3개는 전부 선행 컬럼이 있다(actor/action/resource) — 그 인덱스들로는
필터 없는 정렬 스캔을 못 타서 행이 쌓일수록 기본 화면이 풀 스캔+정렬이 된다.

keyset 커서가 `(timestamp, id)` 튜플 비교이므로 인덱스도 같은 두 컬럼 순서로 둔다.

## CONCURRENTLY 를 쓰지 않는 이유
db/env.py 가 transaction_per_migration=True 라 각 마이그레이션이 트랜잭션에 감싸인다.
CONCURRENTLY 는 트랜잭션 블록 불가(0026/0034 의 같은 주석 참조). audit_logs 는
INSERT-only 테이블이라 짧은 SHARE 락 동안 쓰기가 잠깐 밀리는 수준 — dev/초기 운영
규모에서 허용된다. 행 수가 커지면 수동 CONCURRENTLY 로 따로 처리한다.
"""
from alembic import op
from sqlalchemy import text

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_ts_id "
            "ON audit.audit_logs (timestamp DESC, id DESC)"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS audit.idx_audit_logs_ts_id"))
