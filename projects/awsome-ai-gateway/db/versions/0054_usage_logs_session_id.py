# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""usage.usage_logs.session_id — 명시적 대화 세션 키 + 조회 인덱스

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-07

관리자 본문-로그 세션 뷰어가 (user, client, 시간인접성) 휴리스틱으로 대화를 묶던
것을 명시적 세션 키로 교체한다. 게이트웨이가 원본 요청 본문의
metadata.user_id "…_session_<id>" 접미(Claude Code) 또는 metadata.session_id 에서
추출해 cost:stream 을 거쳐 기록한다. 못 추출한 요청/구 행은 NULL — 뷰어가 그
행만 휴리스틱으로 폴백 그룹핑한다(600~1000 사용자 규모에서 같은 유저의
동시 대화가 합쳐지는 오류 방지).

## 인덱스
세션 요약은 ``session_id IS NOT NULL AND requested_at >= window`` 를 GROUP BY
하고, 세션 상세는 ``session_id IN (...)`` 으로 호출을 읽는다 — 두 패턴 모두
``(session_id, requested_at)`` 복합 인덱스로 커버된다. 부분 인덱스(NULL 제외)로
대부분이 NULL 인 테이블에서 인덱스 부피를 줄인다.

## CONCURRENTLY 를 쓰지 않는 이유
db/env.py 가 transaction_per_migration=True 라 각 마이그레이션이 트랜잭션에
감싸인다(0053 과 같은 제약). ADD COLUMN 은 메타데이터-only 라 즉시고,
인덱스 생성은 짧은 SHARE 락 — dev/초기 운영 규모에서 허용.
"""
from alembic import op
from sqlalchemy import text

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE usage.usage_logs "
            "ADD COLUMN IF NOT EXISTS session_id VARCHAR(128)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_usage_logs_session "
            "ON usage.usage_logs (session_id, requested_at) "
            "WHERE session_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS usage.idx_usage_logs_session"))
    op.execute(text("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS session_id"))
