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
세션 상세의 ``session_id IN (...) ORDER BY requested_at`` 호출 조회를
``(session_id, requested_at)`` 복합으로 커버한다. 부분 인덱스(NULL 제외)라
대부분이 NULL 인 테이블에서 인덱스 부피를 줄인다. 요약 쿼리는
``requested_at >= window`` 필터라 기존 ``idx_usage_logs_requested_at`` 을
타는 게 정상 — 이 인덱스는 요약용이 아니다.

## 락
db/env.py 가 transaction_per_migration=True 라 ALTER 의 ACCESS EXCLUSIVE 락이
커밋까지 유지되고 같은 트랜잭션의 인덱스 빌드도 그 락 안에서 돈다 — 짧지만
읽기도 막는다(0026 실측 600k 행 기준 ~139ms 수준, dev/초기 운영 규모 허용).
테이블이 커지면 컬럼/인덱스를 분리하거나 수동 CONCURRENTLY 로 처리한다.
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
